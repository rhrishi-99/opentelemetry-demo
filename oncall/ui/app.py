"""Demo UI: watch the agent investigate an alert, live or replayed from a run log.

Run from the repo root: streamlit run oncall/ui/app.py
Read-only: it calls the agent and the read-only tools, and reads RUNS_DIR/*.jsonl.
No action is ever executed from here.
"""

import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

# `streamlit run` puts only this file's folder on sys.path, so add the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
import pandas as pd
import streamlit as st

from oncall.agent.loop import investigate_and_diagnose
from oncall.agent.runlog import RunLog
from oncall.agent.taxonomy import CLIENT_SERVICES
from oncall.config import Settings, load_settings
from oncall.endpoints import resolve_endpoints
from oncall.main import _manual_alert
from oncall.models import MAX_EVIDENCE_ITEMS, ToolResult, scrub_flags
from oncall.platform.containers import (
    DockerError,
    check_containers,
    compose_container_ids,
    inspect_containers,
)
from oncall.report import summary
from oncall.report.output import report_run
from oncall.safety.actionlog import read_action_log
from oncall.safety.exclusions import EXCLUDED_SERVICES
from oncall.tools import TOOLS
from oncall.tools.backends import BackendError, LiveBackend, get_backend
from oncall.tools.metrics import _build_promql, _extract_vector_results

DEFAULT_ALERT = "users report errors when shopping"
ANY_SERVICE = "(any service)"
ENV_KEYS = {
    "prometheus": "PROM_URL",
    "jaeger": "JAEGER_URL",
    "opensearch": "OPENSEARCH_URL",
}
MAX_PAST_RUNS = 30
APPROVAL_CAPTION = "Approve/Reject stay disabled until the approval gate lands (F23)."
TOP_EVIDENCE = 3
# The "[confidence: note; note]" suffix the confidence rules add to reasoning.
CONFIDENCE_SUFFIX = re.compile(r"\s*\[confidence: (?P<notes>[^\]]*)\]\s*$")
CATEGORY_WORDS = {
    "container_down": "is down",
    "dependency_unavailable": "dependency unavailable",
    "app_error": "application errors",
    "resource_cpu": "CPU saturation",
    "resource_memory": "memory pressure",
    "network_degradation": "network degradation",
    "traffic_surge": "traffic surge",
    "upstream_propagation": "errors propagated from a dependency",
    "no_incident": "healthy",
    "unknown": "cause unclear",
}
DECISION_BADGES = {
    "act": (":green-background[ACT]", "Safe fix proposed, waiting for your approval"),
    "escalate": (
        ":orange-background[ESCALATE]",
        "Agent is not confident enough to act — review the findings below",
    ),
    "no_action": (":gray-background[NO ACTION]", "Nothing to do"),
}
# evidence id prefix -> (icon, label)
EVIDENCE_LABELS = {
    "metric": ("📈", "Metrics"),
    "trace": ("🔗", "Traces"),
    "log": ("📜", "Logs"),
    "container": ("📦", "Containers"),
    "deploy": ("🚀", "Deploys"),
}
# health signal -> Alert["signal"]
ALERT_SIGNALS = {
    "container": "container",
    "error_rate": "error_rate",
    "latency_p95": "latency",
}
AGENT_STAGES = (
    ("Detective", {"detective"}),
    ("Planner", {"planner"}),
    ("Human", {"human", "human_gate"}),
    ("Operator", {"operator"}),
)

# container_health evidence text: "<name>: state=<state>, health=<health>, ..."
CONTAINER_TEXT = re.compile(r"^(?P<name>[^:]+): state=(?P<state>[^,]+),")
HEALTH_METRICS = ("error_rate", "latency_p95", "rps")
HEALTH_WINDOW = "5m"
BASELINE_OFFSETS_MIN = (30, 15, 10)
# metric -> (regression factor vs baseline, label)
REGRESSION_FACTORS = {
    "error_rate": (3.0, "errors"),
    "latency_p95": (2.0, "p95"),
    "rps": (2.0, "rps"),
}
FLAGGED = " | FLAGGED: "
ERROR_FLOOR = 0.02
# Below this, p95 doubling is noise on fast services (shipping 8 -> 25 ms idle).
P95_FLOOR_MS = 100.0
# Traffic surges only count at the entry points.
SURGE_SERVICES = ("frontend", "frontend-proxy")
AUTO_LABEL = "Auto-detect (prototype — full detector is F14)"
AUTO_INTERVAL_S = 20
AUTO_COOLDOWN_S = 600
# metric -> (alert wording, value format)
SIGNALS = {
    "error_rate": ("error rate", "{:.2f}"),
    "latency_p95": ("p95 latency", "{:.0f} ms"),
    "rps": ("traffic", "{:.2f} rps"),
}


def fetch_metrics(backend) -> tuple[dict[str, dict], int | None]:
    """({service: {metric: (now, baseline or None)}}, baseline offset used).

    Same PromQL as get_metrics, but for all services (get_metrics keeps only its
    top 5 as evidence). The baseline is the first of BASELINE_OFFSETS_MIN with
    data, so the panel still compares after Prometheus restarts.
    """
    now_ts = datetime.now(UTC).timestamp()
    values: dict[str, dict] = {}
    used = None
    for metric in HEALTH_METRICS:
        promql = _build_promql(metric, None, HEALTH_WINDOW)
        now = _extract_vector_results(backend.prom_query(promql))
        base: dict[str, float] = {}
        for offset in BASELINE_OFFSETS_MIN if used is None else (used,):
            at = str(int(now_ts - offset * 60))
            base = _extract_vector_results(backend.prom_query(promql, at=at))
            if base:
                used = offset
                break
        for service, value in now.items():
            values.setdefault(service, {})[metric] = (
                value,
                base.get(service, 0.0) if base else None,
            )
    for metrics in values.values():
        # The error-rate ratio has no series when a service had no errors at all.
        if "rps" in metrics and "error_rate" not in metrics:
            metrics["error_rate"] = (0.0, 0.0 if used is not None else None)
    return values, used


def regressed(
    values: dict, error_floor: float, service: str | None = None
) -> dict[str, str]:
    """{metric: label} for a service's metrics that regressed against a baseline."""
    found = {}
    for metric, (factor, label) in REGRESSION_FACTORS.items():
        if metric == "rps" and service is not None and service not in SURGE_SERVICES:
            continue
        now, base = values.get(metric, (None, None))
        if now is None or base is None:
            continue
        floor = {"error_rate": error_floor, "latency_p95": P95_FLOOR_MS}.get(
            metric, 0.0
        )
        if base > 0 and now >= factor * base and now >= floor:
            found[metric] = f"{label} {now / base:.1f}x"
        elif base == 0 and metric == "error_rate" and now >= error_floor:
            found[metric] = f"{label} new"
    return found


def regressions(
    values: dict, error_floor: float, service: str | None = None
) -> list[str]:
    """Which of a service's metrics regressed against an existing baseline."""
    return list(regressed(values, error_floor, service).values())


def health_rows(
    containers: ToolResult, metrics: dict, error_floor: float
) -> list[dict]:
    """One row per service from container_health output + fetch_metrics() values."""
    rows: dict[str, dict] = {}

    def row(service: str) -> dict:
        return rows.setdefault(
            service, {"service": service, "status": "no container data", "metrics": {}}
        )

    for ev in containers["evidence"] if containers["ok"] else []:
        m = CONTAINER_TEXT.match(ev["text"])
        if m:
            problems = ev["text"].partition(FLAGGED)[2]
            row(m["name"])["status"] = (
                f"unhealthy: {problems}" if problems else m["state"]
            )
    for service, values in metrics.items():
        row(service)["metrics"] = values

    result = []
    for r in rows.values():
        if r["service"] in EXCLUDED_SERVICES:
            continue
        r["client"] = r["service"] in CLIENT_SERVICES
        r["regressed"] = (
            {} if r["client"] else regressed(r["metrics"], error_floor, r["service"])
        )
        r["regressions"] = list(r["regressed"].values())
        r["unhealthy"] = not r["client"] and (
            r["status"].startswith("unhealthy") or bool(r["regressions"])
        )
        result.append(r)
    return sorted(result, key=lambda r: (not r["unhealthy"], r["client"], r["service"]))


def with_dropped_containers(
    result: ToolResult, infos: list[dict], now: datetime
) -> ToolResult:
    """Add the containers container_health left out once it hit MAX_EVIDENCE_ITEMS.

    It lists flagged containers first, so the dropped ones were healthy.
    """
    if not result["ok"]:
        return result
    seen = {e["id"] for e in result["evidence"]}
    dropped = [
        i for i in infos if f"container:{(i.get('Name') or '').lstrip('/')}" not in seen
    ]
    evidence = list(result["evidence"])
    for start in range(0, len(dropped), MAX_EVIDENCE_ITEMS):
        chunk = dropped[start : start + MAX_EVIDENCE_ITEMS]
        evidence += check_containers(chunk, [], None, now, max_healthy=None)["evidence"]
    return {**result, "evidence": evidence}


def all_container_health(settings: Settings) -> ToolResult:
    result = TOOLS["container_health"]()
    if settings.TELEMETRY_BACKEND == "offline":
        return result
    try:
        infos = inspect_containers(compose_container_ids(settings.COMPOSE_PROJECT_NAME))
    except DockerError:
        return result
    return with_dropped_containers(result, infos, datetime.now(UTC))


def tool_steps(events: list[dict]) -> list[dict]:
    """Pair each tool_call event with the tool_result that follows it."""
    steps: list[dict] = []
    for e in events:
        if e["event"] == "tool_call":
            steps.append(
                {
                    "step": e.get("step"),
                    "name": e.get("name"),
                    "args": e.get("args"),
                    "result": None,
                }
            )
        elif e["event"] == "tool_result" and steps and steps[-1]["result"] is None:
            steps[-1]["result"] = e.get("result")
    return steps


def list_runs(runs_dir: str) -> list[Path]:
    """Run logs, newest first."""
    return sorted(
        Path(runs_dir).glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )


def run_label(path: Path) -> str:
    alerts = RunLog(run_id=path.stem, runs_dir=str(path.parent)).events("alert")
    description = (
        (alerts[0].get("alert") or {}).get("description", "") if alerts else ""
    )
    return f"{path.stem} · {description[:40]}" if description else path.stem


@st.cache_resource
def configure() -> tuple[Settings, dict]:
    """Load settings and the endpoints the tools' backends resolve the same way."""
    settings = load_settings()
    if settings.TELEMETRY_BACKEND == "offline":
        return settings, {}
    return settings, resolve_endpoints(settings)


@st.cache_data(ttl=30, show_spinner=False)
def jaeger_v3_services(jaeger_url: str) -> list[str]:
    """Services from Jaeger's v3 API; JAEGER_URL is e.g. http://127.0.0.1:8080/jaeger/ui."""
    res = httpx.get(f"{jaeger_url.rstrip('/')}/api/v3/services", timeout=3)
    res.raise_for_status()
    return res.json().get("services") or []


def probe(endpoints: dict) -> tuple[dict[str, str | None], list[str]]:
    """Reachability per backend (None when reachable, else the error) and Jaeger's services."""
    backend = LiveBackend(
        prom_url=endpoints.get("prometheus"),
        jaeger_url=endpoints.get("jaeger"),
        opensearch_url=endpoints.get("opensearch"),
        timeout=3,
    )
    checks = {
        "prometheus": lambda: backend.prom_query("vector(1)"),
        "jaeger": lambda: jaeger_v3_services(endpoints["jaeger"]),
        "opensearch": lambda: httpx.get(
            backend.opensearch_url, timeout=3
        ).raise_for_status(),
    }
    status: dict[str, str | None] = {}
    services: list[str] = []
    for name, check in checks.items():
        if not endpoints.get(name):
            status[name] = f"endpoint not found; set {ENV_KEYS[name]}"
            continue
        try:
            out = check()
        except (BackendError, httpx.HTTPError, ValueError) as exc:
            status[name] = str(exc)
            continue
        status[name] = None
        if name == "jaeger":
            services = list(out)
    return status, services


def render_header(settings: Settings, endpoints: dict) -> list[str]:
    st.title("AI On-Call Engineer")
    if settings.TELEMETRY_BACKEND == "offline":
        scenario = os.environ.get("OFFLINE_SCENARIO", "baseline")
        st.caption(f"Offline replay of scenario `{scenario}`")
        try:
            return get_backend().jaeger_services()
        except BackendError:
            return []
    status, services = probe(endpoints)
    st.markdown(
        " &nbsp; ".join(
            f"{'🟢' if err is None else '🔴'} {name}" for name, err in status.items()
        )
    )
    errors = {name: err for name, err in status.items() if err}
    if errors:
        with st.expander("Backend errors"):
            for name, err in errors.items():
                st.text(f"{name}: {err}")
    return services


def _fmt_rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.2%}"


def _fmt_ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}"


def _fmt_rps(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _pair(row: dict, metric: str, fmt) -> str:
    now, base = row["metrics"].get(metric, (None, None))
    return f"{fmt(now)} / {fmt(base)}"


def refresh_health(settings: Settings) -> None:
    st.session_state.health_at = time.time()
    with st.spinner("Checking containers, errors, latency and traffic…"):
        try:
            (values, offset), error = fetch_metrics(get_backend()), None
        except BackendError as exc:
            (values, offset), error = ({}, None), str(exc)
        st.session_state.health = (
            all_container_health(settings),
            values,
            offset,
            error,
        )


def alert_signal(r: dict) -> tuple[str, str, str]:
    """(banner text, alert description, signal) for an unhealthy row's first signal."""
    service = r["service"]
    if r["status"].startswith("unhealthy"):
        problems = r["status"].removeprefix("unhealthy: ")
        text = f"{service} container {problems}"
        return text, text, "container"
    metric = next(iter(r["regressed"]))
    name, fmt = SIGNALS[metric]
    now, base = r["metrics"][metric]
    detail = f"{fmt.format(now)} vs baseline {fmt.format(base)}"
    return f"{service} {name} {detail}", f"{name} on {service} {detail}", metric


def primary(unhealthy: list[dict]) -> dict:
    """The row to investigate: a container problem first, else the worst ratio."""

    def key(r: dict) -> tuple:
        ratios = [
            now / base if base else float("inf")
            for metric, (now, base) in r["metrics"].items()
            if metric in r["regressed"]
        ]
        return (not r["status"].startswith("unhealthy"), -max(ratios, default=0))

    return min(unhealthy, key=key)


def should_auto_run(
    services: frozenset, last: tuple | None, now_s: float, failed: bool = False
) -> bool:
    """Run on a service not seen since the last auto-run, or once the cooldown is over.

    `last` is (time, services seen since then), so a metric flapping around its
    threshold does not count as new. After an LLM failure, wait the cooldown.
    """
    if not services:
        return False
    if last is None:
        return True
    at, seen = last
    if now_s - at >= AUTO_COOLDOWN_S:
        return True
    return not failed and bool(services - seen)


def llm_failure(settings: Settings, run_id: str | None) -> str | None:
    """The LLM error that stopped a run, if any."""
    if not run_id:
        return None
    for e in RunLog(run_id=run_id, runs_dir=settings.RUNS_DIR).events("error"):
        if str(e.get("where", "")).startswith("llm"):
            return e.get("error")
    return None


@st.cache_resource
def auto_state() -> dict:
    """State shared by every browser tab: the current run and the auto-run debounce,
    so a reload or a second tab does not start another investigation."""
    return {"lock": threading.Lock()}


def current_run() -> dict:
    return auto_state().get("current") or {}


def is_running(run: dict) -> bool:
    return run.get("thread") is not None and run["thread"].is_alive()


def suppressed_reason(
    settings: Settings, run: dict, last: tuple | None, failed: str | None
) -> str:
    if is_running(run):
        return f"investigation {run['run_id']} is still running"
    if failed and last:
        resume = datetime.fromtimestamp(last[0] + AUTO_COOLDOWN_S)
        return f"last auto-run failed ({failed[:80]}); paused until {resume:%H:%M:%S}"
    at = datetime.fromtimestamp(last[0]) if last else None
    return (
        f"same unhealthy services as the auto-run at {at:%H:%M:%S}, within "
        f"{AUTO_COOLDOWN_S // 60} min"
        if at
        else "auto-run not allowed"
    )


def auto_detect(settings: Settings, rows: list[dict]) -> None:
    shared = auto_state()
    unhealthy = [r for r in rows if r["unhealthy"]]
    services = frozenset(r["service"] for r in unhealthy)
    with shared["lock"]:
        shared["unhealthy"] = services
        if not unhealthy:
            st.success("System healthy")
            checked = st.session_state.get("health_at")
            if checked != st.session_state.get("healthy_counted_at"):
                st.session_state.healthy_counted_at = checked
                st.session_state.healthy_checks = (
                    st.session_state.get("healthy_checks", 0) + 1
                )
            # Two checks in a row, so one quiet window mid-incident is not a reset.
            if st.session_state.healthy_checks >= 2 and shared.get("last"):
                shared["last"] = None
                shared["healthy_since"] = time.time()
            return
        st.session_state.healthy_checks = 0
        run = current_run()
        last = shared.get("last")
        failed = None if is_running(run) else llm_failure(settings, run.get("run_id"))
        now_s = time.time()
        if is_running(run) or not should_auto_run(
            services, last, now_s, failed=bool(failed)
        ):
            if last:
                shared["last"] = (last[0], last[1] | services)
            new = [r for r in unhealthy if r["service"] not in run.get("services", ())]
            if is_running(run) and not new:
                if run.get("trigger") == "auto":
                    st.error(f"ALERT: {run['signal']} — starting investigation")
                return
            banner = alert_signal(primary(new or unhealthy))[0]
            lead = "New anomaly" if new else "Anomaly still active"
            st.warning(
                f"{lead}: {banner} — auto-run suppressed "
                f"({suppressed_reason(settings, run, last, failed)}). "
                "Click Investigate to run now."
            )
            return
        target = primary(unhealthy)
        banner, description, signal = alert_signal(target)
        others = sorted(services - {target["service"]})
        if others:
            description += f"; also unhealthy: {', '.join(others)}"
        shared["last"] = (now_s, services)
    st.error(f"ALERT: {banner} — starting investigation")
    start_run(
        settings,
        description,
        target["service"],
        auto=(signal, banner, services),
    )


def health_panel(settings: Settings, auto: bool, interval: int) -> None:
    st.subheader("System health")
    if st.button("Refresh") or (
        auto and time.time() - st.session_state.get("health_at", 0) >= interval - 2
    ):
        refresh_health(settings)
    if "health" not in st.session_state:
        st.caption("Press Refresh to check containers, errors, latency and traffic.")
        return
    containers, metrics, offset, error = st.session_state.health
    st.caption(
        f"Now = last {HEALTH_WINDOW}; base = same window {offset} min ago."
        if offset
        else "No baseline yet (Prometheus history too short): no regressions marked."
    )
    if not containers["ok"]:
        st.warning(f"container_health: {containers['error']}")
    if error:
        st.warning(f"metrics: {error}")
    rows = health_rows(containers, metrics, ERROR_FLOOR)
    if not rows:
        st.info("No container or metric data.")
        return
    if auto:
        auto_detect(settings, rows)
    else:
        auto_state()["unhealthy"] = frozenset(
            r["service"] for r in rows if r["unhealthy"]
        )
    df = pd.DataFrame(
        [
            {
                "service": r["service"] + (" (client)" if r["client"] else ""),
                "status": r["status"],
                "errors now / base": _pair(r, "error_rate", _fmt_rate),
                "p95 ms now / base": _pair(r, "latency_p95", _fmt_ms),
                "rps now / base": _pair(r, "rps", _fmt_rps),
                "regression": ", ".join(r["regressions"]),
            }
            for r in rows
        ]
    )

    def style(line: pd.Series) -> list[str]:
        r = rows[line.name]
        if r["client"]:
            css = "color: gray"
        elif r["unhealthy"]:
            css = "background-color: rgba(220, 38, 38, 0.25)"
        else:
            css = ""
        return [css] * len(line)

    st.dataframe(df.style.apply(style, axis=1), hide_index=True, width="stretch")
    st.caption(f"checked {datetime.fromtimestamp(st.session_state.health_at):%H:%M:%S}")
    # On a timer tick only the fragment reruns: redraw the run panel if its
    # "previous run" label is now out of date.
    if not st.session_state.get("in_full_run"):
        stale = is_stale(settings, st.session_state.get("active_run_id"))
        if stale != st.session_state.get("rendered_stale", stale):
            st.rerun(scope="app")


def start_run(
    settings: Settings,
    description: str,
    service: str | None,
    auto: tuple[str, str, frozenset] | None = None,
) -> None:
    """Start a run in the background and make it the current run in every tab.

    `auto` is (signal, banner, unhealthy services) when auto-detect started it.
    """
    run_id = f"{datetime.now(UTC):%Y%m%d_%H%M%S}_ui{uuid.uuid4().hex[:4]}"
    runlog = RunLog(run_id=run_id, runs_dir=settings.RUNS_DIR)
    alert = _manual_alert(service, description)
    shared = auto_state()
    services = shared.get("unhealthy") or frozenset()
    if auto:
        signal, _, services = auto
        alert.update(source="detector", signal=ALERT_SIGNALS.get(signal, "manual"))

    def work() -> None:
        diagnosis = None
        try:
            diagnosis = investigate_and_diagnose(alert, runlog=runlog)["diagnosis"]
        except Exception as exc:  # noqa: BLE001
            runlog.log("error", where="ui", error=f"{type(exc).__name__}: {exc}")
        report_run(alert, diagnosis, runlog)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    with shared["lock"]:
        shared["current"] = {
            "run_id": run_id,
            "thread": thread,
            "trigger": "auto" if auto else "manual",
            "signal": auto[1] if auto else description,
            "services": services,
        }
        if not auto and services:
            shared["last"] = (time.time(), services)
    st.session_state.active_run_id = run_id
    st.session_state.followed_run_id = run_id
    st.rerun(scope="app")


def render_alert_form(settings: Settings, services: list[str], running: bool) -> None:
    st.subheader("Alert")
    description = st.text_input("What is wrong?", DEFAULT_ALERT)
    options = [ANY_SERVICE, *sorted(s for s in services if s not in EXCLUDED_SERVICES)]
    service = st.selectbox("Service (optional)", options)
    if st.button("Investigate", type="primary", disabled=running):
        start_run(settings, description, None if service == ANY_SERVICE else service)


def friendly_note(note: str) -> str:
    """'evidence from one tool only: cap 0.55' -> 'capped at 55%: evidence from one tool only'."""
    why, _, how = note.rpartition(": ")
    if not why:
        if " -> " in note and note.startswith("decision "):
            before, after = note.removeprefix("decision ").split(" -> ", 1)
            return f"decision lowered: {before} → {after}"
        return note
    if how.startswith("cap "):
        try:
            return f"capped at {float(how[4:]):.0%}: {why}"
        except ValueError:
            return note
    if re.fullmatch(r"[+-]\d+(\.\d+)?", how):
        return f"{float(how):+.0%}: {why}"
    return f"{why}: {how}"


def evidence_source(eid: str) -> tuple[str, str]:
    return EVIDENCE_LABELS.get(eid.split(":", 1)[0], ("🔎", "Evidence"))


def _sentence(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def run_meta(runlog: RunLog) -> dict:
    """Start time and trigger of a run, from its alert event."""
    alerts = runlog.events("alert")
    alert = (alerts[0].get("alert") or {}) if alerts else {}
    ts = alert.get("ts") or (alerts[0].get("ts") if alerts else None)
    try:
        started = datetime.fromisoformat(ts).astimezone() if ts else None
    except ValueError:
        started = None
    auto = alert.get("source") == "detector"
    return {
        "started": started,
        "trigger": "auto" if auto else "manual",
        "signal": (alert.get("description") or "").split("; also unhealthy")[0],
    }


def is_stale(settings: Settings, run_id: str | None) -> bool:
    """The shown run may not cover the current incident: something unhealthy now
    that the run was not about, or the system recovered after the run started."""
    if not run_id:
        return False
    shared = auto_state()
    run = current_run()
    if run.get("run_id") == run_id and is_running(run):
        return False
    unhealthy = shared.get("unhealthy") or frozenset()
    if not unhealthy:
        return False
    covered = run.get("services", ()) if run.get("run_id") == run_id else ()
    started = run_meta(RunLog(run_id=run_id, runs_dir=settings.RUNS_DIR))["started"]
    healthy_since = shared.get("healthy_since")
    recovered_after = bool(
        started and healthy_since and healthy_since > started.timestamp()
    )
    return recovered_after or bool(unhealthy - set(covered))


def render_run_header(settings: Settings, runlog: RunLog, running: bool) -> None:
    meta = run_meta(runlog)
    started = f"{meta['started']:%H:%M:%S}" if meta["started"] else "?"
    stale = not running and is_stale(settings, runlog.run_id)
    st.session_state.rendered_stale = stale
    if stale:
        st.warning(f"Previous run ({started}) — may not reflect the current incident")
    trigger = (
        f"auto ({_sentence(meta['signal'], 90)})"
        if meta["trigger"] == "auto"
        else "manual"
    )
    tokens = runlog.token_usage()["total_tokens"]
    st.markdown(
        f"Showing run `{runlog.run_id}` · started {started} · trigger: {trigger}"
    )
    st.caption(f"{'running…' if running else 'done'} · tokens: {tokens}")


def render_steps(events: list[dict]) -> None:
    for s in tool_steps(events):
        with st.container(border=True):
            st.markdown(
                f"**Step {s['step']}** · `{s['name']}` `{json.dumps(s['args'])}`"
            )
            result = s["result"]
            if result is None:
                st.caption("running…")
                continue
            st.text(result.get("summary") or f"error: {result.get('error')}")
            with st.popover("Raw ToolResult"):
                st.json(result, expanded=True)
    for e in events:
        if e["event"] == "error":
            st.error(
                f"{e.get('where', 'error')}: {e.get('error') or e.get('problems')}"
            )


def _md(text: str) -> str:
    """Escape Markdown so evidence text renders literally."""
    # Backslash-escape every Markdown control character.
    return re.sub(r"([\\`*_\[\]<>$~#|])", r"\\\1", text)


def render_summary(runlog: RunLog, d: dict, events: list[dict]) -> dict:
    """The incident summary card (oncall/report/summary.py); returns the proposal."""
    review, proposal, verification = _planner_data(events)
    s = summary.build_summary(
        d,
        events,
        runlog.evidence_index(),
        proposal=proposal,
        approval=_read_approval(load_settings(), runlog.run_id),
        verification=verification,
    )
    badge, meaning = DECISION_BADGES.get(s["decision"], DECISION_BADGES["escalate"])
    colour = "gray" if s["tone"] == "gray" else "red"
    icon = "⚪" if s["tone"] == "gray" else "🔴"
    with st.container(border=True):
        st.caption("INCIDENT SUMMARY")
        parts = [
            f":{colour}[**{icon} {_md(s['headline'])}**]",
            f"{badge} ({s['confidence']:.0%})",
        ]
        if s["times"]:
            parts.append(s["times"])
        st.markdown("### " + "  ·  ".join(parts), help=meaning)
        for label, lines in summary.rows(s):
            left, right = st.columns([1, 4])
            left.markdown(f"**{label.upper()}**")
            if label == "Why we think so":
                for w in s["why"]:
                    with right.expander(f"[{w['source']}] {_md(w['text'])}"):
                        st.code(w["id"], language=None)
                        st.text(w["full"])
            else:
                right.markdown("  \n".join(_md(line) for line in lines))
    return {"review": review, "proposal": proposal, "verification": verification}


def render_details(
    settings: Settings,
    runlog: RunLog,
    d: dict | None,
    events: list[dict],
    running: bool,
) -> None:
    finishes = [e for e in events if e["event"] == "finish"]
    reports = [e for e in events if e.get("stage") == "report"]
    if d is not None:
        reasoning, notes = summary.confidence_notes(d.get("reasoning", ""))
        with st.expander("Full agent narrative"):
            if finishes:
                st.markdown("**Symptom summary**")
                st.text(finishes[-1].get("summary") or "(no summary)")
            st.markdown("**Cause**")
            st.text(d.get("cause", ""))
            st.markdown("**Reasoning**")
            st.text(reasoning)
        with st.expander("Confidence and scope"):
            for n in notes:
                st.markdown(f"- {_md(friendly_note(n))}")
            if not notes:
                st.caption("No confidence adjustments.")
            chips = [f":red-background[{d['component']}]"] if d.get("component") else []
            chips += [
                f":orange-background[{x}]"
                for x in d.get("affected_services") or []
                if x != d.get("component")
            ]
            st.markdown("Affected: " + (" ".join(chips) or "none identified"))
        alternatives = d.get("alternatives") or []
        with st.expander(f"Alternatives considered ({len(alternatives)})"):
            if not alternatives:
                st.caption("None.")
            for a in alternatives:
                st.markdown(
                    f"- **{a.get('hypothesis')}**: rejected because "
                    f"{a.get('rejected_because')}"
                )
        index = runlog.evidence_index()
        ids = d.get("evidence_ids") or []
        with st.expander(f"All evidence ({len(ids)})"):
            if not ids:
                st.caption("No evidence cited.")
            for eid in ids:
                icon, label = evidence_source(eid)
                st.markdown(f"{icon} **{label}** `{eid}`")
                item = index.get(eid)
                st.text(item.get("text", "") if item else "Not in this run's evidence.")
    steps = tool_steps(events)
    with st.expander(f"Investigation steps ({len(steps)})", expanded=running):
        render_steps(events)
    review, proposal, verification = _planner_data(events)
    with st.expander("Agent stages and planner review"):
        _render_stage_strip(events)
        _render_planner(review, proposal)
    with st.expander("Action log and verification"):
        _render_action_log(settings, runlog.run_id)
        _render_verification(verification)
    if reports:
        json_path, md_path = (
            Path(reports[-1]["json_path"]),
            Path(reports[-1]["md_path"]),
        )
        with st.expander("Raw report JSON"):
            if json_path.is_file():
                st.json(json.loads(json_path.read_text(encoding="utf-8")))
            else:
                st.caption(f"{json_path} not found.")
        cols = st.columns(2)
        for col, path, mime in (
            (cols[0], json_path, "application/json"),
            (cols[1], md_path, "text/markdown"),
        ):
            if path.is_file():
                col.download_button(
                    f"Download {path.name}",
                    path.read_bytes(),
                    file_name=path.name,
                    mime=mime,
                    key=f"dl-{path.name}",
                    width="stretch",
                )


def handoff_stages(events: list[dict]) -> dict[str, bool]:
    """Return which agent stages have appeared in a hand-off event."""
    seen: set[str] = set()
    for event in events:
        if event.get("event") != "handoff":
            continue
        for field in ("from_agent", "to_agent", "from", "to"):
            value = event.get(field)
            if isinstance(value, str):
                seen.add(value.strip().lower().replace(" ", "_"))
    return {name: bool(aliases & seen) for name, aliases in AGENT_STAGES}


def _planner_data(events: list[dict]) -> tuple[dict | None, dict | None, dict | None]:
    """Find the latest review, proposal, and verification in run events."""
    review = proposal = verification = None
    for event in reversed(events):
        plan = event.get("plan")
        execution = event.get("execution")
        if not isinstance(plan, dict) and isinstance(execution, dict):
            plan = execution.get("plan")
        if isinstance(plan, dict):
            if review is None and isinstance(plan.get("review"), dict):
                review = plan["review"]
            if proposal is None and isinstance(plan.get("proposal"), dict):
                proposal = plan["proposal"]
        if review is None and isinstance(event.get("review"), dict):
            review = event["review"]
        if proposal is None and isinstance(event.get("proposal"), dict):
            proposal = event["proposal"]
        if verification is None and isinstance(event.get("verification"), dict):
            verification = event["verification"]
        if verification is None and isinstance(execution, dict):
            value = execution.get("verification")
            if isinstance(value, dict):
                verification = value
        if review is not None and proposal is not None and verification is not None:
            break
    return review, proposal, verification


def _approval_path(settings: Settings, run_id: str) -> Path:
    if not run_id or re.fullmatch(r"[A-Za-z0-9_-]+", run_id) is None:
        raise ValueError("Invalid run id for approval path")
    return Path(settings.RUNS_DIR) / "approvals" / f"{run_id}.json"


def write_approval(
    settings: Settings,
    run_id: str,
    approved: bool,
    by: str,
    comment: str = "",
    dry_run: bool = True,
) -> Path:
    """Write a human decision and requested execution mode for a run."""
    approver = by.strip()
    if not approver:
        raise ValueError("Enter the name of the person making this decision")
    path = _approval_path(settings, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "approved": bool(approved),
        "by": approver,
        "ts": datetime.now(UTC).isoformat(),
        "comment": comment.strip(),
        "dry_run": bool(dry_run),
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _read_approval(settings: Settings, run_id: str) -> dict | None:
    path = _approval_path(settings, run_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _set_kill_switch(path: Path, enabled: bool) -> None:
    """Apply an explicit UI toggle to the configured kill-switch file."""
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    else:
        path.unlink(missing_ok=True)


def _kill_switch_changed(path: str, state_key: str) -> None:
    """Persist the user's kill-switch toggle without changing it on reruns."""
    try:
        _set_kill_switch(Path(path), bool(st.session_state.get(state_key)))
    except OSError as exc:
        st.session_state[f"{state_key}-error"] = str(exc)


def action_records(settings: Settings, run_id: str) -> list[dict]:
    """Read only the requested run's action records and remove flag details."""
    records = read_action_log(Path(settings.RUNS_DIR) / "actions.jsonl", run_id)
    return [scrub_flags(record) for record in records]


def _render_stage_strip(events: list[dict]) -> None:
    stages = handoff_stages(events)
    columns = st.columns(len(AGENT_STAGES))
    for column, (name, _) in zip(columns, AGENT_STAGES, strict=True):
        column.markdown(f"{'🟢' if stages[name] else '⚪'} **{name}**")


def _render_planner(review: dict | None, proposal: dict | None) -> None:
    st.subheader("Planner")
    with st.container(border=True):
        if review is None:
            st.caption("No planner review recorded yet.")
        else:
            st.caption(
                "Confidence adjustment: "
                f"{review.get('confidence_adjustment', 'not recorded')}"
            )
            challenges = review.get("challenges", [])
            if isinstance(challenges, list) and challenges:
                for challenge in challenges:
                    if not isinstance(challenge, dict):
                        continue
                    status = "Ruled out" if challenge.get("ruled_out") else "Unresolved"
                    st.markdown(f"**{status}:** {challenge.get('hypothesis', '')}")
                    if challenge.get("reason"):
                        st.text(challenge["reason"])
                    evidence_ids = challenge.get("evidence_ids") or []
                    if evidence_ids:
                        st.caption("Evidence: " + ", ".join(map(str, evidence_ids)))
            else:
                st.caption("No alternative challenges recorded.")

    st.subheader("Proposal")
    if proposal is None:
        st.caption("No proposal recorded.")
        return
    with st.container(border=True):
        st.markdown(
            f"**{proposal.get('action', 'Action')}** → `{proposal.get('target', '')}`"
        )
        if proposal.get("params"):
            st.json(proposal["params"])
        st.markdown("**Reason**")
        st.text(proposal.get("reason", ""))
        st.markdown("**Expected effect**")
        st.text(proposal.get("expected_effect", ""))
        st.caption(
            f"Risk: {proposal.get('risk', 'unknown')} · "
            f"Rollback: {proposal.get('rollback_hint', 'not recorded')}"
        )


def _render_human_gate(settings: Settings, run_id: str, proposal: dict | None) -> None:
    st.subheader("Human gate")
    kill_path = Path(settings.KILL_SWITCH_FILE)
    kill_key = f"kill-switch-{run_id}"
    if (
        kill_key not in st.session_state
        or st.session_state[kill_key] != kill_path.exists()
    ):
        st.session_state[kill_key] = kill_path.exists()
    st.toggle(
        "Kill switch",
        key=kill_key,
        on_change=_kill_switch_changed,
        args=(str(kill_path), kill_key),
        help="When enabled, the operator must not execute actions.",
    )
    kill_enabled = bool(st.session_state[kill_key])
    kill_error = st.session_state.pop(f"{kill_key}-error", None)
    if kill_error:
        st.error(f"Could not update kill switch: {kill_error}")

    execute_for_real = st.toggle(
        "Execute for real",
        value=False,
        disabled=kill_enabled,
        key=f"execute-for-real-{run_id}",
        help="Off by default; approval records dry_run=true.",
    )
    dry_run = not execute_for_real
    st.caption("Dry-run mode" if dry_run else "Real execution requested")
    approval = _read_approval(settings, run_id)
    if approval:
        verdict = "Approved" if approval.get("approved") else "Rejected"
        mode = "dry run" if approval.get("dry_run", True) else "real execution"
        st.info(f"{verdict} by {approval.get('by', 'unknown')} · {mode}")

    approver = st.text_input("Your name", key=f"approver-{run_id}")
    comment = st.text_input("Approval note (optional)", key=f"approval-note-{run_id}")
    left, right = st.columns(2)
    can_decide = proposal is not None and bool(approver.strip())
    if left.button(
        "Approve",
        disabled=not can_decide or kill_enabled,
        width="stretch",
        key=f"approve-{run_id}",
    ):
        path = write_approval(settings, run_id, True, approver, comment, dry_run)
        st.success(f"Approval saved to {path}")
    if right.button(
        "Reject",
        disabled=not can_decide,
        width="stretch",
        key=f"reject-{run_id}",
    ):
        path = write_approval(settings, run_id, False, approver, comment, dry_run)
        st.success(f"Rejection saved to {path}")
    if proposal is None:
        st.caption("A proposal is required before it can be approved or rejected.")
    if kill_enabled:
        st.warning("Kill switch is on; real execution is disabled.")


def _render_action_log(settings: Settings, run_id: str) -> None:
    st.subheader("Action log")
    try:
        records = action_records(settings, run_id)
    except (OSError, json.JSONDecodeError) as exc:
        st.warning(f"Could not read action log: {exc}")
        return
    if records:
        st.json(records)
    else:
        st.caption("No action records for this run.")


def _render_verification(verification: dict | None) -> None:
    st.subheader("Verification")
    if verification is None:
        st.caption("No verification results recorded yet.")
    else:
        st.json(verification)


def render_run(settings: Settings, run_id: str, running: bool) -> None:
    runlog = RunLog(run_id=run_id, runs_dir=settings.RUNS_DIR)
    events = [scrub_flags(e) for e in runlog.events()]
    st.subheader("Investigation")
    render_run_header(settings, runlog, running)

    finishes = [e for e in events if e["event"] == "finish"]
    decisions = [e for e in events if e["event"] == "decision" and "diagnosis" in e]
    diagnosis = decisions[-1]["diagnosis"] if decisions else None
    if diagnosis is not None:
        plan = render_summary(runlog, diagnosis, events)
        if plan["proposal"] is not None:
            _render_human_gate(settings, run_id, plan["proposal"])
    elif finishes:
        st.caption(
            "Waiting for the diagnosis…" if running else "No diagnosis in this run."
        )
    render_details(settings, runlog, diagnosis, events, running)


@st.fragment(run_every=1)
def live_view(settings: Settings, run_id: str, thread: threading.Thread) -> None:
    running = thread.is_alive()
    render_run(settings, run_id, running)
    if not running:
        st.rerun()


def render_auto_controls() -> tuple[bool, int]:
    st.sidebar.header(AUTO_LABEL)
    auto = st.sidebar.toggle("Auto-detect", value=True)
    interval = int(
        st.sidebar.number_input(
            "Interval (s)", min_value=5, value=AUTO_INTERVAL_S, step=5
        )
    )
    last = auto_state().get("last")
    st.sidebar.caption(
        f"last auto-run: {datetime.fromtimestamp(last[0]):%H:%M:%S}"
        if last
        else "last auto-run: never"
    )
    return auto, interval


def render_sidebar(settings: Settings) -> None:
    st.sidebar.header("Past runs")
    runs = list_runs(settings.RUNS_DIR)
    if not runs:
        st.sidebar.caption("No runs yet.")
    for path in runs[:MAX_PAST_RUNS]:
        if st.sidebar.button(run_label(path), key=f"run-{path.stem}", width="stretch"):
            st.session_state.active_run_id = path.stem


def main() -> None:
    st.session_state.in_full_run = True
    try:
        render_app()
    finally:
        st.session_state.in_full_run = False


def render_app() -> None:
    st.set_page_config(page_title="AI On-Call Engineer", layout="wide")
    settings, endpoints = configure()
    services = render_header(settings, endpoints)
    auto, interval = render_auto_controls()

    run = current_run()
    # A run started anywhere (auto-detect or another tab) takes over the panel.
    if run.get("run_id") and run["run_id"] != st.session_state.get("followed_run_id"):
        st.session_state.followed_run_id = run["run_id"]
        st.session_state.active_run_id = run["run_id"]
    render_sidebar(settings)
    running = is_running(run)
    st.fragment(health_panel, run_every=interval if auto else None)(
        settings, auto, interval
    )
    render_alert_form(settings, services, running)

    run_id = st.session_state.get("active_run_id")
    if not run_id:
        return
    if running and run_id == run.get("run_id"):
        live_view(settings, run_id, run["thread"])
    else:
        render_run(settings, run_id, running=False)


if __name__ == "__main__":
    main()

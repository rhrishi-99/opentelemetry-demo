"""Alert detector (the "Detect" step): start an investigation automatically.

Every poll it checks three signals and raises an Alert when one crosses its
threshold:
  - error rate per service    > max(ALERT_ERROR_RATE, 3 x baseline)
  - p95 latency per service   > max(ALERT_P95_MS, 2 x baseline)
  - container health          down, restarting or OOM-killed
The baseline is each service's median over the 30 minutes before `watch`
starts. Read-only: it only queries Prometheus and uses container_health's
read-only docker helpers.
"""

import json
import logging
import math
import os
import statistics
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from oncall.config import Settings, load_settings
from oncall.models import Alert
from oncall.platform import containers
from oncall.safety.exclusions import EXCLUDED_SERVICES, PROMQL_EXCLUDE
from oncall.tools.backends import BackendError, TelemetryBackend, get_backend
from oncall.tools.metrics import PROMQL

log = logging.getLogger(__name__)

WINDOW = "5m"  # rate window for error rate and p95
BASELINE_MIN = 30  # baseline = median of the 30 minutes before watch starts
ERROR_RATE_FACTOR = 3.0
P95_FACTOR = 2.0
SUPPRESS_MIN = 10  # after an alert, stay quiet this long unless signals recover
# When a run ends with a human holding the incident (escalated, aborted by a
# rejection or the kill switch, or no action found), re-paging every 10 min only
# burns LLM calls on the same evidence. Stay quiet for a shift instead; signals
# recovering or a newly stopped container still break through.
ESCALATED_SUPPRESS_MIN = 480
HUMAN_HAS_IT = ("escalated", "aborted", "no_action")
WATCH_LOG = "watch.jsonl"

# Edge services see every user request (frontend-web is the browser). Their
# errors are a user-facing symptom, not a root cause, so they raise a symptom
# alert with service=None.
EDGE_SERVICES = ("frontend-proxy", "load-generator", "frontend-web")

# Signal order when several alerts fire in one poll: the clearest cause first.
SIGNAL_ORDER = {"container": 0, "error_rate": 1, "latency": 2}

# {"error_rate": {service: median}, "p95_ms": {service: median}}
Baseline = dict[str, dict[str, float]]


def ignored_services() -> set[str]:
    """Services never alerted on: the team's shared exclusion list plus any
    named in ALERT_IGNORE_SERVICES (comma-separated)."""
    raw = os.environ.get("ALERT_IGNORE_SERVICES", "")
    return {s.strip() for s in raw.split(",") if s.strip()} | set(EXCLUDED_SERVICES)


def _query(name: str) -> str:
    """One of get_metrics' PROMQL templates, filled in with our window and the
    shared exclusions (same as get_metrics does)."""
    return PROMQL[name].replace("{window}", WINDOW).replace("{exclude}", PROMQL_EXCLUDE)


def _to_float(raw: object) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def by_service(resp: dict) -> dict[str, float]:
    """Prometheus instant-query result -> {service_name: value}."""
    out = {}
    for item in resp.get("data", {}).get("result", []):
        service = item.get("metric", {}).get("service_name")
        value = _to_float((item.get("value") or [None, None])[1])
        if service and value is not None:
            out[service] = value
    return out


def median_by_service(resp: dict) -> dict[str, float]:
    """Prometheus range-query result -> {service_name: median value}."""
    out = {}
    for item in resp.get("data", {}).get("result", []):
        service = item.get("metric", {}).get("service_name")
        values = [_to_float(raw) for _, raw in item.get("values", [])]
        values = [v for v in values if v is not None]
        if service and values:
            out[service] = statistics.median(values)
    return out


def capture_baseline(backend: TelemetryBackend, now: datetime) -> Baseline:
    """Median error rate and p95 per service over the last BASELINE_MIN minutes."""
    start = str(int((now - timedelta(minutes=BASELINE_MIN)).timestamp()))
    end = str(int(now.timestamp()))
    return {
        "error_rate": median_by_service(
            backend.prom_range(_query("error_rate_all"), start, end, "60s")
        ),
        "p95_ms": median_by_service(
            backend.prom_range(_query("latency_p95_all"), start, end, "60s")
        ),
    }


def _alert(
    now: datetime,
    service: str | None,
    signal: str,
    value: float | None,
    threshold: float | None,
    description: str,
) -> Alert:
    return {
        "id": f"alert-{now:%Y%m%dT%H%M%S}-{service or 'edge'}-{signal}",
        "ts": now.isoformat(timespec="seconds"),
        "service": service,
        "signal": signal,
        "value": value,
        "threshold": threshold,
        "description": description,
        "source": "detector",
    }


def _threshold(fixed: float, factor: float, base: float | None, cap: bool) -> float:
    """max(fixed, factor x baseline).

    With `cap`, a baseline already above the fixed threshold is ignored: the
    service was failing when watch started, and that must not raise the bar
    (it would hide the ongoing fault). Edge services are not capped because
    they are normally noisy (live: load-generator ~28% errors) and their
    errors are symptoms anyway.
    """
    if base is None or (cap and base > fixed):
        return fixed
    return max(fixed, factor * base)


def _metric_alerts(
    backend: TelemetryBackend,
    baseline: Baseline,
    settings: Settings,
    now: datetime,
    ignored: set[str],
) -> list[Alert]:
    alerts = []
    base_errors = baseline.get("error_rate", {})
    errors = by_service(backend.prom_query(_query("error_rate_all")))
    for service, rate in sorted(errors.items()):
        if service in ignored:
            continue
        threshold = _threshold(
            settings.ALERT_ERROR_RATE,
            ERROR_RATE_FACTOR,
            base_errors.get(service),
            cap=service not in EDGE_SERVICES,
        )
        if rate <= threshold:
            continue
        if service in EDGE_SERVICES:
            alerts.append(
                _alert(
                    now,
                    None,
                    "error_rate",
                    rate,
                    threshold,
                    f"User-facing symptom: error rate at {service} is {rate:.1%} "
                    f"over the last {WINDOW} (threshold {threshold:.1%}). The "
                    "failing service behind it is not known yet.",
                )
            )
        else:
            alerts.append(
                _alert(
                    now,
                    service,
                    "error_rate",
                    rate,
                    threshold,
                    f"{service} error rate is {rate:.1%} over the last {WINDOW} "
                    f"(threshold {threshold:.1%}).",
                )
            )

    base_p95 = baseline.get("p95_ms", {})
    p95s = by_service(backend.prom_query(_query("latency_p95_all")))
    for service, p95 in sorted(p95s.items()):
        if service in ignored or service in EDGE_SERVICES:
            continue
        threshold = _threshold(
            settings.ALERT_P95_MS, P95_FACTOR, base_p95.get(service), cap=True
        )
        if p95 > threshold:
            alerts.append(
                _alert(
                    now,
                    service,
                    "latency",
                    p95,
                    threshold,
                    f"{service} p95 latency is {p95:.0f} ms over the last {WINDOW} "
                    f"(threshold {threshold:.0f} ms).",
                )
            )
    return alerts


def _container_alerts(
    settings: Settings, now: datetime, ignored: set[str]
) -> list[Alert]:
    try:
        if settings.TELEMETRY_BACKEND == "offline":
            infos = (containers.load_fixture(settings) or {}).get("inspect", [])
        else:
            ids = containers.compose_container_ids(settings.COMPOSE_PROJECT_NAME)
            infos = containers.inspect_containers(ids)
    except containers.DockerError as exc:
        log.warning("container check skipped: %s", exc)
        return []

    alerts = []
    for info in infos:
        # No `docker stats`: CPU and memory pressure are not alert signals.
        c = containers.describe_container(info, None)
        service = c["service"] or c["name"]
        if service in ignored or service in EDGE_SERVICES:
            continue
        # A failing healthcheck alone is not an alert: on a loaded host the
        # demo's healthchecks time out while the service still works. The
        # agent still sees it through container_health when it investigates.
        found = [
            p
            for p in containers.find_problems(c, now)
            if p != containers.HEALTHCHECK_UNHEALTHY
        ]
        if found:
            alerts.append(
                _alert(
                    now,
                    service,
                    "container",
                    None,
                    None,
                    f"{service} container: {'; '.join(found)}.",
                )
            )
    return alerts


def check_once(
    backend: TelemetryBackend,
    baseline: Baseline,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> list[Alert]:
    """Check every signal once and return all alerts that would fire now.

    No debouncing here; `watch` decides which alerts are actually raised.
    Raises BackendError if Prometheus can't be reached. A Docker failure only
    skips the container check.
    """
    settings = settings or load_settings()
    now = now or datetime.now(UTC)
    ignored = ignored_services()
    return _metric_alerts(backend, baseline, settings, now, ignored) + (
        _container_alerts(settings, now, ignored)
    )


def merge_alerts(alerts: list[Alert]) -> Alert:
    """Turn one poll's alerts into the single alert for this incident.

    The main alert is a named service before an edge symptom, then the
    clearest signal (container > error rate > latency), then the biggest
    overshoot. Every other alert is appended to its description.
    """

    def rank(a: Alert) -> tuple:
        overshoot = (a["value"] or 0.0) / (a["threshold"] or 1.0)
        return (a["service"] is None, SIGNAL_ORDER[a["signal"]], -overshoot)

    main, *others = sorted(alerts, key=rank)
    if others:
        also = " ".join(a["description"] for a in others)
        main = {**main, "description": f"{main['description']} Also seen: {also}"}
    return main


class Debouncer:
    """One alert per incident.

    After an alert fires, stay quiet for SUPPRESS_MIN minutes, or until a check
    comes back clean. While quiet, only a container that newly goes down fires
    again: new error-rate or latency alerts are usually knock-on effects of the
    same incident (live: cart stopped, then frontend errors 30 s later).
    """

    def __init__(self, suppress_min: int = SUPPRESS_MIN) -> None:
        self.suppress = timedelta(minutes=suppress_min)
        self.quiet_until: datetime | None = None
        self.down: set[str | None] = set()  # containers already alerted on
        self.held_by: dict | None = None  # the run a human is handling, if any

    def should_fire(self, alerts: list[Alert], now: datetime) -> bool:
        if not alerts:
            self.quiet_until = None
            self.down = set()
            self.held_by = None
            return False
        down = {a["service"] for a in alerts if a["signal"] == "container"}
        quiet = self.quiet_until is not None and now < self.quiet_until
        if quiet and down <= self.down:
            return False
        self.down = (self.down | down) if quiet else down
        self.quiet_until = now + self.suppress
        self.held_by = None
        return True

    def hold(self, now: datetime, minutes: int, run: dict) -> datetime:
        """Extend the quiet period: a human now owns this incident (see run)."""
        self.quiet_until = now + timedelta(minutes=minutes)
        self.held_by = {**run, "until": self.quiet_until.isoformat(timespec="seconds")}
        return self.quiet_until


def _print_alert(alert: Alert) -> None:
    """Default on_alert when none is given (the CLI passes the agent instead)."""
    print(json.dumps(alert, indent=2))


def _append_log(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def watch(
    poll_s: int | None = None,
    on_alert: Callable[[Alert], object] | None = None,
    once: bool = False,
    backend: TelemetryBackend | None = None,
    settings: Settings | None = None,
    sleep: Callable[[float], object] = time.sleep,
    on_check: Callable[[dict], object] | None = None,
) -> None:
    """Poll every `poll_s` seconds and call `on_alert` once per incident.

    `on_alert` may return the run's Execution (or any dict with a `status`).
    When that status says a human now holds the incident, the quiet period
    becomes ESCALATED_SUPPRESS_MIN. Every check is logged as one line in
    runs/watch.jsonl and also passed to `on_check` (the CLI prints it).
    """
    settings = settings or load_settings()
    poll_s = poll_s or settings.ALERT_POLL_S
    on_alert = on_alert or _print_alert
    backend = backend or get_backend()
    log_path = Path(settings.RUNS_DIR) / WATCH_LOG

    started = datetime.now(UTC)
    try:
        baseline = capture_baseline(backend, started)
    except BackendError as exc:
        log.warning("no baseline, using fixed thresholds only: %s", exc)
        baseline = {}
    _append_log(
        log_path,
        {
            "ts": started.isoformat(timespec="seconds"),
            "event": "baseline",
            "services": sorted(baseline.get("error_rate", {})),
        },
    )

    debouncer = Debouncer()
    while True:
        now = datetime.now(UTC)
        entry: dict = {"ts": now.isoformat(timespec="seconds"), "event": "check"}
        try:
            alerts = check_once(backend, baseline, settings, now)
        except BackendError as exc:
            # Don't touch the debouncer: an outage is not "signals recovered".
            entry["error"] = str(exc)[:300]
        else:
            entry["alerts"] = [
                {"service": a["service"], "signal": a["signal"], "value": a["value"]}
                for a in alerts
            ]
            if debouncer.should_fire(alerts, now):
                alert = merge_alerts(alerts)
                entry["fired"] = alert["id"]
                try:
                    result = on_alert(alert)
                except Exception as exc:  # keep watching even if the agent crashes
                    log.exception("on_alert failed")
                    entry["on_alert_error"] = f"{type(exc).__name__}: {exc}"
                else:
                    status = result.get("status") if isinstance(result, dict) else None
                    if status in HUMAN_HAS_IT:
                        run_id = (result.get("investigation") or {}).get("run_id")
                        until = debouncer.hold(
                            now,
                            ESCALATED_SUPPRESS_MIN,
                            {"run_id": run_id, "status": status},
                        )
                        entry["quiet_until"] = until.isoformat(timespec="seconds")
            elif alerts and debouncer.held_by:
                entry["suppressed"] = debouncer.held_by
        _append_log(log_path, entry)
        if on_check:
            on_check(entry)
        if once:
            return
        sleep(poll_s)

"""Verify recovery after an executed action, then roll back or escalate.

Reads signals only (read-only docker inspect, Jaeger, Prometheus). A rollback is
never executed here: it goes back through the caller's approval gate and
executor, so a human approves it the same way as the original action.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from oncall.agent.taxonomy import CLIENT_SERVICES
from oncall.config import load_settings
from oncall.models import ActionResult, Alert, Diagnosis, Verification, VerifyCheck
from oncall.platform.containers import describe_container, inspect_containers
from oncall.safety.exclusions import is_excluded
from oncall.tools.backends import TelemetryBackend, get_backend
from oncall.tools.metrics import _build_promql, _extract_vector_results
from oncall.tools.traces import _error_details

POLL_S = 15
REQUIRED_OK_POLLS = 3
ERROR_LOOKBACK = "1m"
BEFORE_LOOKBACK = "5m"
MAX_ERROR_TRACES = 50
MAX_ERROR_SPANS_OK = 1
ERROR_DROP_RATIO = 0.2
PROM_WINDOW = "2m"
RPS_BASELINE_OFFSET_S = 30 * 60
RPS_SURGE_FACTOR = 1.5
EDGE_SERVICES = ("frontend-proxy", "frontend")
CONTAINER_ACTIONS = ("start_service", "restart_service")
CONTAINER_CATEGORIES = ("container_down", "dependency_unavailable")
TRAFFIC_ACTIONS = ("throttle_load",)
TRAFFIC_CATEGORIES = ("traffic_surge",)


class RunLogLike(Protocol):
    def log(self, event: str, **data: Any) -> None: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _checkable(service: str | None) -> bool:
    return bool(service) and not is_excluded(service) and service not in CLIENT_SERVICES


def _inspect_one(name: str) -> dict:
    infos = inspect_containers([name])
    if not infos:
        raise RuntimeError(f"container {name} not found")
    return infos[0]


class _Probe:
    """One signal to poll. `read()` returns (ok, value); ok None means no data (skipped)."""

    def __init__(
        self, signal: str, target: str, required: bool, read: Callable, before=None
    ):
        self.signal = signal
        self.target = target
        self.required = required
        self.read = read
        self.before = before


def _container_probe(target: str, action_result: ActionResult, inspect) -> _Probe:
    def read() -> tuple[bool, str]:
        c = describe_container(inspect(target), None)
        ok = c["state"] == "running" and c["health"] != "unhealthy"
        return ok, f"state={c['state']} health={c['health']}"

    status = (action_result.get("before") or {}).get("status")
    return _Probe(
        "container", target, True, read, f"state={status}" if status else None
    )


def _error_spans(backend: TelemetryBackend, service: str, lookback: str) -> int:
    traces = backend.jaeger_traces(
        service=service,
        lookback=lookback,
        limit=MAX_ERROR_TRACES,
        tags={"error": "true"},
    )
    return sum(
        1
        for trace in traces[:MAX_ERROR_TRACES]
        for span in trace.get("spans", [])
        if span.get("service") == service and _error_details(span)[0]
    )


def _error_probe(service: str, backend: TelemetryBackend, notes: list[str]) -> _Probe:
    try:
        before: int | None = _error_spans(backend, service, BEFORE_LOOKBACK)
    except Exception as exc:  # noqa: BLE001 - verify never raises
        before = None
        notes.append(f"error_spans {service}: before unavailable ({exc})")

    def read() -> tuple[bool, str]:
        count = _error_spans(backend, service, ERROR_LOOKBACK)
        limit = max(MAX_ERROR_SPANS_OK, ERROR_DROP_RATIO * (before or 0))
        return count <= limit, f"{count} error spans in last {ERROR_LOOKBACK}"

    before_text = (
        None if before is None else f"{before} error spans in last {BEFORE_LOOKBACK}"
    )
    return _Probe("error_spans", service, True, read, before_text)


def _prom_value(
    backend: TelemetryBackend, promql: str, service: str, at=None
) -> float | None:
    return _extract_vector_results(backend.prom_query(promql, at=at), service).get(
        service
    )


def _rps_probe(service: str, backend: TelemetryBackend) -> _Probe:
    promql = _build_promql("rps", service, PROM_WINDOW)

    def read() -> tuple[bool | None, str]:
        at = str(int(time.time() - RPS_BASELINE_OFFSET_S))
        now = _prom_value(backend, promql, service)
        base = _prom_value(backend, promql, service, at=at)
        if now is None or not base:
            return None, f"rps {service}: no data, check skipped"
        return now < RPS_SURGE_FACTOR * base, f"{now:.2f} rps (baseline {base:.2f})"

    return _Probe("rps", service, True, read)


def _error_rate_probe(service: str, backend: TelemetryBackend) -> _Probe:
    promql = _build_promql("error_rate", service, PROM_WINDOW)

    def read() -> tuple[bool | None, str]:
        value = _prom_value(backend, promql, service)
        if value is None:
            return None, f"error_rate {service}: no data, check skipped"
        return value <= load_settings().ALERT_ERROR_RATE, f"error rate {value:.3f}"

    return _Probe("error_rate (info)", service, False, read)


def _probes(
    alert: Alert,
    diagnosis: Diagnosis,
    action_result: ActionResult,
    backend: TelemetryBackend,
    inspect,
    notes: list[str],
) -> list[_Probe]:
    request = action_result.get("request") or {}
    action = request.get("action", "")
    category = diagnosis.get("category", "")
    component = diagnosis.get("component")
    probes: list[_Probe] = []

    if action in CONTAINER_ACTIONS or category in CONTAINER_CATEGORIES:
        target = request.get("target") if action in CONTAINER_ACTIONS else component
        if _checkable(target):
            probes.append(_container_probe(target, action_result, inspect))

    services = [
        s for s in dict.fromkeys([component, alert.get("service")]) if _checkable(s)
    ]
    if _checkable(component):
        for service in services:
            probes.append(_error_probe(service, backend, notes))

    if action in TRAFFIC_ACTIONS or category in TRAFFIC_CATEGORIES:
        probes.extend(_rps_probe(s, backend) for s in EDGE_SERVICES)

    if _checkable(component):
        probes.append(_error_rate_probe(component, backend))
    return probes


def _run(probe: _Probe, notes: list[str]) -> tuple[VerifyCheck | None, bool | None]:
    """Read one probe; the check is None when it had no data this poll."""
    try:
        ok, value = probe.read()
    except Exception as exc:  # noqa: BLE001 - a tool error fails the check, never raises
        ok, value = False, f"error: {exc}"
        notes.append(f"{probe.signal} {probe.target}: {exc}")
    if ok is None:
        notes.append(value)
        return None, None
    check: VerifyCheck = {
        "signal": probe.signal,
        "target": probe.target,
        "before": probe.before,
        "after": value,
        "ok": bool(ok),
        "ts": _now(),
    }
    return check, bool(ok)


def _result(
    recovered: bool,
    checks: list[VerifyCheck],
    polls: int,
    start: float,
    notes: list[str],
) -> Verification:
    return {
        "recovered": recovered,
        "checks": checks,
        "polls": polls,
        "duration_s": round(time.monotonic() - start, 1),
        "rolled_back": False,
        "rollback_result": None,
        "outcome": "recovered" if recovered else "not_recovered_escalated",
        "notes": "; ".join(dict.fromkeys(n for n in notes if n)),
    }


def _poll(
    alert: Alert,
    diagnosis: Diagnosis,
    action_result: ActionResult,
    *,
    timeout_s: float | None = None,
    poll_s: float = POLL_S,
    required_ok_polls: int = REQUIRED_OK_POLLS,
    sleep: Callable[[float], None] = time.sleep,
    backend: TelemetryBackend | None = None,
    inspect: Callable[[str], dict] | None = None,
    runlog: RunLogLike | None = None,
) -> Verification:
    start = time.monotonic()
    notes: list[str] = []
    if action_result.get("dry_run") or not action_result.get("executed"):
        result = _result(
            False, [], 0, start, ["action not executed (dry run or denied)"]
        )
        result["outcome"] = "skipped_dry_run"
        return result

    if timeout_s is None:
        timeout_s = load_settings().VERIFY_WINDOW_S
    try:
        probes = _probes(
            alert,
            diagnosis,
            action_result,
            backend or get_backend(),
            inspect or _inspect_one,
            notes,
        )
    except Exception as exc:  # noqa: BLE001 - verify never raises
        probes = []
        notes.append(f"could not set up checks: {exc}")
    if not any(p.required for p in probes):
        notes.append("no applicable required checks")

    max_polls = max(required_ok_polls, int(timeout_s // max(poll_s, 1e-9)) + 1)
    streak = polls = 0
    checks: list[VerifyCheck] = []
    while polls < max_polls:
        polls += 1
        checks, required_ok = [], []
        for probe in probes:
            check, ok = _run(probe, notes)
            if check:
                checks.append(check)
                if probe.required:
                    required_ok.append(ok)
        all_ok = bool(required_ok) and all(required_ok)
        streak = streak + 1 if all_ok else 0
        if runlog:
            runlog.log(
                "verify_poll", poll=polls, ok=all_ok, streak=streak, checks=checks
            )
        if streak >= required_ok_polls:
            return _result(True, checks, polls, start, notes)
        if polls < max_polls:
            sleep(poll_s)
    notes.append(f"not recovered within {timeout_s:.0f}s")
    return _result(False, checks, polls, start, notes)


def verify(
    alert: Alert,
    diagnosis: Diagnosis,
    action_result: ActionResult,
    *,
    timeout_s: float | None = None,
    poll_s: float = POLL_S,
    required_ok_polls: int = REQUIRED_OK_POLLS,
    sleep: Callable[[float], None] = time.sleep,
    backend: TelemetryBackend | None = None,
    inspect: Callable[[str], dict] | None = None,
    runlog: RunLogLike | None = None,
) -> Verification:
    """Poll the relevant signals until they are healthy for required_ok_polls polls, or timeout.

    timeout_s defaults to Settings.VERIFY_WINDOW_S.
    """
    result = _poll(
        alert,
        diagnosis,
        action_result,
        timeout_s=timeout_s,
        poll_s=poll_s,
        required_ok_polls=required_ok_polls,
        sleep=sleep,
        backend=backend,
        inspect=inspect,
        runlog=runlog,
    )
    if runlog:
        runlog.log("verification", **result)
    return result


def verify_and_maybe_rollback(
    alert: Alert,
    diagnosis: Diagnosis,
    action_result: ActionResult,
    *,
    approve_rollback: Callable[[dict], bool],
    execute_rollback: Callable[[dict], dict],
    **kw: Any,
) -> Verification:
    """verify(); if not recovered, offer the action's rollback to the human gate, else escalate."""
    runlog = kw.get("runlog")
    result = _poll(alert, diagnosis, action_result, **kw)
    rollback = action_result.get("rollback")
    if result["outcome"] == "not_recovered_escalated":
        notes = [result["notes"]]
        if not rollback:
            notes.append("no rollback available; escalating")
        else:
            try:
                approved = bool(approve_rollback(rollback))
            except Exception as exc:  # noqa: BLE001 - verify never raises
                approved = False
                notes.append(f"rollback approval failed: {exc}")
            if not approved:
                notes.append("rollback not approved; escalating")
            else:
                try:
                    rollback_result = execute_rollback(rollback)
                except Exception as exc:  # noqa: BLE001 - verify never raises
                    rollback_result = {"ok": False, "detail": str(exc)}
                result["rollback_result"] = rollback_result
                if rollback_result.get("ok"):
                    result["rolled_back"] = True
                    result["outcome"] = "not_recovered_rolled_back"
                else:
                    notes.append(f"rollback failed: {rollback_result.get('detail')}")
        result["notes"] = "; ".join(n for n in notes if n)
    if runlog:
        runlog.log("verification", **result)
    return result

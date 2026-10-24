"""Tests for F24 verify recovery + rollback + escalate (stubbed signals, no sleeping)."""

import re
from pathlib import Path
from unittest import mock

from oncall.safety import verify as verify_mod
from oncall.safety.verify import verify, verify_and_maybe_rollback

ALERT = {
    "id": "a1",
    "ts": "2026-10-24T10:00:00Z",
    "service": "cart",
    "signal": "container",
    "value": None,
    "threshold": None,
    "description": "valkey-cart down",
    "source": "detector",
}
DIAGNOSIS = {
    "component": "valkey-cart",
    "category": "container_down",
    "cause": "valkey-cart exited",
    "confidence": 0.9,
    "evidence_ids": [],
    "affected_services": ["cart"],
    "alternatives": [],
    "decision": "act",
    "reasoning": "",
}
ROLLBACK = {
    "action": "throttle_load",
    "target": "load-generator",
    "params": {"users": 10},
    "reason": "rollback",
    "run_id": "r1",
    "dry_run": False,
}


def action_result(executed=True, dry_run=False, rollback=None):
    return {
        "request": {
            "action": "start_service",
            "target": "valkey-cart",
            "params": {},
            "reason": "",
            "run_id": "r1",
            "dry_run": dry_run,
        },
        "allowed": True,
        "executed": executed,
        "dry_run": dry_run,
        "ok": True,
        "detail": "",
        "before": {"status": "exited", "running": False},
        "after": {},
        "rollback": rollback,
        "ts": "2026-10-24T10:01:00Z",
    }


class FakeBackend:
    """No error spans, no Prometheus data."""

    def __init__(self):
        self.calls = 0

    def jaeger_traces(self, service, lookback="15m", limit=20, tags=None):
        self.calls += 1
        return []

    def prom_query(self, promql, at=None):
        return {"status": "success", "data": {"resultType": "vector", "result": []}}


def inspector(states):
    """Fake docker inspect returning the next state on each call (last one repeats)."""
    states = list(states)

    def inspect(name):
        status = states.pop(0) if len(states) > 1 else states[0]
        return {"Name": f"/{name}", "State": {"Status": status}}

    return inspect


def run(states, **kw):
    kw.setdefault("timeout_s", 60)
    return dict(
        sleep=lambda s: None,
        backend=FakeBackend(),
        inspect=inspector(states),
        poll_s=15,
        **kw,
    )


def test_container_comes_back_on_poll_2_recovers_after_3_ok_polls():
    result = verify(ALERT, DIAGNOSIS, action_result(), **run(["exited", "running"]))
    assert result["recovered"] is True
    assert result["outcome"] == "recovered"
    assert result["polls"] == 4
    container = next(c for c in result["checks"] if c["signal"] == "container")
    assert container == {**container, "before": "state=exited", "ok": True}
    assert {c["target"] for c in result["checks"] if c["signal"] == "error_spans"} == {
        "valkey-cart",
        "cart",
    }


def test_never_recovers_rollback_approved_is_executed_once():
    execute = mock.Mock(return_value={"ok": True, "executed": True})
    result = verify_and_maybe_rollback(
        ALERT,
        DIAGNOSIS,
        action_result(rollback=ROLLBACK),
        approve_rollback=lambda req: True,
        execute_rollback=execute,
        **run(["exited"]),
    )
    execute.assert_called_once_with(ROLLBACK)
    assert result["outcome"] == "not_recovered_rolled_back"
    assert result["rolled_back"] is True
    assert result["recovered"] is False
    assert result["polls"] == 5


def test_never_recovers_rollback_denied_escalates_without_executing():
    execute = mock.Mock()
    result = verify_and_maybe_rollback(
        ALERT,
        DIAGNOSIS,
        action_result(rollback=ROLLBACK),
        approve_rollback=lambda req: False,
        execute_rollback=execute,
        **run(["exited"]),
    )
    execute.assert_not_called()
    assert result["outcome"] == "not_recovered_escalated"
    assert result["rolled_back"] is False


def test_no_rollback_available_escalates():
    approve = mock.Mock()
    result = verify_and_maybe_rollback(
        ALERT,
        DIAGNOSIS,
        action_result(),
        approve_rollback=approve,
        execute_rollback=mock.Mock(),
        **run(["exited"]),
    )
    approve.assert_not_called()
    assert result["outcome"] == "not_recovered_escalated"


def test_dry_run_is_skipped_without_polling():
    kw = run(["running"])
    result = verify(ALERT, DIAGNOSIS, action_result(executed=False, dry_run=True), **kw)
    assert result["outcome"] == "skipped_dry_run"
    assert result["polls"] == 0
    assert kw["backend"].calls == 0


def test_tool_error_fails_check_without_raising():
    def broken(name):
        raise RuntimeError("docker not running")

    kw = run(["running"])
    kw["inspect"] = broken
    result = verify(ALERT, DIAGNOSIS, action_result(), **kw)
    container = next(c for c in result["checks"] if c["signal"] == "container")
    assert container["ok"] is False
    assert "docker not running" in result["notes"]
    assert result["outcome"] == "not_recovered_escalated"


def test_runlog_gets_polls_and_final_result():
    runlog = mock.Mock()
    verify(ALERT, DIAGNOSIS, action_result(), runlog=runlog, **run(["running"]))
    events = [c.args[0] for c in runlog.log.call_args_list]
    assert events == ["verify_poll"] * 3 + ["verification"]


def test_never_checks_flagd_or_clients():
    diagnosis = {**DIAGNOSIS, "component": "flagd", "category": "app_error"}
    alert = {**ALERT, "service": "load-generator"}
    result = verify(alert, diagnosis, action_result(), **run(["running"]))
    assert {c["target"] for c in result["checks"]} == {"valkey-cart"}


def test_verify_does_not_import_action_api():
    source = Path(verify_mod.__file__).read_text(encoding="utf-8")
    assert not re.search(r"action_api", source)

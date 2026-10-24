import re
import time
from pathlib import Path
from unittest import mock

import pytest

from oncall.agent.runlog import RunLog
from oncall.config import Settings
from oncall.safety import executor, killswitch
from oncall.safety.executor import execute, to_request
from oncall.tests.safety.test_approval import PROPOSAL

YES: dict = {
    "approved": True,
    "by": "tester",
    "ts": "2026-10-24T10:00:00+00:00",
    "comment": "go",
}
NO: dict = {**YES, "approved": False, "comment": "not convinced"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated settings, a run log, a fake Action API and a passing allowlist."""
    monkeypatch.delenv(killswitch.ENV_VAR, raising=False)
    settings = Settings(
        RUNS_DIR=str(tmp_path / "runs"),
        KILL_SWITCH_FILE=str(tmp_path / ".killswitch"),
        DRY_RUN=True,
    )
    run = RunLog(run_id="r1", runs_dir=settings.RUNS_DIR)
    api = mock.Mock(
        side_effect=lambda req, s: {
            "request": req,
            "allowed": True,
            "executed": True,
            "dry_run": False,
            "ok": True,
            "detail": "restarted",
            "before": {"state": "exited"},
            "after": {"state": "running"},
            "rollback": None,
            "ts": "2026-10-24T10:00:01+00:00",
        }
    )
    monkeypatch.setattr(executor, "_action_api_execute", lambda: api)
    monkeypatch.setattr(
        executor,
        "_validate",
        lambda req: (req["action"] != "bogus", "allowlist says so"),
    )
    return settings, run, api


def _events(run: RunLog, event: str) -> list[dict]:
    return run.events(event)


def test_approved_and_switch_off_executes_once(env):
    settings, run, api = env
    result = execute(PROPOSAL, run, dry_run=False, approval=YES, settings=settings)
    assert result["executed"] is True
    api.assert_called_once()
    req = api.call_args.args[0]
    assert req == to_request(PROPOSAL, "r1", dry_run=False)
    assert req["run_id"] == "r1"
    stages = [e["stage"] for e in _events(run, "handoff")]
    assert stages == ["executor", "approval"]
    assert _events(run, "action")[-1]["result"]["executed"] is True


def test_kill_switch_blocks_before_anyone_is_asked(env):
    settings, run, api = env
    killswitch.engage(settings)
    ask = mock.Mock()
    with mock.patch.object(executor, "request_approval", ask):
        result = execute(PROPOSAL, run, dry_run=False, settings=settings)
    assert result["executed"] is False
    assert result["allowed"] is False
    assert "kill switch" in result["detail"]
    ask.assert_not_called()
    api.assert_not_called()


def test_not_allowlisted_blocks_before_anyone_is_asked(env):
    settings, run, api = env
    ask = mock.Mock()
    bogus = {**PROPOSAL, "action": "bogus"}
    with mock.patch.object(executor, "request_approval", ask):
        result = execute(bogus, run, dry_run=False, settings=settings)
    assert result["allowed"] is False
    assert "not allowlisted" in result["detail"]
    ask.assert_not_called()
    api.assert_not_called()


def test_rejection_blocks(env):
    settings, run, api = env
    result = execute(PROPOSAL, run, dry_run=False, approval=NO, settings=settings)
    assert result["executed"] is False
    assert result["allowed"] is True
    assert "not approved by tester: not convinced" in result["detail"]
    api.assert_not_called()


def test_cli_rejection_and_timeout_block(env):
    settings, run, api = env
    result = execute(
        PROPOSAL, run, dry_run=False, settings=settings, input_fn=lambda p: "no"
    )
    assert result["executed"] is False
    api.assert_not_called()

    def slow(prompt: str) -> str:
        time.sleep(1)
        return "yes"

    result = execute(
        PROPOSAL, run, dry_run=False, settings=settings, timeout_s=0.1, input_fn=slow
    )
    assert result["executed"] is False
    assert "no answer" in result["detail"]
    api.assert_not_called()


def test_cli_yes_executes(env):
    settings, run, api = env
    result = execute(
        PROPOSAL, run, dry_run=False, settings=settings, input_fn=lambda p: "yes"
    )
    assert result["executed"] is True
    api.assert_called_once()


def test_kill_switch_engaged_after_approval_still_blocks(env):
    settings, run, api = env

    def approve_then_engage(*args, **kwargs):
        killswitch.engage(settings)
        return YES

    with mock.patch.object(executor, "request_approval", approve_then_engage):
        result = execute(PROPOSAL, run, dry_run=False, settings=settings)
    assert result["executed"] is False
    assert result["detail"] == "kill switch engaged after approval"
    api.assert_not_called()


def test_dry_run_never_calls_the_action_api(env):
    settings, run, api = env
    result = execute(PROPOSAL, run, dry_run=True, approval=YES, settings=settings)
    assert result["dry_run"] is True
    assert result["executed"] is False
    assert result["ok"] is True
    assert result["detail"].startswith("[DRY RUN] would restart_service cart")
    api.assert_not_called()
    assert _events(run, "action")[-1]["result"]["dry_run"] is True


def test_dry_run_is_the_default_from_settings(env):
    settings, run, api = env  # DRY_RUN=True in the fixture
    result = execute(PROPOSAL, run, approval=YES, settings=settings)
    assert result["dry_run"] is True
    api.assert_not_called()


def test_action_api_unavailable_blocks(env, monkeypatch):
    settings, run, _api = env
    monkeypatch.setattr(executor, "_action_api_execute", lambda: None)
    result = execute(PROPOSAL, run, dry_run=False, approval=YES, settings=settings)
    assert result["executed"] is False
    assert result["ok"] is False
    assert "unavailable" in result["detail"]


def test_action_api_exception_becomes_a_failed_result(env, monkeypatch):
    settings, run, _api = env
    monkeypatch.setattr(
        executor,
        "_action_api_execute",
        lambda: mock.Mock(side_effect=RuntimeError("boom")),
    )
    result = execute(PROPOSAL, run, dry_run=False, approval=YES, settings=settings)
    assert result["ok"] is False
    assert "RuntimeError: boom" in result["detail"]


def test_missing_allowlist_allows_nothing(monkeypatch):
    # Without F19's allowlist there is no Action API either; stay closed.
    import builtins

    real_import = builtins.__import__

    def no_allowlist(name, *args, **kwargs):
        if name == "oncall.safety.allowlist":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_allowlist)
    allowed, detail = executor._validate(to_request(PROPOSAL, "r", dry_run=False))
    assert allowed is False
    assert "F19" in detail


def test_only_the_executor_imports_the_action_api():
    """Mirror of the CI rule: the LLM-facing code never gets a path to actions."""
    root = Path(executor.__file__).resolve().parents[1]  # oncall/
    pattern = re.compile(
        r"oncall\.safety\.action_api|from oncall\.safety import .*\baction_api\b"
    )
    offenders = [
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if "tests" not in p.parts
        and p.name not in ("executor.py", "action_api.py")
        and pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


ROLLBACK_REQ: dict = {
    "action": "throttle_load",
    "target": "load-generator",
    "params": {"users": 10},
    "reason": "restore the previous user count",
    "run_id": "r1",
    "dry_run": False,
}


def test_execute_request_runs_the_same_gate(env):
    settings, run, api = env
    result = executor.execute_request(
        ROLLBACK_REQ, run, dry_run=False, approval=YES, settings=settings
    )
    assert result["executed"] is True
    sent = api.call_args.args[0]
    assert (sent["action"], sent["target"], sent["params"]) == (
        "throttle_load",
        "load-generator",
        {"users": 10},
    )
    assert sent["reason"].startswith("rollback: restore")
    killswitch.engage(settings)
    blocked = executor.execute_request(
        ROLLBACK_REQ, run, dry_run=False, settings=settings
    )
    assert blocked["executed"] is False


def test_rollback_gate_asks_once_then_executes_with_that_answer(env):
    settings, run, api = env
    asked = []
    approve, do = executor.rollback_gate(
        run,
        dry_run=False,
        settings=settings,
        input_fn=lambda p: asked.append(p) or "yes",
    )
    assert approve(ROLLBACK_REQ) is True
    result = do(ROLLBACK_REQ)
    assert result["executed"] is True
    assert len(asked) == 1  # the human was asked exactly once
    api.assert_called_once()


def test_rollback_gate_rejection_never_executes(env):
    settings, run, api = env
    approve, do = executor.rollback_gate(
        run, dry_run=False, settings=settings, input_fn=lambda p: "no"
    )
    assert approve(ROLLBACK_REQ) is False
    assert do(ROLLBACK_REQ)["executed"] is False
    api.assert_not_called()


def test_rollback_gate_plugs_into_verify_and_maybe_rollback(env, monkeypatch):
    from oncall.safety import verify

    settings, run, api = env
    failed_action = {
        **api(ROLLBACK_REQ, settings),
        "rollback": ROLLBACK_REQ,
    }
    api.reset_mock()
    monkeypatch.setattr(
        verify,
        "_poll",
        lambda *a, **k: {
            "recovered": False,
            "checks": [],
            "polls": 1,
            "duration_s": 0.0,
            "rolled_back": False,
            "rollback_result": None,
            "outcome": "not_recovered_escalated",
            "notes": "still failing",
        },
    )
    approve, do = executor.rollback_gate(
        run, dry_run=False, settings=settings, input_fn=lambda p: "yes"
    )
    v = verify.verify_and_maybe_rollback(
        {
            "id": "a",
            "ts": "t",
            "service": "cart",
            "signal": "manual",
            "value": None,
            "threshold": None,
            "description": "d",
            "source": "manual",
        },
        DIAGNOSIS_FOR_VERIFY,
        failed_action,
        approve_rollback=approve,
        execute_rollback=do,
    )
    assert v["outcome"] == "not_recovered_rolled_back"
    assert v["rolled_back"] is True
    api.assert_called_once()


DIAGNOSIS_FOR_VERIFY = {
    "component": "cart",
    "category": "container_down",
    "cause": "cart container exited",
    "confidence": 0.8,
    "evidence_ids": ["container:cart"],
    "affected_services": ["cart"],
    "alternatives": [],
    "decision": "act",
    "reasoning": "x",
}

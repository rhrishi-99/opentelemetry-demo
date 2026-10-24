"""Tests for the three-agent orchestrator with typed hand-offs (F28)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from oncall.agent.llm import LLMReply
from oncall.agent.orchestrator import (
    Detective,
    HumanGate,
    Operator,
    Planner,
    run_incident,
)
from oncall.agent.runlog import RunLog
from oncall.config import Settings
from oncall.models import (
    Alert,
    Approval,
    Diagnosis,
    Investigation,
    Plan,
)

ALERT: Alert = {
    "id": "alert-test-1",
    "ts": "2026-10-24T10:00:00Z",
    "service": "cartservice",
    "signal": "manual",
    "value": None,
    "threshold": None,
    "description": "cartservice is down and failing requests",
    "source": "manual",
}


class MockLLM:
    """Mock LLM that captures invocations and tools."""

    def __init__(self, reply_text: str = "Alternative hypotheses checked.") -> None:
        self.calls: list[dict] = []
        self.reply_text = reply_text

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        self.calls.append({"messages": messages, "tools": tools})
        return LLMReply(text=self.reply_text, tool_calls=[], usage={"total_tokens": 10})


@pytest.fixture
def runlog(tmp_path: Path) -> RunLog:
    return RunLog(run_id="test-run-123", runs_dir=str(tmp_path))


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    return Settings(
        RUNS_DIR=str(tmp_path),
        KILL_SWITCH_FILE=str(tmp_path / ".killswitch"),
    )


def make_investigation(
    category: str = "container_down",
    component: str = "cartservice",
    decision: str = "act",
    confidence: float = 0.9,
    evidence_valid: bool = True,
) -> Investigation:
    d: Diagnosis = {
        "component": component,
        "category": category,
        "confidence": confidence,
        "decision": decision,  # type: ignore[typeddict-item]
        "cause": f"{category} on {component}",
        "reasoning": f"Evidence shows {component} failed",
        "evidence_ids": [f"container:{component}@2026-10-24T10:00:00Z"],
        "affected_services": [component],
        "alternatives": [],
    }
    return {
        "alert": ALERT,
        "diagnosis": d,
        "evidence_valid": evidence_valid,
        "run_id": "test-run-123",
    }


def test_planner_has_no_tools(runlog: RunLog) -> None:
    """Invariant: Planner is called with tools=[] strictly."""
    mock_llm = MockLLM(
        reply_text="Checked memory leak hypothesis, evidence rules it out."
    )
    planner = Planner(llm=mock_llm)  # type: ignore[arg-type]
    inv = make_investigation()

    plan = planner.review_and_plan(inv, runlog)

    assert len(mock_llm.calls) == 1
    assert mock_llm.calls[0]["tools"] == []
    assert plan["proposal"] is not None
    assert plan["proposal"]["action"] == "restart_service"
    # Allowlist normalizes cartservice -> canonical cart
    assert plan["proposal"]["target"] == "cart"
    assert "prevention" in plan


def test_no_execution_without_approval(test_settings: Settings, runlog: RunLog) -> None:
    """Invariant: Operator never executes actions without explicit human approval."""
    operator = Operator(settings=test_settings)
    inv = make_investigation()
    planner = Planner()
    plan = planner.review_and_plan(inv, runlog)

    rejected_approval: Approval = {
        "approved": False,
        "by": "human_cli",
        "ts": "2026-10-24T10:05:00Z",
        "comment": "Rejected by operator",
    }

    with patch("oncall.agent.orchestrator.execute_action") as mock_exec:
        exec_result = operator.execute_and_report(
            inv, plan, rejected_approval, runlog, dry_run=True
        )
        mock_exec.assert_not_called()

    assert exec_result["action_result"] is None
    assert exec_result["status"] == "aborted"
    assert exec_result["report_path"] is not None
    assert Path(exec_result["report_path"]).is_file()


def test_escalate_path_writes_report(test_settings: Settings, runlog: RunLog) -> None:
    """Invariant: escalate/no_action stops before human gate and still writes a valid report."""
    inv = make_investigation(
        category="unknown", component=None, decision="escalate", confidence=0.3
    )
    mock_llm = MockLLM()

    with (
        patch.object(Detective, "investigate", return_value=inv),
        patch.object(HumanGate, "request_approval") as mock_gate,
        patch("oncall.agent.orchestrator.execute_action") as mock_exec,
    ):
        # Pre-configure gate return for safety bypass check
        mock_gate.return_value = {
            "approved": False,
            "by": "system",
            "ts": "2026-10-24T10:05:00Z",
            "comment": "Bypassed",
        }

        execution = run_incident(
            ALERT,
            channel="cli",
            dry_run=True,
            settings=test_settings,
            llm=mock_llm,  # type: ignore[arg-type]
            runlog=runlog,
        )
        mock_exec.assert_not_called()

    assert execution["status"] == "escalated"
    assert execution["action_result"] is None
    assert execution["report_path"] is not None
    assert Path(execution["report_path"]).is_file()

    # Hand-off went directly from planner to operator
    handoffs = runlog.events("handoff")
    assert any(
        h.get("from") == "planner" and h.get("to") == "operator" for h in handoffs
    )
    assert not any(h.get("to") == "human_gate" for h in handoffs)


def test_kill_switch_blocks_execution(
    test_settings: Settings, runlog: RunLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant: Kill switch blocks approval and action execution."""
    monkeypatch.setenv("KILL_SWITCH", "1")
    gate = HumanGate(settings=test_settings)
    assert gate.kill_switch_engaged()

    inv = make_investigation()
    planner = Planner()
    plan = planner.review_and_plan(inv, runlog)

    approval = gate.request_approval(
        plan["proposal"], inv["diagnosis"], runlog.run_id, channel="cli"
    )
    assert not approval["approved"]
    assert approval["by"] == "kill_switch"

    operator = Operator(settings=test_settings)
    with patch("oncall.agent.orchestrator.execute_action") as mock_exec:
        execution = operator.execute_and_report(
            inv, plan, approval, runlog, dry_run=True
        )
        mock_exec.assert_not_called()

    assert execution["status"] == "aborted"
    assert execution["action_result"] is None


def test_kill_switch_file_blocks(test_settings: Settings, runlog: RunLog) -> None:
    """Kill switch file presence also blocks execution immediately."""
    kill_file = Path(test_settings.KILL_SWITCH_FILE)
    kill_file.write_text("ACTIVE", encoding="utf-8")

    gate = HumanGate(settings=test_settings)
    assert gate.kill_switch_engaged()

    inv = make_investigation()
    planner = Planner()
    plan = planner.review_and_plan(inv, runlog)
    approval = gate.request_approval(
        plan["proposal"], inv["diagnosis"], runlog.run_id, channel="cli"
    )
    assert not approval["approved"]
    assert approval["by"] == "kill_switch"


def test_handoff_events_logged(test_settings: Settings, runlog: RunLog) -> None:
    """Invariant: Every agent boundary crossing emits a typed 'handoff' run-log event."""
    inv = make_investigation()
    mock_llm = MockLLM()

    with patch.object(Detective, "investigate", return_value=inv):
        run_incident(
            ALERT,
            channel="cli",
            dry_run=True,
            settings=test_settings,
            input_fn=lambda _prompt: "yes",
            llm=mock_llm,  # type: ignore[arg-type]
            runlog=runlog,
        )

    handoffs = runlog.events("handoff")
    assert len(handoffs) >= 3

    steps = [(h.get("from"), h.get("to")) for h in handoffs]
    assert ("detective", "planner") in steps
    assert ("planner", "human_gate") in steps
    assert ("human_gate", "operator") in steps


def test_operator_executes_only_approved_proposal(
    test_settings: Settings, runlog: RunLog
) -> None:
    """Operator executes approved proposal via Action API and writes verification."""
    operator = Operator(settings=test_settings)
    inv = make_investigation()
    plan: Plan = {
        "review": {},
        "diagnosis_final": inv["diagnosis"],
        "proposal": {
            "action": "restart_service",
            "target": "cartservice",
            "params": {},
            "reason": "Recover cartservice",
            "expected_effect": "Healthy container",
            "rollback_hint": "idempotent",
            "risk": "low",
        },
        "prevention": "Alerting",
    }
    approval: Approval = {
        "approved": True,
        "by": "human_cli",
        "ts": "2026-10-24T10:05:00Z",
        "comment": "Approved",
    }

    mock_res = {
        "request": {},
        "allowed": True,
        "executed": True,
        "dry_run": True,
        "ok": True,
        "detail": "[DRY RUN] Would restart cartservice",
        "before": {},
        "after": {},
        "rollback": None,
        "ts": "2026-10-24T10:05:01Z",
    }

    with patch(
        "oncall.agent.orchestrator.execute_action", return_value=mock_res
    ) as mock_exec:
        execution = operator.execute_and_report(
            inv, plan, approval, runlog, dry_run=True
        )
        mock_exec.assert_called_once()

    assert execution["status"] == "success"
    assert execution["action_result"] is not None
    assert execution["action_result"]["ok"] is True
    # A dry run changes nothing, so recovery cannot be claimed: F24 says so.
    assert execution["verification"] is not None
    assert execution["verification"]["outcome"] == "skipped_dry_run"
    assert execution["verification"]["recovered"] is False


def test_operator_rollback_on_failed_action(
    test_settings: Settings, runlog: RunLog
) -> None:
    """Operator triggers rollback and escalates when action execution fails."""
    operator = Operator(settings=test_settings)
    inv = make_investigation()
    plan: Plan = {
        "review": {},
        "diagnosis_final": inv["diagnosis"],
        "proposal": {
            "action": "throttle_load",
            "target": "load-generator",
            "params": {"users": 5},
            "reason": "Throttle surge",
            "expected_effect": "Reduce load",
            "rollback_hint": "restore previous state",
            "risk": "low",
        },
        "prevention": "Rate limiting",
    }
    approval: Approval = {
        "approved": True,
        "by": "human_cli",
        "ts": "2026-10-24T10:05:00Z",
        "comment": "Approved",
    }

    fail_res = {
        "request": {},
        "allowed": True,
        "executed": True,
        "dry_run": False,
        "ok": False,
        "detail": "Failed to contact Locust",
        "before": {"users": 10},
        "after": {},
        "rollback": {
            "action": "throttle_load",
            "target": "load-generator",
            "params": {"users": 10},
            "reason": "Rollback load throttling",
            "run_id": runlog.run_id,
            "dry_run": False,
        },
        "ts": "2026-10-24T10:05:01Z",
    }
    rollback_res = {
        "request": {},
        "allowed": True,
        "executed": True,
        "dry_run": False,
        "ok": True,
        "detail": "Rollback succeeded",
        "before": {},
        "after": {},
        "rollback": None,
        "ts": "2026-10-24T10:05:02Z",
    }

    with patch(
        "oncall.agent.orchestrator.execute_action", side_effect=[fail_res, rollback_res]
    ) as mock_exec:
        execution = operator.execute_and_report(
            inv, plan, approval, runlog, dry_run=False
        )
        assert mock_exec.call_count == 2

    assert execution["status"] == "escalated"
    assert execution["verification"] is not None
    assert execution["verification"]["recovered"] is False
    assert execution["verification"]["rollback_executed"] is True


def test_cli_run_subcommand(tmp_path: Path) -> None:
    """Test python -m oncall.main run command parses and dispatches correctly."""
    from oncall.main import main

    mock_exec = {
        "investigation": {"run_id": "test-cli-run"},
        "status": "success",
        "report_path": str(tmp_path / "report.json"),
    }
    with patch(
        "oncall.agent.orchestrator.run_incident", return_value=mock_exec
    ) as mock_run:
        exit_code = main(
            [
                "run",
                "--description",
                "Test incident",
                "--service",
                "cartservice",
                "--execute",
            ]
        )
        assert exit_code == 0
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["dry_run"] is False
        assert call_kwargs["channel"] == "cli"


def _executed_result(rollback: dict | None = None) -> dict:
    return {
        "request": {},
        "allowed": True,
        "executed": True,
        "dry_run": False,
        "ok": True,
        "detail": "Restarted container cart",
        "before": {"status": "exited"},
        "after": {"status": "running"},
        "rollback": rollback,
        "ts": "2026-10-24T10:05:01Z",
    }


def _plan(inv: Investigation) -> Plan:
    return {
        "review": {},
        "diagnosis_final": inv["diagnosis"],
        "proposal": {
            "action": "restart_service",
            "target": "cart",
            "params": {},
            "reason": "Recover cart",
            "expected_effect": "Healthy container",
            "rollback_hint": "idempotent",
            "risk": "low",
        },
        "prevention": "Alerting",
    }


APPROVED: Approval = {
    "approved": True,
    "by": "human_cli",
    "ts": "2026-10-24T10:05:00Z",
    "comment": "Approved",
}


def _verification(outcome: str) -> dict:
    return {
        "recovered": outcome == "recovered",
        "checks": [],
        "polls": 3,
        "duration_s": 45.0,
        "rolled_back": False,
        "rollback_result": None,
        "outcome": outcome,
        "notes": "",
    }


def test_operator_success_requires_verified_recovery(
    test_settings: Settings, runlog: RunLog
) -> None:
    """status is success only when F24 verification says recovered."""
    operator = Operator(settings=test_settings)
    inv = make_investigation()
    with (
        patch(
            "oncall.agent.orchestrator.execute_action",
            return_value=_executed_result(),
        ),
        patch(
            "oncall.agent.orchestrator.verify_recovery",
            return_value=_verification("recovered"),
        ) as verify,
    ):
        execution = operator.execute_and_report(
            inv, _plan(inv), APPROVED, runlog, dry_run=False
        )
    assert execution["status"] == "success"
    assert execution["verification"]["outcome"] == "recovered"
    kwargs = verify.call_args.kwargs
    assert kwargs["timeout_s"] == test_settings.VERIFY_WINDOW_S
    assert callable(kwargs["approve_rollback"])
    assert callable(kwargs["execute_rollback"])


def test_operator_escalates_when_not_recovered(
    test_settings: Settings, runlog: RunLog
) -> None:
    operator = Operator(settings=test_settings)
    inv = make_investigation()
    with (
        patch(
            "oncall.agent.orchestrator.execute_action",
            return_value=_executed_result(),
        ),
        patch(
            "oncall.agent.orchestrator.verify_recovery",
            return_value=_verification("not_recovered_escalated"),
        ),
    ):
        execution = operator.execute_and_report(
            inv, _plan(inv), APPROVED, runlog, dry_run=False
        )
    assert execution["status"] == "escalated"
    assert execution["verification"]["recovered"] is False


def test_rollback_offered_by_verify_goes_through_the_human_gate(
    test_settings: Settings, runlog: RunLog
) -> None:
    """When verify asks for a rollback, the human is asked and the gate executes it."""
    operator = Operator(settings=test_settings)
    inv = make_investigation()
    rollback_req = {
        "action": "throttle_load",
        "target": "load-generator",
        "params": {"users": 10},
        "reason": "restore",
        "run_id": "test-run-123",
        "dry_run": False,
    }
    asked: list[str] = []

    def fake_verify(
        alert, diagnosis, action_result, *, approve_rollback, execute_rollback, **kw
    ):
        approved = approve_rollback(rollback_req)
        result = execute_rollback(rollback_req) if approved else None
        rolled_back = bool(result and result["ok"])
        v = _verification(
            "not_recovered_rolled_back" if rolled_back else "not_recovered_escalated"
        )
        v["rolled_back"] = rolled_back
        v["rollback_result"] = result
        return v

    def fake_api(req, settings):
        return {**_executed_result(), "request": req, "detail": "rolled back"}

    with (
        patch(
            "oncall.agent.orchestrator.execute_action",
            return_value=_executed_result(rollback=rollback_req),
        ) as mock_exec,
        patch("oncall.agent.orchestrator.verify_recovery", side_effect=fake_verify),
        patch("oncall.safety.executor._validate", return_value=(True, "ok")),
        patch("oncall.safety.executor._action_api_execute", return_value=fake_api),
    ):
        execution = operator.execute_and_report(
            inv,
            _plan(inv),
            APPROVED,
            runlog,
            dry_run=False,
            input_fn=lambda prompt: asked.append(prompt) or "yes",
        )
    assert len(asked) == 1  # the human was asked once, for the rollback
    assert execution["verification"]["rolled_back"] is True
    assert execution["status"] == "escalated"
    assert (
        mock_exec.call_count == 1
    )  # the original action; the rollback ran via the gate

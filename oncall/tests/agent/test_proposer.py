import json

import pytest

from oncall.agent.llm import LLMReply
from oncall.agent.proposer import ALLOWED_ACTIONS, build_plan, propose
from oncall.agent.runlog import RunLog
from oncall.models import Investigation, make_result

USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
EVIDENCE_ID = "container:cart"


class PlannerLLM:
    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = []

    def chat(self, messages, tools):
        self.calls.append((messages, tools))
        return LLMReply(self.texts.pop(0), [], USAGE, "test:model")


def _run(tmp_path, state="running"):
    run = RunLog("planner", str(tmp_path))
    result = make_result(
        "container_health",
        {},
        "",
        [{"id": EVIDENCE_ID, "text": f"cart: state={state}, health=none", "ts": None}],
    )
    run.log("tool_result", name="container_health", result=result)
    return run


def _diagnosis(**updates):
    diagnosis = {
        "component": "cart",
        "category": "app_error",
        "cause": "cart fails on a request",
        "confidence": 0.85,
        "evidence_ids": [EVIDENCE_ID],
        "affected_services": ["cart"],
        "alternatives": [],
        "decision": "act",
        "reasoning": "Cart is failing.",
    }
    diagnosis.update(updates)
    return diagnosis


def _proposal_text(**updates):
    response = {
        "reason": "The evidence supports this action.",
        "expected_effect": "Service recovers.",
    }
    response.update(updates)
    return json.dumps(response)


@pytest.mark.parametrize(
    ("category", "state", "action", "risk"),
    [
        ("container_down", "exited", "start_service", "low"),
        ("container_down", "running", "restart_service", "low"),
        ("dependency_unavailable", "exited", "start_service", "low"),
        ("app_error", "running", "disable_failure_flags", "medium"),
        ("resource_memory", "running", "restart_service", "low"),
        ("resource_cpu", "running", "restart_service", "medium"),
    ],
)
def test_fixed_category_action_mapping(tmp_path, category, state, action, risk):
    run = _run(tmp_path, state)
    llm = PlannerLLM(_proposal_text(action="throttle_load"))
    proposal = propose(_diagnosis(category=category), run, llm)
    assert proposal is not None
    assert proposal["action"] == action
    assert proposal["risk"] == risk
    assert proposal["action"] in ALLOWED_ACTIONS
    assert llm.calls[0][1] == []


def test_app_error_falls_back_to_restart_when_preferred_action_is_not_allowed(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "oncall.agent.proposer.ALLOWED_ACTIONS",
        frozenset({"restart_service", "start_service", "throttle_load"}),
    )
    proposal = propose(_diagnosis(), _run(tmp_path), PlannerLLM(_proposal_text()))
    assert proposal is not None and proposal["action"] == "restart_service"


def test_decision_or_category_without_safe_action_returns_none(tmp_path):
    run = _run(tmp_path)
    for category in (
        "network_degradation",
        "unknown",
        "upstream_propagation",
        "no_incident",
    ):
        assert (
            propose(_diagnosis(category=category), run, PlannerLLM(_proposal_text()))
            is None
        )
    assert (
        propose(_diagnosis(decision="escalate"), run, PlannerLLM(_proposal_text()))
        is None
    )


def test_llm_cannot_override_the_fixed_action_and_bad_text_is_rejected(tmp_path):
    run = _run(tmp_path)
    proposal = propose(
        _diagnosis(), run, PlannerLLM(_proposal_text(action="restart_service"))
    )
    assert proposal is not None and proposal["action"] == "disable_failure_flags"
    assert propose(_diagnosis(), run, PlannerLLM("not json")) is None


def test_traffic_surge_requires_a_known_user_count(tmp_path):
    run = _run(tmp_path)
    diagnosis = _diagnosis(category="traffic_surge")
    assert propose(diagnosis, run, PlannerLLM(_proposal_text())) is None

    run.log("action", result={"before": {"users": 20}})
    proposal = propose(diagnosis, run, PlannerLLM(_proposal_text()))
    assert proposal is not None
    assert proposal["action"] == "throttle_load"
    assert proposal["target"] == "load-generator"
    assert proposal["params"] == {"users": 10}


def test_build_plan_escalates_when_review_cannot_rule_out_challenge(tmp_path):
    run = _run(tmp_path)
    inv: Investigation = {
        "alert": {
            "id": "a1",
            "ts": "now",
            "service": "cart",
            "signal": "manual",
            "value": None,
            "threshold": None,
            "description": "cart errors",
            "source": "manual",
        },
        "diagnosis": _diagnosis(),
        "evidence_valid": True,
        "run_id": run.run_id,
    }
    review = json.dumps(
        {
            "challenges": [
                {
                    "hypothesis": "A downstream dependency could be the cause.",
                    "ruled_out": False,
                    "evidence_ids": [EVIDENCE_ID],
                    "reason": "The evidence does not rule it out.",
                }
            ],
            "confidence_adjustment": 0,
        }
    )
    plan = build_plan(inv, run, PlannerLLM(review))
    assert plan["diagnosis_final"]["decision"] == "escalate"
    assert plan["diagnosis_final"]["confidence"] == pytest.approx(0.75)
    assert plan["proposal"] is None
    assert [event["to_agent"] for event in run.events("handoff")] == [
        "planner",
        "human_gate",
    ]


def test_build_plan_returns_proposal_after_review_rules_out_challenge(tmp_path):
    run = _run(tmp_path)
    inv: Investigation = {
        "alert": {
            "id": "a1",
            "ts": "now",
            "service": "cart",
            "signal": "manual",
            "value": None,
            "threshold": None,
            "description": "cart errors",
            "source": "manual",
        },
        "diagnosis": _diagnosis(),
        "evidence_valid": True,
        "run_id": run.run_id,
    }
    review = json.dumps(
        {
            "challenges": [
                {
                    "hypothesis": "A dependency failure caused the errors.",
                    "ruled_out": True,
                    "evidence_ids": [EVIDENCE_ID],
                    "reason": "Container and logs indicate the application path.",
                }
            ],
            "confidence_adjustment": 0,
        }
    )
    llm = PlannerLLM(review, _proposal_text())
    plan = build_plan(inv, run, llm)
    assert len(llm.calls) == 2
    assert all(tools == [] for _, tools in llm.calls)
    assert plan["proposal"] is not None
    assert plan["diagnosis_final"]["decision"] == "act"
    assert plan["prevention"]

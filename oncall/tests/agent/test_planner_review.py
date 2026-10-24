import json

from oncall.agent.llm import LLM, LLMReply
from oncall.agent.planner_review import MAX_REVIEW_COMPLETION_TOKENS, review
from oncall.agent.runlog import RunLog
from oncall.config import Settings
from oncall.models import Investigation, make_result

EVIDENCE_ID = "log:cart:1@2026-10-24T10:00:00Z"
USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


class PlannerLLM:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def chat(self, messages, tools):
        self.calls.append((messages, tools))
        return LLMReply(self.text, [], USAGE, "test:model")


class CaptureTransport:
    def __init__(self, content):
        self.content = content
        self.requests = []

    def __call__(self, url, headers, body):
        self.requests.append(body)
        return 200, json.dumps(
            {
                "choices": [{"message": {"content": self.content}}],
                "usage": USAGE,
            }
        )


def _run(tmp_path):
    run = RunLog("review", str(tmp_path))
    run.log(
        "tool_result",
        name="search_logs",
        result=make_result(
            "search_logs",
            {"service": "cart"},
            "",
            [{"id": EVIDENCE_ID, "text": "cart returned an error", "ts": None}],
        ),
    )
    return run


def _investigation(run) -> Investigation:
    return {
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
        "diagnosis": {
            "component": "cart",
            "category": "app_error",
            "cause": "cart request failed",
            "confidence": 0.85,
            "evidence_ids": [EVIDENCE_ID],
            "affected_services": ["cart"],
            "alternatives": [],
            "decision": "act",
            "reasoning": "Cart returns an error.",
        },
        "evidence_valid": True,
        "run_id": run.run_id,
    }


def _review_response(evidence_ids=None, ruled_out=True):
    return json.dumps(
        {
            "challenges": [
                {
                    "hypothesis": "A dependency issue caused the failure.",
                    "ruled_out": ruled_out,
                    "evidence_ids": [EVIDENCE_ID]
                    if evidence_ids is None
                    else evidence_ids,
                    "reason": "The cited log supports this assessment.",
                }
            ],
            "confidence_adjustment": 0,
        }
    )


def test_review_uses_one_tool_free_call_and_only_known_evidence(tmp_path):
    run = _run(tmp_path)
    llm = PlannerLLM(_review_response())
    result = review(_investigation(run), llm, run)
    assert len(llm.calls) == 1
    assert llm.calls[0][1] == []
    assert result["challenges"][0]["ruled_out"] is True
    assert result["challenges"][0]["evidence_ids"] == [EVIDENCE_ID]


def test_review_downgrades_challenge_with_unknown_evidence(tmp_path):
    run = _run(tmp_path)
    result = review(
        _investigation(run),
        PlannerLLM(_review_response(["log:not-in-run", EVIDENCE_ID])),
        run,
    )
    challenge = result["challenges"][0]
    assert challenge["ruled_out"] is False
    assert challenge["evidence_ids"] == [EVIDENCE_ID]
    assert result["confidence_adjustment"] <= -0.1


def test_review_invalid_output_becomes_unresolved_without_retry(tmp_path):
    run = _run(tmp_path)
    llm = PlannerLLM("not json")
    result = review(_investigation(run), llm, run)
    assert len(llm.calls) == 1
    assert result["challenges"][0]["ruled_out"] is False
    assert result["confidence_adjustment"] == -0.1


def test_review_caps_completion_and_sends_no_tools(tmp_path):
    run = _run(tmp_path)
    transport = CaptureTransport(_review_response())
    llm = LLM(
        Settings(
            LLM_PROVIDER="groq",
            LLM_API_KEY="test-key",
            LLM_MAX_TOKENS=4000,
        ),
        transport,
    )
    review(_investigation(run), llm, run)
    request = transport.requests[0]
    assert len(transport.requests) == 1
    assert request["max_completion_tokens"] == MAX_REVIEW_COMPLETION_TOKENS
    assert "tools" not in request
    assert len(request["messages"][0]["content"]) < 6000

import json
import re
from pathlib import Path

import pytest

from oncall.agent import diagnosis, prompts, taxonomy
from oncall.agent.diagnosis import check, diagnose, escalate_unknown
from oncall.agent.llm import LLMError, LLMReply
from oncall.agent.loop import investigate_and_diagnose
from oncall.agent.runlog import RunLog
from oncall.models import make_result
from oncall.tests.agent.stub_tools import STUB_SCHEMAS, STUB_TOOLS
from oncall.tests.agent.test_loop import ALERT, USAGE, ScriptedLLM, call, reply

TRACE_ID = "trace:abc123/span:GetCart"
LOG_ID = "log:cart@2026-10-23T10:00:00Z"


def valid(**overrides) -> dict:
    d = {
        "component": "cart",
        "category": "app_error",
        "cause": "cart GetCart fails with connection refused to its cache.",
        "confidence": 0.8,
        "evidence_ids": [TRACE_ID, LOG_ID, TRACE_ID],
        "affected_services": ["cart", "checkout"],
        "alternatives": [
            {"hypothesis": "checkout bug", "rejected_because": "errors start in cart"}
        ],
        "decision": "act",
        "reasoning": f"Deepest ERROR span is in cart [{TRACE_ID}] [{LOG_ID}].",
    }
    d.update(overrides)
    return d


def submit(args) -> callable:
    return reply(call(diagnosis.SUBMIT, args))


@pytest.fixture
def run(tmp_path):
    rl = RunLog(run_id="t", runs_dir=str(tmp_path))
    traces = make_result(
        "search_traces",
        {"service": "cart"},
        "12 error traces",
        [{"id": TRACE_ID, "text": "GetCart ERROR", "ts": None}],
    )
    logs = make_result(
        "search_logs",
        {"service": "cart"},
        "connection refused x40",
        [{"id": LOG_ID, "text": "connection refused", "ts": None}],
    )
    rl.log("tool_result", step=1, name="search_traces", result=traces)
    rl.log("tool_result", step=2, name="search_logs", result=logs)
    rl.log("finish", stopped="finish", summary="cart errors", steps=3)
    return rl


def test_valid_diagnosis_returned(run):
    llm = ScriptedLLM([submit(valid())])
    d = diagnose(run, ALERT, llm)
    assert d["component"] == "cart"
    assert d["category"] == "app_error"
    assert d["decision"] == "act"
    assert d["evidence_ids"] == [TRACE_ID, LOG_ID]
    logged = run.events("decision")[-1]
    assert logged["valid"] is True
    assert logged["diagnosis"] == d


def test_invalid_then_valid_retries_once(run):
    seen = {}

    def second(messages):
        seen["retry"] = messages[-1]["content"]
        return LLMReply("", [call(diagnosis.SUBMIT, valid())], USAGE)

    llm = ScriptedLLM([submit(valid(category="cosmic_rays")), second])
    d = diagnose(run, ALERT, llm)
    assert llm.calls == 2
    assert d["category"] == "app_error"
    assert "category must be one of" in seen["retry"]


def test_invalid_twice_escalates_unknown(run):
    llm = ScriptedLLM([submit({"component": "cart"})])
    d = diagnose(run, ALERT, llm)
    assert llm.calls == diagnosis.MAX_ATTEMPTS
    assert d["decision"] == "escalate"
    assert d["category"] == "unknown"
    assert d["component"] is None
    assert "missing fields" in d["reasoning"]
    assert run.events("decision")[-1]["valid"] is False


def test_llm_error_escalates_unknown(run):
    def boom(messages):
        raise LLMError("HTTP 500")

    d = diagnose(run, {**ALERT, "service": "checkout"}, ScriptedLLM([boom]))
    assert d["decision"] == "escalate"
    assert d["category"] == "unknown"
    assert d["affected_services"] == ["checkout"]
    assert "HTTP 500" in d["reasoning"]


def test_text_reply_is_invalid(run):
    def text(messages):
        return LLMReply("it is cart", [], USAGE)

    d = diagnose(run, ALERT, ScriptedLLM([text]))
    assert d["category"] == "unknown"
    assert "must call submit_diagnosis" in d["reasoning"]


def test_bad_arguments_json_is_invalid(run):
    bad = reply(call(diagnosis.SUBMIT, None, error="arguments are not valid JSON"))
    d = diagnose(run, ALERT, ScriptedLLM([bad]))
    assert d["category"] == "unknown"


@pytest.mark.parametrize("client", taxonomy.CLIENT_SERVICES)
def test_client_never_component(run, client):
    d = diagnose(run, ALERT, ScriptedLLM([submit(valid(component=client))]))
    assert d["component"] is None
    assert d["decision"] == "escalate"
    assert client in d["affected_services"]
    assert d["reasoning"].startswith("[post-check:")


@pytest.mark.parametrize("confidence", [-0.1, 1.5, "high", True])
def test_bad_confidence_invalid(confidence):
    assert any("confidence" in p for p in check(valid(confidence=confidence)))


def test_check_accepts_valid_and_null_component():
    assert check(valid()) == []
    assert check(valid(component=None, decision="escalate")) == []


def test_check_rejects_wrong_types():
    problems = check(
        valid(component=3, evidence_ids="x", alternatives=[{"hypothesis": 1}])
    )
    assert len(problems) == 3
    assert check("nope") == ["the diagnosis must be a JSON object"]


def test_reasoning_truncated(run):
    long = valid(reasoning="x" * 5000)
    d = diagnose(run, ALERT, ScriptedLLM([submit(long)]))
    assert len(d["reasoning"]) == diagnosis.MAX_REASONING_CHARS


def test_prompt_includes_run_evidence_and_taxonomy(run):
    seen = {}

    def capture(messages):
        seen["prompt"] = messages[0]["content"]
        return LLMReply("", [call(diagnosis.SUBMIT, valid())], USAGE)

    diagnose(run, ALERT, ScriptedLLM([capture]))
    prompt = seen["prompt"]
    assert f"- {TRACE_ID}: GetCart ERROR" in prompt
    assert "search_logs" in prompt and "connection refused x40" in prompt
    assert "cart errors" in prompt
    for name in taxonomy.CATEGORIES:
        assert f"- {name}:" in prompt


def test_only_submit_tool_offered(run):
    seen = {}

    class Capture:
        def chat(self, messages, tools):
            seen["tools"] = tools
            return LLMReply("", [call(diagnosis.SUBMIT, valid())], USAGE)

    diagnose(run, ALERT, Capture())
    assert [t["name"] for t in seen["tools"]] == [diagnosis.SUBMIT]


def test_escalate_unknown_is_valid_payload():
    assert check(escalate_unknown("why")) == []


def test_investigate_and_diagnose(tmp_path):
    rl = RunLog(run_id="e2e", runs_dir=str(tmp_path))
    llm = ScriptedLLM(
        [
            reply(call("stub_search_traces", {"service": "cart"}, 1)),
            reply(call("finish", {"summary": "cart errors"}, 2)),
            submit(valid(evidence_ids=[TRACE_ID])),
        ]
    )
    result = investigate_and_diagnose(
        ALERT, tools=STUB_TOOLS, llm=llm, runlog=rl, schemas=STUB_SCHEMAS
    )
    assert result["stopped"] == "finish"
    assert result["diagnosis"]["component"] == "cart"
    assert TRACE_ID in rl.evidence_index()
    assert [e["event"] for e in rl.events()][-1] == "decision"


AGENT_DIR = Path(diagnosis.__file__).parent
# Flag wording ("feature" + "_", "." or " " + "flag", and the flag service name,
# written so this file passes CI's own greps) and answer-style mappings such
# as "scenario-01" or "scenario 3".
FORBIDDEN = re.compile(r"feature[_. ]?flag|flag-?d|scenario[- ]?\d", re.IGNORECASE)


@pytest.mark.parametrize("name", ["taxonomy.py", "prompts.py"])
def test_no_flag_or_scenario_text(name):
    text = (AGENT_DIR / name).read_text(encoding="utf-8")
    assert not FORBIDDEN.search(text)


def test_investigate_prompt_limits_repeated_tool_calls():
    text = " ".join(prompts.INVESTIGATE.split())
    assert "Never call the same tool for the same service more than twice." in text
    assert (
        "If a tool returns nothing twice, move on to a different tool or finish."
        in text
    )


def _containers(rl, *lines):
    evidence = [
        {"id": f"container:{line.split(':', 1)[0]}", "text": line, "ts": None}
        for line in lines
    ]
    rl.log(
        "tool_result",
        step=9,
        name="container_health",
        result=make_result("container_health", {}, "containers", evidence),
    )


def _llm_down(messages):
    raise LLMError("HTTP 429: rate limited")


def _logs(rl, *items):
    rl.log(
        "tool_result",
        step=1,
        name="search_logs",
        result=make_result(
            "search_logs",
            {},
            "logs",
            [{"id": i, "text": t, "ts": None} for i, t in items],
        ),
    )


VALKEY_DOWN = (
    "valkey-cart: state=exited, health=unhealthy | FLAGGED: not running "
    "(state=exited, exit_code=0); healthcheck unhealthy"
)
EMAIL_DOWN = "email: state=exited | FLAGGED: not running (state=exited, exit_code=0)"


def test_fallback_picks_the_single_down_container(tmp_path):
    rl = RunLog(run_id="fb1", runs_dir=str(tmp_path))
    _logs(
        rl,
        ("log:cart:1@t", "x19 [cart] cannot connect to redis"),
        ("log:ad:1@t", "x2 [ad] ad timeout"),
    )
    _containers(
        rl,
        VALKEY_DOWN,
        "product-catalog: state=running, health=healthy | FLAGGED: CPU 110% of limit",
        "cart: state=running, health=healthy",
    )
    d = diagnose(rl, ALERT, ScriptedLLM([_llm_down]))
    assert d["component"] == "valkey-cart"
    # cart's errors reference it (via the redis alias): a dependency is unavailable.
    assert d["category"] == "dependency_unavailable"
    assert d["decision"] == "act"
    assert d["confidence"] == diagnosis.FALLBACK_CONFIDENCE
    assert d["evidence_ids"] == ["container:valkey-cart", "log:cart:1@t"]
    assert d["cause"] == "valkey-cart down → cart can't reach it (19 errors)"
    assert d["reasoning"].startswith("rule-based diagnosis (LLM unavailable)")
    assert "HTTP 429" in d["reasoning"]


def test_fallback_ranks_two_down_containers_by_evidence(tmp_path):
    rl = RunLog(run_id="fb2", runs_dir=str(tmp_path))
    _logs(rl, ("log:cart:1@t", "x19 [cart] Wasn't able to connect to redis"))
    rl.log(
        "tool_result",
        step=2,
        name="get_metrics",
        result=make_result(
            "get_metrics",
            {},
            "errors",
            [
                {
                    "id": "metric:error_rate:cart@t",
                    "text": "cart error_rate 0.1200 (baseline 0.0000, delta +0.1200)",
                    "ts": None,
                }
            ],
        ),
    )
    _containers(rl, EMAIL_DOWN, VALKEY_DOWN)
    d = diagnose(rl, ALERT, ScriptedLLM([_llm_down]))
    assert d["component"] == "valkey-cart"
    assert d["affected_services"] == ["valkey-cart", "email"]
    assert d["decision"] == "escalate"
    assert d["category"] == "dependency_unavailable"
    assert "container:valkey-cart" in d["evidence_ids"]
    assert any(e.startswith("log:cart:") for e in d["evidence_ids"])
    assert "metric:error_rate:cart@t" in d["evidence_ids"]
    assert "container:email" in d["evidence_ids"]
    assert d["cause"] == (
        "valkey-cart down → cart can't reach it (19 errors); "
        "email also down (no user-facing errors)"
    )


def test_tool_use_failed_is_retried_with_a_nudge_then_falls_back(tmp_path):
    rl = RunLog(run_id="fb3", runs_dir=str(tmp_path))
    _containers(rl, VALKEY_DOWN)
    seen = []

    def failed(messages):
        seen.append(messages[-1]["content"])
        raise LLMError(
            'groq returned HTTP 400: {"error":{"code":"tool_use_failed"}}', status=400
        )

    llm = ScriptedLLM([failed])
    d = diagnose(rl, ALERT, llm)
    assert llm.calls == diagnosis.MAX_ATTEMPTS
    assert seen[1:] == [diagnosis.NUDGE, diagnosis.NUDGE]
    assert d["component"] == "valkey-cart"
    assert d["reasoning"].startswith("rule-based diagnosis (LLM unavailable)")
    assert "tool_use_failed" in d["reasoning"]


def test_auto_tool_choice_reads_json_from_the_text(run):
    class AutoLLM(ScriptedLLM):
        def chat(self, messages, tools, tool_choice="required"):
            if tool_choice == "auto":
                body = json.dumps({"name": diagnosis.SUBMIT, "arguments": valid()})
                return LLMReply(text=f"Here it is: {body}", tool_calls=[], usage=USAGE)
            return super().chat(messages, tools)

    text_only = lambda messages: LLMReply(text="I think cart", usage=USAGE)
    d = diagnose(run, ALERT, AutoLLM([text_only]))
    assert d["component"] == "cart"
    assert run.events("decision")[-1]["attempt"] == "auto"


def test_any_other_exception_falls_back(tmp_path):
    rl = RunLog(run_id="fb4", runs_dir=str(tmp_path))
    _containers(rl, VALKEY_DOWN)

    def crash(messages):
        raise TimeoutError("read timed out")

    d = diagnose(rl, ALERT, ScriptedLLM([crash]))
    assert d["component"] == "valkey-cart"
    assert "read timed out" in d["reasoning"]


def test_fallback_tie_goes_to_the_container_users_depend_on(tmp_path):
    rl = RunLog(run_id="fb5", runs_dir=str(tmp_path))
    _containers(rl, EMAIL_DOWN, VALKEY_DOWN)
    d = diagnose(rl, ALERT, ScriptedLLM([_llm_down]))
    assert d["component"] == "valkey-cart"
    assert d["category"] == "container_down"
    assert d["affected_services"] == ["valkey-cart", "email"]


def _cart_errors(rl):
    rl.log(
        "tool_result",
        step=3,
        name="get_metrics",
        result=make_result(
            "get_metrics",
            {},
            "errors",
            [
                {
                    "id": "metric:error_rate:cart@t",
                    "text": "cart error_rate 0.3000 (baseline 0.0000, delta +0.3)",
                    "ts": None,
                },
                {
                    "id": "metric:error_rate:checkout@t",
                    "text": "checkout error_rate 0.0000 (baseline 0.0000, delta +0)",
                    "ts": None,
                },
            ],
        ),
    )


def _llm_pick(**over):
    return valid(
        component="cart",
        category="app_error",
        evidence_ids=["metric:error_rate:cart@t"],
        alternatives=[
            {"hypothesis": "valkey-cart is down", "rejected_because": "not checked"}
        ],
        **over,
    )


def test_precedence_moves_the_llm_pick_to_alternatives(tmp_path):
    rl = RunLog(run_id="p1", runs_dir=str(tmp_path))
    _logs(rl, ("log:cart:1@t", "x12 [cart] Wasn't able to connect to redis"))
    _cart_errors(rl)
    _containers(rl, VALKEY_DOWN, "cart: state=running, health=healthy")
    d = diagnosis.container_precedence(_llm_pick(), rl)
    assert d["component"] == "valkey-cart"
    assert d["category"] == "dependency_unavailable"
    assert d["affected_services"] == ["cart"]
    assert d["alternatives"][0] == {
        "hypothesis": "cart is the root cause (app_error)",
        "rejected_because": "downstream symptom of valkey-cart being down",
    }
    # The down container never stays among the rejected alternatives.
    assert all("valkey-cart" not in a["hypothesis"] for a in d["alternatives"])
    assert d["evidence_ids"][:2] == ["container:valkey-cart", "log:cart:1@t"]
    assert "metric:error_rate:cart@t" in d["evidence_ids"]
    assert d["reasoning"].startswith("[precedence rule applied: valkey-cart is down")
    logged = [e for e in rl.events("decision") if e.get("stage") == "precedence"]
    assert logged and logged[0]["rule"] == "precedence rule applied"


def test_precedence_with_metric_regressions_only_is_container_down(tmp_path):
    rl = RunLog(run_id="p2", runs_dir=str(tmp_path))
    _cart_errors(rl)
    _containers(rl, VALKEY_DOWN)
    d = diagnosis.container_precedence(_llm_pick(), rl)
    assert (d["component"], d["category"]) == ("valkey-cart", "container_down")
    assert d["affected_services"] == ["cart"]


def test_precedence_needs_one_down_container_and_a_symptom(tmp_path):
    rl = RunLog(run_id="p3", runs_dir=str(tmp_path))
    _containers(rl, VALKEY_DOWN)
    assert diagnosis.container_precedence(_llm_pick(), rl)["component"] == "cart"
    rl2 = RunLog(run_id="p4", runs_dir=str(tmp_path))
    _cart_errors(rl2)
    _containers(rl2, VALKEY_DOWN, EMAIL_DOWN)
    assert diagnosis.container_precedence(_llm_pick(), rl2)["component"] == "cart"


def test_telemetry_infra_is_never_the_component(run):
    d = diagnose(run, ALERT, ScriptedLLM([submit(valid(component="otelcol-contrib"))]))
    assert d["component"] is None
    assert d["decision"] == "escalate"
    assert "telemetry infra" in d["reasoning"]

"""Deterministic, allowlist-checked remediation proposals (F22)."""

import json

from oncall.agent.llm import LLM
from oncall.agent.planner_review import review
from oncall.agent.prompts import PROPOSE
from oncall.agent.runlog import RunLog
from oncall.agent.taxonomy import CLIENT_SERVICES
from oncall.config import load_settings
from oncall.models import Diagnosis, Investigation, Plan, Proposal, scrub_flags

ALLOWED_ACTIONS = frozenset(
    {"start_service", "restart_service", "disable_failure_flags", "throttle_load"}
)
CATEGORY_ACTIONS = {
    "container_down": frozenset({"start_service", "restart_service"}),
    "dependency_unavailable": frozenset({"start_service", "restart_service"}),
    "app_error": frozenset({"disable_failure_flags", "restart_service"}),
    "resource_memory": frozenset({"restart_service"}),
    "resource_cpu": frozenset({"restart_service"}),
    "traffic_surge": frozenset({"throttle_load"}),
}
PREVENTION = {
    "container_down": "Add health checks and alert on unexpected container exits.",
    "dependency_unavailable": "Monitor dependency health and configure bounded retries.",
    "app_error": "Add regression tests for the failing request path.",
    "resource_memory": "Alert on memory growth before the service reaches its limit.",
    "resource_cpu": "Monitor sustained CPU saturation and review service capacity.",
    "traffic_surge": "Review capacity and traffic shaping for sustained request spikes.",
    "network_degradation": "Add latency monitoring between the affected services.",
    "upstream_propagation": "Improve dependency health monitoring and trace coverage.",
    "unknown": "Improve telemetry coverage so the cause can be identified safely.",
    "no_incident": "Continue monitoring service health and established baselines.",
}
ACTION_RISK = {
    "start_service": "low",
    "restart_service": "low",
    "disable_failure_flags": "medium",
    "throttle_load": "medium",
}
ROLLBACK_HINT = {
    "start_service": "Verify health after starting; escalate if it does not recover.",
    "restart_service": "Verify health after restarting; escalate if it does not recover.",
    "disable_failure_flags": "Restore the previous setting through the approved Action API if needed.",
    "throttle_load": "Restore the previous user count through the approved Action API if needed.",
}


def _container_state(run: RunLog, target: str) -> str | None:
    evidence = run.evidence_index()
    item = evidence.get(f"container:{target}")
    if not item:
        return None
    state = (item.get("text") or "").partition("state=")[2].partition(",")[0]
    return state.strip().lower() or None


def _current_users(run: RunLog) -> int | None:
    for event in reversed(run.events("action")):
        result = event.get("result") or event.get("action_result") or {}
        before = result.get("before") if isinstance(result, dict) else None
        users = before.get("users") if isinstance(before, dict) else None
        if isinstance(users, int) and not isinstance(users, bool) and users > 0:
            return users
    return None


def _fixed_action(
    category: str, component: str, run: RunLog
) -> tuple[str, dict] | None:
    if category in ("container_down", "dependency_unavailable"):
        action = (
            "start_service"
            if _container_state(run, component) == "exited"
            else "restart_service"
        )
        return action, {}
    if category == "app_error":
        action = "disable_failure_flags"
        if action not in ALLOWED_ACTIONS:
            action = "restart_service"
        return action, {}
    if category in ("resource_memory", "resource_cpu"):
        return "restart_service", {}
    if category == "traffic_surge":
        current = _current_users(run)
        if current is None:
            return None
        return "throttle_load", {"users": max(1, current // 2)}
    return None


def _valid_proposal(proposal: Proposal, category: str) -> bool:
    action = proposal["action"]
    if action not in ALLOWED_ACTIONS or action not in CATEGORY_ACTIONS.get(
        category, ()
    ):
        return False
    if not proposal["target"].strip() or proposal["risk"] not in ("low", "medium"):
        return False
    if not proposal["reason"].strip() or not proposal["expected_effect"].strip():
        return False
    if action == "throttle_load":
        users = proposal["params"].get("users")
        return (
            proposal["target"] == "load-generator"
            and isinstance(users, int)
            and users >= 1
        )
    return not proposal["params"]


def _proposal_text(
    d: Diagnosis, action: str, target: str, params: dict, run: RunLog, llm: LLM | None
) -> tuple[str, str] | None:
    settings = load_settings()
    try:
        writer = llm or LLM(settings)
        evidence = [
            scrub_flags({"id": item["id"], "text": (item.get("text") or "")[:240]})
            for item in list(run.evidence_index().values())[:8]
        ]
        prompt = PROPOSE.format(
            diagnosis=json.dumps(
                {
                    "component": d["component"],
                    "category": d["category"],
                    "cause": d["cause"][:400],
                    "confidence": d["confidence"],
                },
                ensure_ascii=False,
            ),
            action=action,
            target=target,
            params=json.dumps(params),
            evidence=json.dumps(evidence, ensure_ascii=False),
        )
        reply = writer.chat([{"role": "user", "content": prompt}], [])
    except Exception as exc:  # noqa: BLE001
        run.log("error", where="propose", error=str(exc))
        return None
    run.log(
        "llm_call",
        step="propose",
        model=getattr(reply, "model", ""),
        usage=getattr(reply, "usage", {}),
        tool_calls=getattr(reply, "tool_calls", []),
    )
    if getattr(reply, "tool_calls", []):
        run.log("error", where="propose", error="tool calls are not allowed")
        return None
    try:
        raw = json.loads(reply.text)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    reason, expected = raw.get("reason"), raw.get("expected_effect")
    if not isinstance(reason, str) or not isinstance(expected, str):
        return None
    return reason[:500].strip(), expected[:500].strip()


def propose(d: Diagnosis, run: RunLog, llm: LLM | None = None) -> Proposal | None:
    """Build one deterministic action proposal for an act diagnosis."""
    component = d["component"]
    if d["decision"] != "act" or not component or component in CLIENT_SERVICES:
        return None
    fixed = _fixed_action(d["category"], component, run)
    if fixed is None:
        return None
    action, params = fixed
    if action not in ALLOWED_ACTIONS:
        return None
    target = "load-generator" if action == "throttle_load" else component
    text = _proposal_text(d, action, target, params, run, llm)
    if text is None:
        return None
    reason, expected = text
    risk = "medium" if d["category"] == "resource_cpu" else ACTION_RISK[action]
    proposal: Proposal = {
        "action": action,
        "target": target,
        "params": params,
        "reason": reason,
        "expected_effect": expected,
        "rollback_hint": ROLLBACK_HINT[action],
        "risk": risk,
    }
    return proposal if _valid_proposal(proposal, d["category"]) else None


def _final_diagnosis(d: Diagnosis, review_result: dict) -> Diagnosis:
    final: Diagnosis = {**d}
    adjustment = review_result["confidence_adjustment"]
    unresolved = [
        challenge
        for challenge in review_result["challenges"]
        if not challenge["ruled_out"]
    ]
    final["confidence"] = round(max(0.0, min(1.0, d["confidence"] + adjustment)), 3)
    if unresolved or final["confidence"] < load_settings().CONFIDENCE_MIN:
        final["decision"] = "escalate"
    if unresolved:
        note = "Planner could not rule out: " + "; ".join(
            challenge["hypothesis"] for challenge in unresolved
        )
        final["reasoning"] = (final["reasoning"] + " [" + note + "]")[:800]
    return final


def build_plan(inv: Investigation, run: RunLog, llm: LLM | None = None) -> Plan:
    """Review an Investigation and return a Plan without executing its proposal."""
    run.log(
        "handoff",
        from_agent="detective",
        to_agent="planner",
        run_id=inv["run_id"],
        diagnosis_component=inv["diagnosis"]["component"],
    )
    review_result = review(inv, llm, run)
    diagnosis_final = _final_diagnosis(inv["diagnosis"], review_result)
    if not inv["evidence_valid"]:
        diagnosis_final["decision"] = "escalate"
        diagnosis_final["confidence"] = min(diagnosis_final["confidence"], 0.3)
        diagnosis_final["reasoning"] = (
            diagnosis_final["reasoning"]
            + " [Investigation evidence was not validated.]"
        )[:800]
    proposal = propose(diagnosis_final, run, llm)
    plan: Plan = {
        "review": review_result,
        "diagnosis_final": diagnosis_final,
        "proposal": proposal,
        "prevention": PREVENTION.get(
            diagnosis_final["category"], PREVENTION["unknown"]
        ),
    }
    run.log(
        "handoff",
        from_agent="planner",
        to_agent="human_gate",
        run_id=inv["run_id"],
        decision=diagnosis_final["decision"],
        proposal=proposal,
    )
    return plan

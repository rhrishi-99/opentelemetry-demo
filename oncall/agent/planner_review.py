"""Tool-free Planner review of an Investigation (F22)."""

import json
from dataclasses import replace

from oncall.agent.llm import LLM
from oncall.agent.prompts import REVIEW
from oncall.agent.runlog import RunLog
from oncall.config import load_settings
from oncall.models import Investigation, scrub_flags

MAX_REVIEW_COMPLETION_TOKENS = 900
MAX_REVIEW_EVIDENCE = 8
MAX_EVIDENCE_TEXT_CHARS = 240


def _fallback(reason: str) -> dict:
    return {
        "challenges": [
            {
                "hypothesis": "An alternative explanation could not be ruled out.",
                "ruled_out": False,
                "evidence_ids": [],
                "reason": reason[:300],
            }
        ],
        "confidence_adjustment": -0.1,
    }


def _bounded_llm(llm: LLM | None) -> LLM:
    settings = load_settings()
    settings = replace(
        settings,
        LLM_MAX_TOKENS=min(settings.LLM_MAX_TOKENS, MAX_REVIEW_COMPLETION_TOKENS),
    )
    if llm is None:
        return LLM(settings)
    if isinstance(llm, LLM):
        bounded = replace(
            llm.settings,
            LLM_MAX_TOKENS=min(
                llm.settings.LLM_MAX_TOKENS, MAX_REVIEW_COMPLETION_TOKENS
            ),
        )
        return LLM(bounded, llm.transport)
    return llm


def _prompt(inv: Investigation, run: RunLog) -> str:
    diagnosis = inv["diagnosis"]
    summary = {
        "component": diagnosis["component"],
        "category": diagnosis["category"],
        "cause": diagnosis["cause"][:400],
        "confidence": diagnosis["confidence"],
        "evidence_ids": diagnosis["evidence_ids"][:MAX_REVIEW_EVIDENCE],
        "alternatives": [
            {
                "hypothesis": item["hypothesis"][:250],
                "rejected_because": item["rejected_because"][:250],
            }
            for item in diagnosis["alternatives"][:2]
        ],
        "reasoning": diagnosis["reasoning"][:500],
        "evidence_valid": inv["evidence_valid"],
    }
    evidence = []
    for evidence_id, item in list(run.evidence_index().items())[:MAX_REVIEW_EVIDENCE]:
        safe = scrub_flags({"id": evidence_id, "text": item.get("text", "")})
        evidence.append(
            {"id": safe["id"], "text": str(safe["text"])[:MAX_EVIDENCE_TEXT_CHARS]}
        )
    return REVIEW.format(
        diagnosis=json.dumps(summary, ensure_ascii=False),
        evidence=json.dumps(evidence, ensure_ascii=False),
    )


def _normalise(raw: object, evidence_ids: set[str]) -> dict | None:
    if not isinstance(raw, dict):
        return None
    challenges = raw.get("challenges")
    if not isinstance(challenges, list) or not 1 <= len(challenges) <= 2:
        return None
    adjustment = raw.get("confidence_adjustment", 0)
    if isinstance(adjustment, bool) or not isinstance(adjustment, (int, float)):
        return None

    normalized = []
    for challenge in challenges:
        if not isinstance(challenge, dict):
            return None
        hypothesis = challenge.get("hypothesis")
        ruled_out = challenge.get("ruled_out")
        cited = challenge.get("evidence_ids")
        reason = challenge.get("reason", "")
        if (
            not isinstance(hypothesis, str)
            or not hypothesis.strip()
            or not isinstance(ruled_out, bool)
            or not isinstance(cited, list)
            or not all(isinstance(item, str) for item in cited)
            or not isinstance(reason, str)
        ):
            return None
        valid_ids = list(dict.fromkeys(item for item in cited if item in evidence_ids))
        if len(valid_ids) != len(cited) or (ruled_out and not valid_ids):
            ruled_out = False
        normalized.append(
            {
                "hypothesis": hypothesis[:300],
                "ruled_out": ruled_out,
                "evidence_ids": valid_ids,
                "reason": reason[:500],
            }
        )

    adjustment = max(-0.5, min(float(adjustment), 0.0))
    if any(not challenge["ruled_out"] for challenge in normalized):
        adjustment = min(adjustment, -0.1)
    return {"challenges": normalized, "confidence_adjustment": adjustment}


def review(
    inv: Investigation, llm: LLM | None = None, run: RunLog | None = None
) -> dict:
    """Challenge a diagnosis once using only cited, already-collected evidence."""
    settings = load_settings()
    run = run or RunLog(inv["run_id"], settings.RUNS_DIR)
    try:
        reviewer = _bounded_llm(llm)
        reply = reviewer.chat([{"role": "user", "content": _prompt(inv, run)}], [])
    except Exception as exc:  # noqa: BLE001
        run.log("error", where="planner_review", error=str(exc))
        return _fallback("The Planner review call failed.")

    run.log(
        "llm_call",
        step="planner_review",
        model=getattr(reply, "model", ""),
        usage=getattr(reply, "usage", {}),
        tool_calls=getattr(reply, "tool_calls", []),
    )
    if getattr(reply, "tool_calls", []):
        run.log("error", where="planner_review", error="tool calls are not allowed")
        return _fallback("The review returned a tool call, which is not allowed.")
    try:
        raw = json.loads(reply.text)
    except (TypeError, json.JSONDecodeError):
        raw = None
    checked = _normalise(raw, set(run.evidence_index()))
    return checked or _fallback("The review response was invalid.")

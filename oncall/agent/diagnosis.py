"""Diagnosis step: turn a finished investigation into a structured Diagnosis."""

import json
import re
from typing import get_args

from oncall.agent import taxonomy
from oncall.agent.evidence import service_of, supports_component
from oncall.agent.llm import LLM, LLMError
from oncall.agent.prompts import DIAGNOSE
from oncall.agent.runlog import RunLog
from oncall.models import Alert, Category, Diagnosis, llm_query
from oncall.safety.allowlist import DEMO_SERVICES
from oncall.safety.exclusions import INFRA_SERVICES

SUBMIT = "submit_diagnosis"
MAX_REASONING_CHARS = 800
# Forced submit_diagnosis calls, then one tool_choice="auto" try.
MAX_ATTEMPTS = 3
CATEGORY_NAMES = list(get_args(Category))
DECISIONS = ["act", "escalate", "no_action"]
FALLBACK_NOTE = "rule-based diagnosis (LLM unavailable)"
NUDGE = f"You MUST call {SUBMIT} now."
# Groq's 400 when a required tool call is missing or malformed.
TOOL_FAILURE = re.compile(r"tool_use_failed|did not call a tool")
# search_logs evidence starts with its line count: "x19 [cart] ...".
LOG_COUNT = re.compile(r"^x(\d+) ")
# get_metrics evidence: "<service> <metric> <now> (baseline <base>, ...)".
METRIC_TEXT = re.compile(
    r"^\S+ (?P<metric>\w+) (?P<now>[0-9.]+) \(baseline (?P<base>[0-9.]+)"
)
# Services whose error/latency regressions count as impact of a down dependency.
DEPENDENTS = {"valkey-cart": ("cart",)}
FALLBACK_CONFIDENCE = 0.75
FLAGGED = " | FLAGGED: "
# container_health problems that mean the container itself is down.
DOWN_PROBLEMS = ("not running", "restarted", "OOM-killed")

SUBMIT_SCHEMA = {
    "name": SUBMIT,
    "description": "Submit the structured diagnosis of the incident.",
    "input_schema": {
        "type": "object",
        "properties": {
            "component": {
                "type": ["string", "null"],
                "description": "Root-cause service or container, or null if unknown.",
            },
            "category": {"type": "string", "enum": CATEGORY_NAMES},
            "cause": {"type": "string", "description": "One specific sentence."},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "affected_services": {"type": "array", "items": {"type": "string"}},
            "alternatives": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "hypothesis": {"type": "string"},
                        "rejected_because": {"type": "string"},
                    },
                    "required": ["hypothesis", "rejected_because"],
                },
            },
            "decision": {"type": "string", "enum": DECISIONS},
            "reasoning": {"type": "string"},
        },
        "required": [
            "component",
            "category",
            "cause",
            "confidence",
            "evidence_ids",
            "affected_services",
            "alternatives",
            "decision",
            "reasoning",
        ],
    },
}


def escalate_unknown(reason: str, affected: list[str] | None = None) -> Diagnosis:
    """The safe fallback: no component, category unknown, escalate to a human."""
    return {
        "component": None,
        "category": "unknown",
        "cause": "Could not determine the root cause.",
        "confidence": 0.0,
        "evidence_ids": [],
        "affected_services": list(affected or []),
        "alternatives": [],
        "decision": "escalate",
        "reasoning": reason[:MAX_REASONING_CHARS],
    }


def down_containers(run: RunLog) -> dict[str, tuple[str, str]]:
    """{name: (evidence id, problems)} for demo containers container_health found
    not running, restarting or OOM-killed."""
    down = {}
    for eid, item in run.evidence_index().items():
        if not eid.startswith("container:"):
            continue
        name = eid.split(":", 1)[1]
        problems = (item.get("text") or "").partition(FLAGGED)[2]
        if name in DEMO_SERVICES and any(p in problems for p in DOWN_PROBLEMS):
            down[name] = (eid, problems)
    return down


def _regressed(text: str) -> bool:
    m = METRIC_TEXT.match(text)
    if not m:
        return False
    now, base = float(m["now"]), float(m["base"])
    return now > base and (m["metric"] != "error_rate" or now >= 0.01)


def support(name: str, index: dict) -> tuple[list[str], int, list[str]]:
    """(evidence ids, error count, calling services) pointing at a down container:
    its own container item, other services' logs/traces/metrics naming it or an
    alias, and regressions on the services that depend on it."""
    ids, errors, callers = [f"container:{name}"], 0, []
    for eid, item in index.items():
        kind = eid.split(":", 1)[0]
        if kind not in ("log", "trace", "metric"):
            continue
        text = item.get("text") or ""
        service = service_of({**item, "id": eid})
        if service == name:
            continue
        mentions = kind != "metric" and supports_component({**item, "id": eid}, name)
        depends = (
            kind == "metric"
            and service in DEPENDENTS.get(name, ())
            and _regressed(text)
        )
        if not (mentions or depends):
            continue
        ids.append(eid)
        if mentions:
            m = LOG_COUNT.match(text)
            errors += int(m.group(1)) if m else 1
            if service and service not in callers:
                callers.append(service)
    return ids, errors, callers


def _impact(name: str, errors: int, callers: list[str]) -> str:
    if callers:
        return f"{', '.join(callers)} can't reach it ({errors} errors)"
    return "no user-facing errors"


def _metric_regression(text: str) -> str | None:
    """The service whose error rate (>=3x, >=2%) or p95 (>=2x) regressed, if any."""
    m = METRIC_TEXT.match(text)
    if not m:
        return None
    now, base = float(m["now"]), float(m["base"])
    if m["metric"] == "error_rate":
        regressed = now >= 0.02 and now >= 3 * base
    elif m["metric"] == "latency_p95":
        regressed = base > 0 and now >= 2 * base
    else:
        regressed = False
    return text.split(" ", 1)[0].split("/", 1)[0] if regressed else None


def container_precedence(d: Diagnosis, run: RunLog) -> Diagnosis:
    """A single down demo container whose dependents show symptoms is the cause.

    Applies when exactly one demo container is down, restarting or OOM-killed and
    another service regressed or its logs/traces name the container (or an
    alias). The LLM's own pick becomes a rejected alternative."""
    down = down_containers(run)
    if len(down) != 1:
        return d
    ((name, (container_id, problems)),) = down.items()
    if d.get("component") == name:
        return d
    index = run.evidence_index()
    ids, _, callers = support(name, index)
    regressed = [
        svc
        for eid, item in index.items()
        if eid.startswith("metric:")
        and (svc := _metric_regression(item.get("text") or ""))
        and svc != name
        and svc not in taxonomy.CLIENT_SERVICES
        and svc not in INFRA_SERVICES
    ]
    dependents = list(dict.fromkeys([*callers, *regressed]))
    if not dependents:
        return d
    rejected = [
        a
        for a in d.get("alternatives") or []
        if name not in f"{a.get('hypothesis')} {a.get('rejected_because')}"
    ]
    if d.get("component"):
        rejected.insert(
            0,
            {
                "hypothesis": f"{d['component']} is the root cause ({d.get('category')})",
                "rejected_because": f"downstream symptom of {name} being down",
            },
        )
    regressed_ids = [
        eid
        for eid, item in index.items()
        if eid.startswith("metric:")
        and _metric_regression(item.get("text") or "") in dependents
    ]
    out: Diagnosis = {
        **d,
        "component": name,
        "category": "dependency_unavailable" if callers else "container_down",
        "cause": f"{name} is down ({problems}); "
        f"{', '.join(dependents)} show the symptoms",
        "confidence": max(float(d.get("confidence") or 0), FALLBACK_CONFIDENCE),
        "evidence_ids": list(
            dict.fromkeys(
                [container_id, *ids, *regressed_ids, *(d.get("evidence_ids") or [])]
            )
        ),
        "affected_services": dependents,
        "alternatives": rejected,
        "decision": "act",
        "reasoning": (
            f"[precedence rule applied: {name} is down, {', '.join(dependents)} "
            f"depend on it] {d.get('reasoning', '')}"
        )[:MAX_REASONING_CHARS],
    }
    run.log(
        "decision",
        stage="precedence",
        rule="precedence rule applied",
        container=name,
        replaced=d.get("component"),
        diagnosis=out,
    )
    return out


def rule_based(run: RunLog, alert: Alert, why: str) -> Diagnosis:
    """Diagnose from the run log alone when the LLM cannot.

    The down demo containers are ranked by the evidence pointing at them; the top
    one is the component. One down container may act (the F13/F15 rules decide);
    several always escalate."""
    index = run.evidence_index()
    down = down_containers(run)
    if not down:
        affected = [alert["service"]] if alert.get("service") else []
        return escalate_unknown(
            f"{FALLBACK_NOTE}: no down container in the run log. {why}", affected
        )
    scored = {name: support(name, index) for name in down}
    # Ties go to a container with user-facing dependents (valkey-cart -> cart).
    ranked = sorted(
        down,
        key=lambda n: (-len(scored[n][0]), -scored[n][1], n not in DEPENDENTS, n),
    )
    top = ranked[0]
    ids, errors, callers = scored[top]
    category = "dependency_unavailable" if callers else "container_down"
    cause = f"{top} down → {_impact(top, errors, callers)}"
    for other in ranked[1:]:
        _, other_errors, other_callers = scored[other]
        cause += f"; {other} also down ({_impact(other, other_errors, other_callers)})"
    evidence = list(
        dict.fromkeys([*ids, *(i for n in ranked[1:] for i in scored[n][0])])
    )
    note = (
        f"exactly one demo container is down ({top})"
        if len(down) == 1
        else f"{len(down)} demo containers are down; {top} has the most evidence"
    )
    return {
        "component": top,
        "category": category,
        "cause": cause,
        "confidence": FALLBACK_CONFIDENCE,
        "evidence_ids": [e for e in evidence if e in index],
        "affected_services": ranked,
        "alternatives": [],
        "decision": "act" if len(down) == 1 else "escalate",
        "reasoning": f"{FALLBACK_NOTE}: {note}. {why}"[:MAX_REASONING_CHARS],
    }


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def check(raw: object) -> list[str]:
    """Problems with a submit_diagnosis payload; empty when it is valid."""
    if not isinstance(raw, dict):
        return ["the diagnosis must be a JSON object"]
    problems = []
    missing = [k for k in SUBMIT_SCHEMA["input_schema"]["required"] if k not in raw]
    if missing:
        problems.append(f"missing fields: {', '.join(missing)}")
    if "component" in raw and not (
        raw["component"] is None or isinstance(raw["component"], str)
    ):
        problems.append("component must be a string or null")
    if "category" in raw and raw["category"] not in CATEGORY_NAMES:
        problems.append(f"category must be one of: {', '.join(CATEGORY_NAMES)}")
    if "decision" in raw and raw["decision"] not in DECISIONS:
        problems.append("decision must be 'act', 'escalate' or 'no_action'")
    if "confidence" in raw:
        c = raw["confidence"]
        if isinstance(c, bool) or not isinstance(c, (int, float)) or not 0 <= c <= 1:
            problems.append("confidence must be a number from 0 to 1")
    for key in ("cause", "reasoning"):
        if key in raw and not isinstance(raw[key], str):
            problems.append(f"{key} must be a string")
    for key in ("evidence_ids", "affected_services"):
        if key in raw and not _is_str_list(raw[key]):
            problems.append(f"{key} must be a list of strings")
    if "alternatives" in raw:
        alts = raw["alternatives"]
        if not isinstance(alts, list) or not all(
            isinstance(a, dict)
            and isinstance(a.get("hypothesis"), str)
            and isinstance(a.get("rejected_because"), str)
            for a in alts
        ):
            problems.append(
                "alternatives must be a list of {hypothesis, rejected_because} strings"
            )
    return problems


def _post_check(raw: dict) -> Diagnosis:
    """Normalise a valid payload and enforce the rules the LLM may not break."""
    d: Diagnosis = {
        "component": raw["component"] or None,
        "category": raw["category"],
        "cause": raw["cause"],
        "confidence": float(raw["confidence"]),
        "evidence_ids": list(dict.fromkeys(raw["evidence_ids"])),
        "affected_services": list(dict.fromkeys(raw["affected_services"])),
        "alternatives": [
            {"hypothesis": a["hypothesis"], "rejected_because": a["rejected_because"]}
            for a in raw["alternatives"]
        ],
        "decision": raw["decision"],
        "reasoning": raw["reasoning"],
    }
    if d["component"] in (*taxonomy.CLIENT_SERVICES, *INFRA_SERVICES):
        client = d["component"]
        kind = "a client" if client in taxonomy.CLIENT_SERVICES else "telemetry infra"
        d["component"] = None
        d["decision"] = "escalate"
        if client not in d["affected_services"]:
            d["affected_services"].append(client)
        d["reasoning"] = (
            f"[post-check: {client} is {kind}, so it cannot be the root cause] "
            + d["reasoning"]
        )
    d["reasoning"] = d["reasoning"][:MAX_REASONING_CHARS]
    return d


def _tool_log(run: RunLog) -> str:
    lines = []
    for i, e in enumerate(run.events("tool_result"), 1):
        r = e.get("result") or {}
        status = "ok" if r.get("ok") else f"error: {r.get('error')}"
        lines.append(
            f"{i}. {e.get('name')}({json.dumps(llm_query(r.get('query') or {}))}) -> {status}: "
            f"{r.get('summary', '')}"
        )
    return "\n".join(lines) or "(no tool calls)"


def _evidence(run: RunLog) -> str:
    items = run.evidence_index().values()
    return "\n".join(f"- {e['id']}: {e.get('text', '')}" for e in items) or "(none)"


def _summary(run: RunLog) -> str:
    finishes = run.events("finish")
    return (finishes[-1].get("summary") if finishes else "") or "(no summary)"


def build_prompt(run: RunLog, alert: Alert) -> str:
    return DIAGNOSE.format(
        alert_id=alert["id"],
        alert_ts=alert["ts"],
        alert_service=alert["service"],
        alert_signal=alert["signal"],
        alert_description=alert["description"],
        summary=_summary(run),
        tool_log=_tool_log(run),
        evidence=_evidence(run),
        taxonomy=taxonomy.render(),
    )


def _fallback(run: RunLog, alert: Alert, why: str) -> Diagnosis:
    d = rule_based(run, alert, why)
    run.log("decision", diagnosis=d, valid=False)
    return d


def _json_object(text: str) -> dict | None:
    """The first {...} object in a text reply, unwrapping {"arguments": {...}}."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if isinstance(value, dict) and isinstance(value.get("arguments"), dict):
        value = value["arguments"]
    return value if isinstance(value, dict) else None


def _submitted(reply, allow_text: bool = False) -> tuple[dict | None, list[str]]:
    """(valid diagnosis args or None, problems) from one reply."""
    calls = [c for c in reply.tool_calls if c["name"] == SUBMIT]
    if calls:
        args = calls[0]["args"]
        if args is None:
            return None, [calls[0].get("error", "arguments missing")]
    elif allow_text and (args := _json_object(reply.text or "")) is not None:
        pass
    else:
        return None, [f"you must call {SUBMIT}; got no {SUBMIT} call"]
    problems = check(args)
    return (None, problems) if problems else (args, [])


def diagnose(run: RunLog, alert: Alert, llm: LLM) -> Diagnosis:
    """Ask the LLM for a Diagnosis via a forced submit_diagnosis call.

    A missing or invalid call (including Groq's 400 tool_use_failed) is retried
    with a nudge, then tried once with tool_choice="auto" reading JSON from the
    text. Anything else, or if that fails too, falls back to rule_based(). Never
    raises.
    """
    messages = [{"role": "user", "content": build_prompt(run, alert)}]
    problems: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            reply = llm.chat(messages, [SUBMIT_SCHEMA])
        except LLMError as exc:
            run.log("error", where="diagnose", attempt=attempt, error=str(exc))
            if not TOOL_FAILURE.search(f"{exc} {exc.body}"):
                return _fallback(run, alert, f"LLM call failed during diagnosis: {exc}")
            problems = [f"no {SUBMIT} call: {str(exc)[:200]}"]
            messages.append({"role": "user", "content": NUDGE})
            continue
        except Exception as exc:  # noqa: BLE001
            run.log("error", where="diagnose", attempt=attempt, error=str(exc))
            return _fallback(run, alert, f"LLM call failed during diagnosis: {exc}")
        run.log(
            "llm_call",
            step="diagnose",
            attempt=attempt,
            model=reply.model,
            usage=reply.usage,
        )
        args, problems = _submitted(reply)
        if args is not None:
            d = _post_check(args)
            run.log("decision", diagnosis=d, valid=True, attempt=attempt)
            return d
        run.log("error", where="diagnose", attempt=attempt, problems=problems)
        messages.append(
            {
                "role": "user",
                "content": f"Your diagnosis was invalid: {'; '.join(problems)}. "
                f"Call {SUBMIT} again with every field valid. {NUDGE}",
            }
        )

    try:
        reply = llm.chat(messages, [SUBMIT_SCHEMA], tool_choice="auto")
        args, auto_problems = _submitted(reply, allow_text=True)
    except Exception as exc:  # noqa: BLE001
        run.log("error", where="diagnose", attempt="auto", error=str(exc))
        args, auto_problems = None, []
    if args is not None:
        d = _post_check(args)
        run.log("decision", diagnosis=d, valid=True, attempt="auto")
        return d
    return _fallback(
        run,
        alert,
        f"Diagnosis output invalid after {MAX_ATTEMPTS} attempts: "
        f"{'; '.join(problems or auto_problems)}",
    )

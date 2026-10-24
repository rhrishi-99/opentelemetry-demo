"""Engineer-readable incident summary, built deterministically from a diagnosis and
its run log (no LLM). The UI card and the Markdown report both render it."""

import re
from datetime import UTC, datetime

from oncall.agent.evidence import service_of, supports_component
from oncall.agent.taxonomy import CLIENT_SERVICES

CATEGORY_WORDS = {
    "container_down": "is down",
    "dependency_unavailable": "dependency unavailable",
    "app_error": "application errors",
    "resource_cpu": "CPU saturation",
    "resource_memory": "memory pressure",
    "network_degradation": "network degradation",
    "traffic_surge": "traffic surge",
    "upstream_propagation": "errors propagated from a dependency",
    "no_incident": "healthy",
    "unknown": "cause unclear",
}
UNKNOWN_BY_CATEGORY = {
    "container_down": "why {component} stopped",
    "dependency_unavailable": "why {component} cannot reach its dependency",
    "app_error": "which change introduced the errors in {component}",
    "resource_cpu": "what drove the CPU load on {component}",
    "resource_memory": "what is using the memory in {component}",
    "network_degradation": "which network path to {component} is degraded",
    "traffic_surge": "where the extra traffic comes from",
    "upstream_propagation": "which dependency started failing first",
    "unknown": "the root cause",
}
USER_IMPACT = {
    "cart": "users can't add to cart",
    "valkey-cart": "users can't add to cart",
    "checkout": "orders failing",
    "payment": "orders failing",
    "frontend": "pages slow/erroring",
    "frontend-proxy": "pages slow/erroring",
    "product-catalog": "product pages failing",
    "recommendation": "page features degraded",
    "ad": "page features degraded",
    "email": "order emails not sent",
}
SOURCE_LABELS = {
    "container": "Containers",
    "log": "Logs",
    "trace": "Traces",
    "metric": "Metrics",
    "deploy": "Deploys",
}
# Strongest evidence first when describing what is broken.
SOURCE_STRENGTH = ("container", "log", "trace", "metric", "deploy")
METRIC_WORDS = {"error_rate": "errors", "latency_p95": "p95", "rps": "traffic"}
MAX_WHY = 3
MAX_IMPACT = 3
MAX_NOT_CAUSE = 2
MAX_USER_IMPACTS = 2
LINE_CHARS = 110
# get_metrics evidence: "<service> <metric> <now> (baseline <base>, delta ...)".
METRIC_TEXT = re.compile(
    r"^(?P<service>\S+) (?P<metric>\w+) (?P<now>[0-9.]+) \(baseline (?P<base>[0-9.]+)"
)
# search_logs evidence: "x<count> [<service>] <first line>".
LOG_TEXT = re.compile(r"^x(?P<count>\d+) \[(?P<service>[^\]]+)\] (?P<line>.*)$")
# The "[confidence: note; note]" suffix the confidence rules add to reasoning.
CONFIDENCE_SUFFIX = re.compile(r"\s*\[confidence: (?P<notes>[^\]]*)\]\s*$")
# A sentence that admits something is not known.
UNKNOWN_WORDS = re.compile(
    r"\b(unknown|unclear|could not determine|not determined|undetermined)\b",
    re.IGNORECASE,
)
# Sentence boundary: ., ! or ? followed by whitespace.
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# An ISO timestamp: groups 1-5 = year, month, day, hour, minute (UTC assumed).
ISO_TS = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})[0-9:.]*(Z|[+-]\d{2}:?\d{2})?"
)
MAX_UNKNOWN_CHARS = 140
FLAGGED = " | FLAGGED: "


def fmt_rate(value: float) -> str:
    pct = value * 100
    return f"{pct:.0f}%" if pct >= 1 or pct == 0 else f"{pct:.1f}%"


def fmt_ms(value: float) -> str:
    if value >= 10_000:
        return f"{value / 1000:.0f} s"
    if value >= 1000:
        return f"{value / 1000:.1f} s"
    return f"{value:.0f} ms"


def fmt_metric(metric: str, value: float) -> str:
    if metric == "error_rate":
        return fmt_rate(value)
    if metric.startswith("latency"):
        return fmt_ms(value)
    return f"{value:.1f} rps"


def hhmm(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        return f"{datetime.fromisoformat(ts).astimezone():%H:%M}"
    except ValueError:
        return None


def _iso_to_hhmm(m: re.Match) -> str:
    y, mo, d, h, mi = (int(g) for g in m.groups()[:5])
    return f"{datetime(y, mo, d, h, mi, tzinfo=UTC).astimezone():%H:%M}"


def _short(text: str, limit: int = LINE_CHARS) -> str:
    text = ISO_TS.sub(_iso_to_hhmm, " ".join((text or "").split()))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def confidence_notes(reasoning: str) -> tuple[str, list[str]]:
    """Split the "[confidence: a; b]" suffix the confidence rules append."""
    m = CONFIDENCE_SUFFIX.search(reasoning or "")
    if not m:
        return (reasoning or "").strip(), []
    notes = [n.strip() for n in m["notes"].split(";") if n.strip()]
    return reasoning[: m.start()].strip(), notes


def flagged_containers(index: dict, down_only: bool = False) -> list[str]:
    """Containers container_health flagged (or, with down_only, found not running)."""
    names = []
    for eid, item in index.items():
        text = item.get("text") or ""
        if not eid.startswith("container:") or FLAGGED not in text:
            continue
        if down_only and "state=running" in text:
            continue
        names.append(eid.split(":", 1)[1])
    return names


def components(d: dict, index: dict | None = None) -> list[str]:
    """The failing components: several for a combined fault, else the one named.

    With no component named, two or more down containers in the evidence count
    as a combined fault."""
    _, notes = confidence_notes(d.get("reasoning", ""))
    affected = [s for s in d.get("affected_services") or [] if s not in CLIENT_SERVICES]
    combo = any(n.startswith("more than one unhealthy container") for n in notes) or (
        not d.get("component")
        and len(affected) > 1
        and d.get("category") in (None, "unknown")
    )
    if combo:
        return list(
            dict.fromkeys(
                [*([d["component"]] if d.get("component") else []), *affected]
            )
        )
    if d.get("component"):
        return [d["component"]]
    if d.get("decision") == "no_action":
        return []
    down = flagged_containers(index or {}, down_only=True)
    if len(down) < 2:
        return []
    return list(dict.fromkeys([*down, *flagged_containers(index or {})]))


def _container_line(name: str, text: str) -> str:
    """'valkey-cart: state=exited, ..., exit_code=0 | FLAGGED: ...' -> plain words."""
    fields = dict(re.findall(r"(\w+)=([^,|]+)", text.partition(FLAGGED)[0]))
    state = fields.get("state", "").strip()
    problems = text.partition(FLAGGED)[2]
    if state and state != "running":
        detail = (
            [f"exit {fields['exit_code'].strip()}"] if "exit_code" in fields else []
        )
        if fields.get("health", "").strip() not in ("", "none", "healthy"):
            detail.append(fields["health"].strip())
        return f"{name} container {state}" + (
            f" ({', '.join(detail)})" if detail else ""
        )
    if problems:
        return f"{name} container: {_short(problems, 80)}"
    return f"{name} container {state or 'running'}"


def short_evidence(eid: str, text: str) -> str:
    """One readable line per evidence item."""
    kind = eid.split(":", 1)[0]
    if kind == "container":
        name = eid.split(":", 1)[1]
        return _container_line(name, text)
    if kind == "log" and (m := LOG_TEXT.match(text or "")):
        return f'{m["service"]}: "{_short(m["line"], 70)}" ×{m["count"]}'
    if kind == "metric" and (m := METRIC_TEXT.match(text or "")):
        metric = m["metric"]
        words = METRIC_WORDS.get(metric, metric).replace("errors", "error rate")
        return (
            f"{m['service']} {words} {fmt_metric(metric, float(m['now']))} "
            f"(baseline {fmt_metric(metric, float(m['base']))})"
        )
    return _short(text, 90)


def _about(evidence: dict, eid: str, name: str) -> bool:
    item = {"id": eid, **evidence}
    return service_of(item) == name or supports_component(item, name)


def _broken(d: dict, names: list[str], index: dict) -> list[str]:
    lines = []
    for name in names:
        about = [eid for eid in index if _about(index[eid], eid, name)]
        about.sort(
            key=lambda e: (
                SOURCE_STRENGTH.index(e.split(":", 1)[0])
                if e.split(":", 1)[0] in SOURCE_STRENGTH
                else len(SOURCE_STRENGTH)
            )
        )
        if about:
            lines.append(short_evidence(about[0], index[about[0]].get("text", "")))
        else:
            lines.append(
                f"{name}: {CATEGORY_WORDS.get(d.get('category'), 'cause unclear')}"
            )
    if not lines and d.get("decision") != "no_action":
        lines = [
            short_evidence(f"container:{name}", index[f"container:{name}"]["text"])
            for name in flagged_containers(index)
        ]
    if not lines and d.get("component") and d.get("cause"):
        lines.append(_short(d["cause"]))
    return lines


def _impact(d: dict, names: list[str], index: dict) -> list[str]:
    scope = set(names) | set(d.get("affected_services") or [])
    changes = []
    for eid, item in index.items():
        if not eid.startswith("metric:"):
            continue
        m = METRIC_TEXT.match(item.get("text") or "")
        if not m or m["service"] in CLIENT_SERVICES:
            continue
        now, base, metric = float(m["now"]), float(m["base"]), m["metric"]
        worse = now > base * 1.5 if metric == "rps" else now > base
        if not worse or (metric == "error_rate" and now < 0.01):
            continue
        ratio = (now + 1e-9) / (base + 1e-9)
        changes.append((m["service"] in scope, ratio, m["service"], metric, base, now))
    changes.sort(key=lambda c: (not c[0], -c[1]))
    lines = [
        f"{svc} {METRIC_WORDS.get(metric, metric)} {fmt_metric(metric, base)} → "
        f"{fmt_metric(metric, now)}"
        for _, _, svc, metric, base, now in changes[:MAX_IMPACT]
    ]
    candidates = [*names, *(c[2] for c in changes), *(d.get("affected_services") or [])]
    impacts = list(
        dict.fromkeys(USER_IMPACT[x] for x in candidates if x in USER_IMPACT)
    )
    # One user-facing line per failing component for a combined fault, else one.
    return lines + impacts[: max(1, min(len(names), MAX_USER_IMPACTS))]


def _worse(eid: str, text: str) -> bool:
    """False for a metric that did not get worse than its baseline."""
    m = METRIC_TEXT.match(text) if eid.startswith("metric:") else None
    return m is None or float(m["now"]) > float(m["base"])


def _why(d: dict, names: list[str], index: dict) -> list[dict]:
    cited = [eid for eid in d.get("evidence_ids") or [] if eid in index]
    pool = cited or list(index)
    pool.sort(
        key=lambda e: (
            not any(_about(index[e], e, n) for n in names),
            SOURCE_STRENGTH.index(e.split(":", 1)[0])
            if e.split(":", 1)[0] in SOURCE_STRENGTH
            else len(SOURCE_STRENGTH),
        )
    )
    picked, kinds = [], set()
    for eid in pool:
        kind = eid.split(":", 1)[0]
        if kind in kinds or not _worse(eid, index[eid].get("text") or ""):
            continue
        kinds.add(kind)
        text = index[eid].get("text", "")
        picked.append(
            {
                "source": SOURCE_LABELS.get(kind, "Evidence"),
                "text": short_evidence(eid, text),
                "id": eid,
                "full": text,
            }
        )
        if len(picked) == MAX_WHY:
            break
    return picked


def _not_cause(d: dict, names: list[str], index: dict) -> list[str]:
    items = []
    for eid, item in index.items():
        if not eid.startswith("container:"):
            continue
        name = eid.split(":", 1)[1]
        text = item.get("text") or ""
        if (
            name in (d.get("affected_services") or [])
            and name not in names
            and FLAGGED not in text
            and "state=running" in text
        ):
            items.append(f"{name} container healthy")
    for a in d.get("alternatives") or []:
        items.append(_short(f"{a.get('hypothesis')} — {a.get('rejected_because')}", 90))
    return items[:MAX_NOT_CAUSE]


def _unknown(d: dict, names: list[str], index: dict, summary: str) -> str | None:
    if d.get("decision") == "no_action":
        return None
    reasoning, _ = confidence_notes(d.get("reasoning", ""))
    for text in (summary, reasoning):
        for sentence in SENTENCE_END.split(text or ""):
            sentence = sentence.strip()
            if (
                UNKNOWN_WORDS.search(sentence)
                and len(sentence) <= MAX_UNKNOWN_CHARS
                and sentence[:1].isupper()
                and sentence.endswith((".", "!", "?"))
                and sentence.count("(") == sentence.count(")")
            ):
                return sentence.rstrip(".")
    component = names[0] if names else (d.get("component") or "the service")
    default = UNKNOWN_BY_CATEGORY.get(d.get("category") or "unknown")
    if not default:
        return None
    line = default.format(component=component)
    container = index.get(f"container:{component}", {}).get("text", "")
    if d.get("category") == "container_down" and "exit_code=0" in container:
        line += " (exit 0 = clean stop)"
    return line


def _status(
    events: list[dict], approval: dict | None, verification: dict | None
) -> str:
    if verification:
        outcome = verification.get("outcome")
        if outcome == "recovered":
            return "verified ✅"
        if outcome == "not_recovered_rolled_back" or verification.get("rolled_back"):
            return "rolled back"
        if outcome == "skipped_dry_run":
            return "dry run only"
        if outcome:
            return "not recovered — escalated"
    for e in reversed(events):
        result = e.get("result") if isinstance(e.get("result"), dict) else e
        if e.get("event") == "action" and result.get("executed"):
            return "executed"
    if approval:
        return "approved" if approval.get("approved") else "rejected"
    return "awaiting approval"


def _next(
    d: dict,
    names: list[str],
    index: dict,
    events: list[dict],
    proposal: dict | None,
    approval: dict | None,
    verification: dict | None,
) -> list[str]:
    decision = d.get("decision")
    if decision == "no_action":
        return ["Keep monitoring"]
    if proposal:
        status = _status(events, approval, verification)
        action = f"{proposal.get('action')}({proposal.get('target')})"
        return [f"▶ Proposed: {action} · risk {proposal.get('risk', '?')} — {status}"]
    targets = names or [
        s for s in d.get("affected_services") or [] if s not in CLIENT_SERVICES
    ]
    if not names:
        targets = list(dict.fromkeys([*flagged_containers(index), *targets]))
    steps = []
    reasoning, _ = confidence_notes(d.get("reasoning", ""))
    if reasoning.startswith(("LLM call failed", "rule-based fallback")):
        steps.append("LLM was unavailable: re-run the investigation when it is back")
    steps.append(
        f"Check: {', then '.join(targets)}" if targets else "Check the evidence below"
    )
    return steps


def find_proposal(events: list[dict]) -> dict | None:
    for e in reversed(events):
        for value in (e.get("proposal"), (e.get("plan") or {}).get("proposal")):
            if isinstance(value, dict) and value.get("action"):
                return value
    return None


def find_approval(events: list[dict]) -> dict | None:
    for e in reversed(events):
        if isinstance(e.get("approval"), dict):
            return e["approval"]
    return None


def find_verification(events: list[dict]) -> dict | None:
    for e in reversed(events):
        if e.get("event") == "verification" and "outcome" in e:
            return e
        if isinstance(e.get("verification"), dict) and e["verification"]:
            return e["verification"]
    return None


def build_summary(
    d: dict,
    events: list[dict],
    index: dict[str, dict],
    *,
    proposal: dict | None = None,
    approval: dict | None = None,
    verification: dict | None = None,
) -> dict:
    """The summary lines for a diagnosis; empty lines are left out by the renderers."""
    names = components(d, index)
    decision = d.get("decision") or "escalate"
    if decision == "no_action":
        what = "No incident — system looks healthy"
    elif len(names) > 1:
        what = f"Multiple failures: {', '.join(names)}"
    elif names:
        words = CATEGORY_WORDS.get(d.get("category") or "unknown", "cause unclear")
        what = (
            f"{names[0]} {words}"
            if words.startswith("is ")
            else f"{names[0]} — {words}"
        )
    else:
        what = CATEGORY_WORDS.get(d.get("category") or "unknown", "cause unclear")
        what = what[0].upper() + what[1:]
    alerts = [e for e in events if e.get("event") == "alert"]
    start = hhmm(
        ((alerts[0].get("alert") or {}).get("ts") or alerts[0].get("ts"))
        if alerts
        else None
    )
    ends = [e for e in events if e.get("event") in ("decision", "finish")]
    end = hhmm(ends[-1].get("ts")) if ends else None
    finishes = [e for e in events if e.get("event") == "finish" and e.get("summary")]
    summary = finishes[-1]["summary"] if finishes else ""
    proposal = proposal or find_proposal(events)
    approval = approval or find_approval(events)
    verification = verification or find_verification(events)
    confidence = min(max(float(d.get("confidence") or 0), 0.0), 1.0)
    return {
        "tone": "gray" if decision == "no_action" else "red",
        "headline": what,
        "decision": decision,
        "confidence": confidence,
        "times": " → ".join(t for t in (start, end) if t),
        "broken": _broken(d, names, index),
        "impact": _impact(d, names, index),
        "why": _why(d, names, index),
        "not_cause": _not_cause(d, names, index),
        "unknown": _unknown(d, names, index, summary),
        "next": _next(d, names, index, events, proposal, approval, verification),
    }


def headline_text(s: dict) -> str:
    icon = "⚪" if s["tone"] == "gray" else "🔴"
    parts = [
        f"{icon} {s['headline']}",
        f"{s['decision'].replace('_', ' ').upper()} ({s['confidence']:.0%})",
    ]
    if s["times"]:
        parts.append(s["times"])
    return "  ·  ".join(parts)


def rows(s: dict) -> list[tuple[str, list[str]]]:
    """(label, lines) for each non-empty summary line, in display order."""
    out = [
        ("What's broken", s["broken"]),
        ("Impact", [" · ".join(s["impact"])] if s["impact"] else []),
        ("Why we think so", [f"[{w['source']}] {w['text']}" for w in s["why"]]),
        ("Not the cause", [" · ".join(s["not_cause"])] if s["not_cause"] else []),
        ("Unknown", [s["unknown"]] if s["unknown"] else []),
        ("Next step", s["next"]),
    ]
    return [(label, lines) for label, lines in out if lines]


def to_markdown(s: dict) -> str:
    lines = [
        "## Incident summary",
        "",
        f"**{headline_text(s)}**",
        "",
        "| | |",
        "|---|---|",
    ]
    for label, values in rows(s):
        cell = "<br>".join(
            ("• " if label == "Why we think so" else "") + v.replace("|", "\\|")
            for v in values
        )
        lines.append(f"| **{label}** | {cell} |")
    return "\n".join(lines) + "\n"

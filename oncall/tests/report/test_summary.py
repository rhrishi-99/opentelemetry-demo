from oncall.report import summary

CONTAINER_DOWN = (
    "valkey-cart: state=exited, health=unhealthy, restarts=0, exit_code=0, "
    "oom_killed=false | FLAGGED: not running (state=exited, exit_code=0); "
    "healthcheck unhealthy"
)
INDEX = {
    "container:valkey-cart": {"text": CONTAINER_DOWN},
    "container:cart": {"text": "cart: state=running, health=healthy, restarts=0"},
    "log:cart:1@t": {
        "text": "x7 [cart] Wasn't able to connect to redis at valkey-cart"
    },
    "metric:error_rate:cart@t": {
        "text": "cart error_rate 0.1176 (baseline 0.0000, delta +0.1176)"
    },
    "metric:latency_p95:cart@t": {
        "text": "cart latency_p95 15000.0000 (baseline 5.8700, delta +14994.1300)"
    },
    "metric:latency_p95:ad@t": {
        "text": "ad latency_p95 3.0000 (baseline 4.0000, delta -1.0000)"
    },
}
EVENTS = [
    {"event": "alert", "ts": "2026-10-24T14:02:10+00:00", "alert": {}},
    {"event": "finish", "ts": "2026-10-24T14:04:00+00:00", "summary": "cart errors"},
    {"event": "decision", "ts": "2026-10-24T14:05:30+00:00"},
]


def diagnosis(**kw):
    d = {
        "component": "valkey-cart",
        "category": "container_down",
        "cause": "valkey-cart stopped",
        "confidence": 0.5,
        "evidence_ids": [
            "container:valkey-cart",
            "log:cart:1@t",
            "metric:error_rate:cart@t",
        ],
        "affected_services": ["valkey-cart", "cart"],
        "alternatives": [
            {"hypothesis": "cart bug", "rejected_because": "cart cannot reach redis"}
        ],
        "decision": "escalate",
        "reasoning": "valkey-cart is down",
    }
    return {**d, **kw}


def test_escalate_container_down():
    s = summary.build_summary(diagnosis(), EVENTS, INDEX)
    assert s["headline"] == "valkey-cart is down"
    assert s["tone"] == "red"
    assert s["broken"] == ["valkey-cart container exited (exit 0, unhealthy)"]
    assert s["impact"] == [
        "cart errors 0% → 12%",
        "cart p95 6 ms → 15 s",
        "users can't add to cart",
    ]
    assert [(w["source"], w["text"]) for w in s["why"]] == [
        ("Containers", "valkey-cart container exited (exit 0, unhealthy)"),
        ("Logs", 'cart: "Wasn\'t able to connect to redis at valkey-cart" ×7'),
        ("Metrics", "cart error rate 12% (baseline 0%)"),
    ]
    assert s["not_cause"] == [
        "cart container healthy",
        "cart bug — cart cannot reach redis",
    ]
    assert s["unknown"] == "why valkey-cart stopped (exit 0 = clean stop)"
    assert s["next"] == ["Check: valkey-cart"]
    head = summary.headline_text(s)
    assert head.startswith("🔴 valkey-cart is down  ·  ESCALATE (50%)  ·  ")
    assert "T" not in s["times"] and "→" in s["times"]


def test_act_with_proposal_shows_status():
    events = [
        *EVENTS,
        {
            "event": "handoff",
            "proposal": {
                "action": "start_service",
                "target": "valkey-cart",
                "risk": "low",
            },
        },
    ]
    s = summary.build_summary(diagnosis(decision="act", confidence=0.8), events, INDEX)
    assert s["next"] == [
        "▶ Proposed: start_service(valkey-cart) · risk low — awaiting approval"
    ]
    approved = summary.build_summary(
        diagnosis(decision="act"), events, INDEX, approval={"approved": True}
    )
    assert approved["next"][0].endswith("— approved")
    verified = summary.build_summary(
        diagnosis(decision="act"),
        events,
        INDEX,
        verification={"outcome": "recovered"},
    )
    assert verified["next"][0].endswith("— verified ✅")


def test_no_action_is_gray_and_minimal():
    s = summary.build_summary(
        diagnosis(
            decision="no_action",
            component=None,
            category="no_incident",
            affected_services=[],
            alternatives=[],
        ),
        EVENTS,
        {},
    )
    assert s["tone"] == "gray"
    assert s["headline"] == "No incident — system looks healthy"
    assert s["next"] == ["Keep monitoring"]
    assert s["unknown"] is None and s["broken"] == []
    assert [label for label, _ in summary.rows(s)] == ["Next step"]


def test_combo_lists_every_failing_component():
    index = {
        **INDEX,
        "container:email": {
            "text": "email: state=exited, exit_code=137 | FLAGGED: not running"
        },
    }
    d = diagnosis(
        component=None,
        category="unknown",
        affected_services=["valkey-cart", "email", "load-generator"],
        reasoning="rule-based fallback (LLM unavailable): 2 demo containers are down",
    )
    s = summary.build_summary(d, EVENTS, index)
    assert s["headline"] == "Multiple failures: valkey-cart, email"
    assert s["broken"] == [
        "valkey-cart container exited (exit 0, unhealthy)",
        "email container exited (exit 137)",
    ]
    assert s["next"] == [
        "LLM was unavailable: re-run the investigation when it is back",
        "Check: valkey-cart, then email",
    ]


def test_missing_metrics_and_evidence_omit_lines():
    d = diagnosis(evidence_ids=[], alternatives=[], affected_services=["valkey-cart"])
    s = summary.build_summary(d, [], {})
    assert s["impact"] == ["users can't add to cart"]
    assert s["why"] == [] and s["not_cause"] == [] and s["times"] == ""
    labels = [label for label, _ in summary.rows(s)]
    assert "Why we think so" not in labels and "Not the cause" not in labels
    assert s["broken"] == ["valkey-cart: is down"]


def test_number_formats():
    assert summary.fmt_rate(0.1176) == "12%"
    assert summary.fmt_rate(0.0034) == "0.3%"
    assert summary.fmt_ms(15000) == "15 s"
    assert summary.fmt_ms(6833.3) == "6.8 s"
    assert summary.fmt_ms(5.87) == "6 ms"


def test_markdown_block():
    md = summary.to_markdown(summary.build_summary(diagnosis(), EVENTS, INDEX))
    assert md.startswith("## Incident summary\n\n**🔴 valkey-cart is down")
    assert (
        "| **What's broken** | valkey-cart container exited (exit 0, unhealthy) |" in md
    )
    assert "| **Why we think so** | • [Containers]" in md


def test_unknown_with_down_containers_in_evidence_is_a_combo():
    index = {
        "container:email": {"text": "email: state=exited, exit_code=0 | FLAGGED: x"},
        "container:valkey-cart": {"text": CONTAINER_DOWN},
        "metric:error_rate:load-generator@t": {
            "text": "load-generator error_rate 0.1200 (baseline 0.0300, delta +0.09)"
        },
        "deploy:valkey-cart@t": {
            "text": "valkey-cart: restarted at 2026-10-24T06:26:58.057311414Z"
        },
    }
    d = diagnosis(component=None, category="unknown", affected_services=["email"])
    s = summary.build_summary(d, [], index)
    assert s["headline"] == "Multiple failures: email, valkey-cart"
    assert all("load-generator" not in line for line in s["impact"])
    assert all("2026-" not in w["text"] for w in s["why"])


def test_broken_never_shows_an_unregressed_metric():
    index = {
        "metric:error_rate:checkout@t": {
            "text": "checkout error_rate 0.0000 (baseline 0.0000, delta +0)"
        },
    }
    d = diagnosis(component="checkout", category="app_error", evidence_ids=[])
    s = summary.build_summary(d, [], index)
    assert all("0% (baseline 0%)" not in line for line in s["broken"])


def test_down_container_never_listed_as_not_the_cause():
    d = diagnosis(
        component="cart",
        category="app_error",
        alternatives=[
            {"hypothesis": "valkey-cart is down", "rejected_because": "cart errors"},
            {"hypothesis": "network", "rejected_because": "no timeouts"},
        ],
    )
    s = summary.build_summary(d, EVENTS, INDEX)
    assert all("valkey-cart" not in item for item in s["not_cause"])
    assert any(item.startswith("network") for item in s["not_cause"])


def test_missing_baseline_shows_no_baseline_not_a_delta():
    index = {
        "metric:latency_p95:checkout@t": {
            "text": "checkout latency_p95 4900.0000 (baseline unavailable)"
        },
    }
    assert (
        summary.short_evidence(
            "metric:latency_p95:checkout@t",
            index["metric:latency_p95:checkout@t"]["text"],
        )
        == "checkout p95 4.9 s (no baseline)"
    )
    d = diagnosis(component="checkout", category="app_error", evidence_ids=[])
    s = summary.build_summary(d, [], index)
    assert all("→" not in line for line in s["impact"])

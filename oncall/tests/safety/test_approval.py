import json
import time

import pytest

from oncall.config import Settings
from oncall.safety import approval
from oncall.safety.approval import read_approval_file, request_approval, summarize

PROPOSAL = {
    "action": "restart_service",
    "target": "cart",
    "params": {},
    "reason": "container exited 137; callers failing",
    "expected_effect": "cart comes back, checkout errors stop",
    "rollback_hint": "none needed (restart is idempotent)",
    "risk": "low",
}
DIAGNOSIS = {
    "component": "cart",
    "category": "container_down",
    "cause": "cart container exited with code 137",
    "confidence": 0.85,
    "evidence_ids": ["container:cart", "trace:abc/span:GetCart"],
    "affected_services": ["cart", "checkout"],
    "alternatives": [],
    "decision": "act",
    "reasoning": "container down [container:cart]",
}


def test_summary_shows_what_the_human_needs():
    text = summarize(PROPOSAL, DIAGNOSIS)
    for needle in (
        "cart / container_down",
        "0.85",
        "container:cart, trace:abc/span:GetCart",
        "restart_service on cart",
        "Rollback:  none needed",
        "Risk:      low",
    ):
        assert needle in text
    assert "Diagnosis: (none provided)" in summarize(PROPOSAL, None)


@pytest.mark.parametrize("answer", ["yes", "  yes\n"])
def test_cli_exact_yes_approves(answer):
    shown = []
    a = request_approval(
        PROPOSAL, DIAGNOSIS, "cli", input_fn=lambda prompt: answer, out=shown.append
    )
    assert a["approved"] is True
    assert a["by"]
    assert any("APPROVAL REQUIRED" in s for s in shown)


@pytest.mark.parametrize("answer", ["y", "YES", "Yes", "yes please", "no", ""])
def test_cli_anything_but_yes_rejects(answer):
    a = request_approval(PROPOSAL, DIAGNOSIS, "cli", input_fn=lambda p: answer, out=str)
    assert a["approved"] is False
    assert "rejected" in a["comment"]


def test_cli_timeout_rejects():
    def slow_input(prompt: str) -> str:
        time.sleep(1)
        return "yes"

    a = request_approval(
        PROPOSAL, DIAGNOSIS, "cli", timeout_s=0.1, input_fn=slow_input, out=str
    )
    assert a["approved"] is False
    assert a["by"] == "system"
    assert "no answer" in a["comment"]


def _ui_settings(tmp_path) -> Settings:
    return Settings(RUNS_DIR=str(tmp_path))


def _fake_clock_and_sleep():
    now = [0.0]

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    return clock, sleep


def test_ui_file_with_true_approves(tmp_path):
    s = _ui_settings(tmp_path)
    path = approval.approval_path("run1", s)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"approved": True, "by": "ayesha", "comment": "go"}))
    clock, sleep = _fake_clock_and_sleep()
    a = request_approval(
        PROPOSAL,
        DIAGNOSIS,
        "ui",
        run_id="run1",
        settings=s,
        out=str,
        clock=clock,
        sleep=sleep,
    )
    assert a == {"approved": True, "by": "ayesha", "ts": a["ts"], "comment": "go"}


@pytest.mark.parametrize(
    "content",
    [
        '{"approved": false, "by": "ayesha"}',
        '{"approved": "true"}',
        '{"approved": 1}',
        "[]",
    ],
)
def test_ui_only_a_literal_true_approves(tmp_path, content):
    s = _ui_settings(tmp_path)
    path = approval.approval_path("run2", s)
    path.parent.mkdir(parents=True)
    path.write_text(content)
    clock, sleep = _fake_clock_and_sleep()
    a = request_approval(
        PROPOSAL,
        None,
        "ui",
        run_id="run2",
        settings=s,
        timeout_s=3,
        out=str,
        clock=clock,
        sleep=sleep,
    )
    assert a["approved"] is False


def test_ui_waits_then_times_out(tmp_path):
    s = _ui_settings(tmp_path)
    clock, sleep = _fake_clock_and_sleep()
    a = request_approval(
        PROPOSAL,
        DIAGNOSIS,
        "ui",
        run_id="run3",
        settings=s,
        timeout_s=5,
        out=str,
        clock=clock,
        sleep=sleep,
    )
    assert a["approved"] is False
    assert "no decision within 5 s" == a["comment"]
    assert clock() >= 5


def test_ui_picks_up_a_file_written_later(tmp_path):
    s = _ui_settings(tmp_path)
    path = approval.approval_path("run4", s)
    clock, fake_sleep = _fake_clock_and_sleep()

    def sleep(seconds: float) -> None:
        fake_sleep(seconds)
        if clock() >= 2:  # the UI decides after 2 s
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"approved": True, "by": "ui"}))

    a = request_approval(
        PROPOSAL,
        DIAGNOSIS,
        "ui",
        run_id="run4",
        settings=s,
        timeout_s=10,
        out=str,
        clock=clock,
        sleep=sleep,
    )
    assert a["approved"] is True
    assert a["by"] == "ui"


def test_ui_without_run_id_rejects(tmp_path):
    a = request_approval(
        PROPOSAL, DIAGNOSIS, "ui", settings=_ui_settings(tmp_path), out=str
    )
    assert a["approved"] is False


def test_unreadable_file_is_none(tmp_path):
    assert read_approval_file(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert read_approval_file(bad) is None

"""Human approval gate: nothing changes without an explicit "yes".

Two channels:
  cli  show the diagnosis and the proposal on the terminal and read one line.
       Only the exact word "yes" approves; anything else, or no answer within
       the timeout, is a rejection.
  ui   wait for `runs/approvals/<run_id>.json`, written by the UI's buttons.
       The file holds an Approval: {"approved": true|false, "by": ..., "ts": ...,
       "comment": ...}. Only a literal true approves. No file within the
       timeout is a rejection.
This module never raises: every failure path is a rejected Approval.
"""

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from oncall.config import Settings, load_settings
from oncall.models import Approval, Diagnosis, Proposal

log = logging.getLogger(__name__)

APPROVAL_TIMEOUT_S = 120
APPROVE_WORD = "yes"
POLL_S = 1.0
Channel = Literal["cli", "ui"]


def approval_path(run_id: str, settings: Settings | None = None) -> Path:
    """The file the UI writes its decision to for this run."""
    settings = settings or load_settings()
    return Path(settings.RUNS_DIR) / "approvals" / f"{run_id}.json"


def summarize(proposal: Proposal, diagnosis: Diagnosis | None) -> str:
    """The text a human sees before deciding: diagnosis, evidence, plan, undo."""
    lines = ["=== APPROVAL REQUIRED ==="]
    if diagnosis:
        lines += [
            (
                f"Diagnosis: {diagnosis['component']} / {diagnosis['category']} "
                f"(confidence {diagnosis['confidence']:.2f})"
            ),
            f"Cause:     {diagnosis['cause']}",
            "Evidence:  " + (", ".join(diagnosis["evidence_ids"]) or "(none)"),
        ]
    else:
        lines.append("Diagnosis: (none provided)")
    lines += [
        (
            f"Proposed:  {proposal['action']} on {proposal['target']} "
            f"params={json.dumps(proposal['params'])}"
        ),
        f"Reason:    {proposal['reason']}",
        f"Expected:  {proposal['expected_effect']}",
        f"Rollback:  {proposal['rollback_hint']}",
        f"Risk:      {proposal['risk']}",
    ]
    return "\n".join(lines)


def _approval(approved: bool, by: str, comment: str) -> Approval:
    return {
        "approved": approved,
        "by": by,
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "comment": comment,
    }


def _read_with_timeout(
    prompt: str, timeout_s: float, input_fn: Callable[[str], str]
) -> str | None:
    """Read one line, or return None if nothing arrived within `timeout_s`."""
    answer: list[str] = []

    def read() -> None:
        try:
            answer.append(input_fn(prompt))
        except EOFError:
            answer.append("")

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    thread.join(timeout_s)
    return answer[0] if answer else None


def read_approval_file(path: Path) -> Approval | None:
    """Parse the UI's decision file. None if it is missing or unreadable.

    `approved` must be a literal JSON true to approve; anything else rejects.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    approved = raw.get("approved") is True
    return _approval(
        approved,
        str(raw.get("by") or "ui"),
        str(raw.get("comment") or ("approved in UI" if approved else "rejected in UI")),
    )


def request_approval(
    proposal: Proposal,
    diagnosis: Diagnosis | None,
    channel: Channel = "cli",
    *,
    run_id: str | None = None,
    timeout_s: float = APPROVAL_TIMEOUT_S,
    settings: Settings | None = None,
    input_fn: Callable[[str], str] | None = None,
    out: Callable[[str], object] = print,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Approval:
    """Ask a human to approve `proposal`. Returns an Approval; never raises."""
    out(summarize(proposal, diagnosis))

    if channel == "ui":
        if not run_id:
            return _approval(False, "system", "ui channel needs a run_id")
        path = approval_path(run_id, settings)
        out(f"Waiting up to {timeout_s:.0f} s for a decision in the UI ({path})")
        start = clock()
        while clock() - start < timeout_s:
            if path.exists():
                decision = read_approval_file(path)
                if decision is not None:
                    return decision
                log.warning("approval file %s is unreadable; waiting", path)
            sleep(POLL_S)
        return _approval(False, "system", f"no decision within {timeout_s:.0f} s")

    prompt = (
        f"Type exactly '{APPROVE_WORD}' to approve (anything else, or no answer "
        f"within {timeout_s:.0f} s, rejects): "
    )
    answer = _read_with_timeout(prompt, timeout_s, input_fn or input)
    by = os.environ.get("USERNAME") or os.environ.get("USER") or "cli"
    if answer is None:
        return _approval(False, "system", f"no answer within {timeout_s:.0f} s")
    if answer.strip() == APPROVE_WORD:
        return _approval(True, by, "approved on the command line")
    return _approval(False, by, f"rejected on the command line: {answer.strip()!r}")

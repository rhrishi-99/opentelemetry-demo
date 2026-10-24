"""Executor: the one code path from an approved Proposal to the Action API.

Order, every time:
  1. kill switch        engaged -> blocked, nobody is asked
  2. allowlist          not allowlisted -> blocked, nobody is asked
  3. human approval     anything but an explicit yes -> blocked
  4. dry run            log "would do X", call nothing
  5. kill switch again  engaged after approval -> blocked
  6. Action API         oncall.safety.action_api.execute (the only caller)
Every step is written to the run log, and every outcome to runs/actions.jsonl. This module is deterministic Python: no
LLM, no free-form input, and CI checks that nothing else imports action_api.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from oncall.agent.runlog import RunLog
from oncall.config import Settings, load_settings
from oncall.models import ActionRequest, ActionResult, Approval, Diagnosis, Proposal
from oncall.safety import killswitch
from oncall.safety.actionlog import log_action
from oncall.safety.approval import APPROVAL_TIMEOUT_S, Channel, request_approval

log = logging.getLogger(__name__)


def to_request(proposal: Proposal, run_id: str, dry_run: bool) -> ActionRequest:
    """The ActionRequest the Action API understands, built from a Proposal."""
    return {
        "action": proposal["action"],
        "target": proposal["target"],
        "params": dict(proposal["params"]),
        "reason": proposal["reason"],
        "run_id": run_id,
        "dry_run": dry_run,
    }


def _blocked(req: ActionRequest, detail: str, *, allowed: bool) -> ActionResult:
    """An ActionResult for a request that never reached the Action API."""
    return {
        "request": req,
        "allowed": allowed,
        "executed": False,
        "dry_run": req["dry_run"],
        "ok": False,
        "detail": detail,
        "before": {},
        "after": {},
        "rollback": None,
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _validate(req: ActionRequest) -> tuple[bool, str]:
    """Allowlist pre-check, so a human is never asked about a forbidden action.

    Uses the Action API's own allowlist (F19). Without it nothing is allowed:
    there would be no API to execute against anyway.
    """
    try:
        from oncall.safety.allowlist import validate_action
    except ImportError:
        return False, "allowlist unavailable: the Action API (F19) is not installed"
    allowed, detail, _target, _params = validate_action(req)
    return allowed, detail


def _action_api_execute() -> Callable[..., ActionResult] | None:
    """The Action API's execute(), imported late so the agent never loads it."""
    try:
        from oncall.safety import action_api
    except ImportError:
        return None
    return action_api.execute


def execute(
    proposal: Proposal,
    run: RunLog,
    dry_run: bool | None = None,
    *,
    diagnosis: Diagnosis | None = None,
    channel: Channel = "cli",
    approval: Approval | None = None,
    settings: Settings | None = None,
    timeout_s: float = APPROVAL_TIMEOUT_S,
    input_fn: Callable[[str], str] | None = None,
) -> ActionResult:
    """Run the gate for `proposal` and, only if everything passes, act.

    `dry_run` defaults to settings.DRY_RUN (true unless configured otherwise).
    Pass `approval` when the human already decided (e.g. the UI); otherwise
    it is requested here on `channel`. Never raises.
    """
    settings = settings or load_settings()
    if dry_run is None:
        dry_run = settings.DRY_RUN
    req = to_request(proposal, run.run_id, dry_run)
    run.log("handoff", stage="executor", proposal=proposal, dry_run=dry_run)

    if killswitch.engaged(settings):
        return _finish(
            run, _blocked(req, "blocked: kill switch engaged", allowed=False), settings
        )

    allowed, detail = _validate(req)
    if not allowed:
        return _finish(
            run,
            _blocked(req, f"blocked: not allowlisted: {detail}", allowed=False),
            settings,
        )

    if approval is None:
        approval = request_approval(
            proposal,
            diagnosis,
            channel,
            run_id=run.run_id,
            timeout_s=timeout_s,
            settings=settings,
            input_fn=input_fn,
        )
    run.log("handoff", stage="approval", approval=approval)
    if approval["approved"] is not True:
        detail = f"rejected by {approval['by']}: {approval['comment']}"
        return _finish(run, _blocked(req, detail, allowed=True), settings)

    if dry_run:
        result = _blocked(
            req,
            f"[DRY RUN] would {req['action']} {req['target']} {req['params']}",
            allowed=True,
        )
        result["ok"] = True
        return _finish(run, result, settings)

    if killswitch.engaged(settings):
        return _finish(
            run,
            _blocked(req, "blocked: kill switch engaged after approval", allowed=True),
            settings,
        )

    api_execute = _action_api_execute()
    if api_execute is None:
        return _finish(
            run, _blocked(req, "Action API unavailable", allowed=True), settings
        )
    try:
        result = api_execute(req, settings)
    except Exception as exc:  # the API promises not to raise; stay safe anyway
        log.exception("Action API raised")
        result = _blocked(
            req, f"Action API raised {type(exc).__name__}: {exc}", allowed=True
        )
        return _finish(run, result, settings)
    # The Action API wrote its own actions.jsonl line.
    return _finish(run, result)


def to_proposal(req: ActionRequest, purpose: str = "rollback") -> Proposal:
    """A Proposal for an ActionRequest, so a rollback is shown to the human
    the same way as the original action."""
    return {
        "action": req["action"],
        "target": req["target"],
        "params": dict(req["params"]),
        "reason": f"{purpose}: {req['reason']}",
        "expected_effect": f"undo the previous action on {req['target']}",
        "rollback_hint": "none: this is the rollback",
        "risk": "low",
    }


def execute_request(
    req: ActionRequest, run: RunLog, dry_run: bool | None = None, **gate: object
) -> ActionResult:
    """Run the same gate for a ready-made ActionRequest (e.g. a rollback)."""
    return execute(to_proposal(req), run, dry_run, **gate)  # type: ignore[arg-type]


def rollback_gate(
    run: RunLog,
    *,
    diagnosis: Diagnosis | None = None,
    channel: Channel = "cli",
    dry_run: bool | None = None,
    settings: Settings | None = None,
    timeout_s: float = APPROVAL_TIMEOUT_S,
    input_fn: Callable[[str], str] | None = None,
) -> tuple[Callable[[dict], bool], Callable[[dict], dict]]:
    """The two callbacks verify_and_maybe_rollback() needs, backed by this gate.

    approve_rollback asks the human once and remembers the answer;
    execute_rollback then runs the full gate (kill switch, allowlist, that
    approval, dry run) and the Action API.
    """
    settings = settings or load_settings()
    decided: dict[str, Approval] = {}

    def approve_rollback(req: dict) -> bool:
        key = f"{req['action']}:{req['target']}"
        decided[key] = request_approval(
            to_proposal(req),  # type: ignore[arg-type]
            diagnosis,
            channel,
            run_id=run.run_id,
            timeout_s=timeout_s,
            settings=settings,
            input_fn=input_fn,
        )
        return decided[key]["approved"] is True

    def execute_rollback(req: dict) -> dict:
        key = f"{req['action']}:{req['target']}"
        return execute_request(
            req,  # type: ignore[arg-type]
            run,
            dry_run,
            diagnosis=diagnosis,
            channel=channel,
            approval=decided.get(key),
            settings=settings,
            timeout_s=timeout_s,
            input_fn=input_fn,
        )

    return approve_rollback, execute_rollback


def _finish(
    run: RunLog, result: ActionResult, audit: Settings | None = None
) -> ActionResult:
    """Log the outcome to the run log and, with `audit`, to runs/actions.jsonl.

    `audit` is given for every outcome the Action API never saw.
    """
    run.log("action", result=result)
    if audit is not None:
        log_action(
            result,
            f"act-{uuid.uuid4().hex[:8]}",
            Path(audit.RUNS_DIR) / "actions.jsonl",
        )
    return result

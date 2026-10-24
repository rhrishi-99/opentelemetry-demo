"""Three-agent orchestrator with typed hand-offs (F28).

Orchestrates the remediation lifecycle:
1. Detective (LLM, read-only tools): Alert -> Investigation
2. Planner (LLM, tools=[]): Investigation -> Plan
3. Human Gate: Explicit 'yes' (CLI/UI) + kill switch -> Approval
4. Operator (Deterministic Python, no LLM): Action API + Verification -> Execution

Every hand-off across boundaries is logged to the run log as event 'handoff'.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oncall.agent.evidence import validate as validate_evidence
from oncall.agent.llm import LLM, LLMError
from oncall.agent.loop import investigate_and_diagnose
from oncall.agent.runlog import RunLog
from oncall.config import Settings, load_settings
from oncall.models import (
    ActionRequest,
    ActionResult,
    Alert,
    Approval,
    Diagnosis,
    Execution,
    Investigation,
    Plan,
    Proposal,
    Verification,
)
from oncall.report.output import PREVENTION, report_run
from oncall.safety import approval as human_approval
from oncall.safety import executor, killswitch, validate_action
from oncall.safety.verify import verify_and_maybe_rollback
from oncall.tools import TOOL_SCHEMAS, TOOLS

VERIFY_TIMEOUT_S = 120
VERIFY_POLL_S = 10


def _verification(recovered: bool, outcome: str, notes: str) -> Verification:
    return {
        "recovered": recovered,
        "checks": [],
        "polls": 0,
        "duration_s": 0.0,
        "rolled_back": False,
        "rollback_result": None,
        "outcome": outcome,  # type: ignore[typeddict-item]
        "notes": notes,
    }


def _create_manual_alert(description: str, service: str | None = None) -> Alert:
    now = datetime.now(UTC)
    return {
        "id": f"alert-{now.strftime('%Y%m%dT%H%M%S')}",
        "ts": now.isoformat(),
        "service": service,
        "signal": "manual",
        "value": None,
        "threshold": None,
        "description": description,
        "source": "manual",
    }


class Detective:
    """Agent 1: Investigates using read-only telemetry tools and produces an Investigation."""

    def __init__(
        self,
        llm: LLM | None = None,
        tools: dict | None = None,
        schemas: dict | None = None,
        max_steps: int | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools if tools is not None else TOOLS
        self.schemas = schemas if schemas is not None else TOOL_SCHEMAS
        self.max_steps = max_steps

    def investigate(self, alert: Alert, runlog: RunLog) -> Investigation:
        """Run telemetry investigation and evidence validation."""
        result = investigate_and_diagnose(
            alert,
            tools=self.tools,
            llm=self.llm,
            max_steps=self.max_steps,
            runlog=runlog,
            schemas=self.schemas,
        )
        diagnosis: Diagnosis = result["diagnosis"]
        _, problems = validate_evidence(diagnosis, runlog)
        evidence_valid = len(problems) == 0 and bool(diagnosis.get("evidence_ids"))

        return {
            "alert": alert,
            "diagnosis": diagnosis,
            "evidence_valid": evidence_valid,
            "run_id": runlog.run_id,
        }


class Planner:
    """Agent 2: Challenges the diagnosis and proposes allowlisted remediations with NO tool access."""

    def __init__(self, llm: LLM | None = None) -> None:
        self.llm = llm

    def review_and_plan(self, investigation: Investigation, runlog: RunLog) -> Plan:
        """Review diagnosis without tool access, select allowlisted fix, and generate Plan."""
        diagnosis = investigation["diagnosis"]
        final_diagnosis = dict(diagnosis)
        category = final_diagnosis.get("category", "unknown")
        component = final_diagnosis.get("component")

        # Review challenges using LLM with tools=[]
        review: dict[str, Any] = {
            "challenges": [],
            "ruled_out": True,
            "confidence_adjustment": 0.0,
        }
        if self.llm is not None:
            prompt = (
                f"Review the diagnosis for incident on component {component!r} (category: {category}). "
                f"Reasoning: {final_diagnosis.get('reasoning')}. "
                "List 1-2 possible alternative hypotheses and state whether existing evidence rules them out."
            )
            try:
                # Strictly empty tool list per architectural invariant
                if hasattr(self.llm, "chat"):
                    review_resp = self.llm.chat(
                        messages=[{"role": "user", "content": prompt}],
                        tools=[],
                    )
                else:
                    review_resp = self.llm.call(
                        messages=[{"role": "user", "content": prompt}],
                        tools=[],
                    )
                review_text = getattr(review_resp, "text", str(review_resp))
                review["challenges"].append({"review_text": review_text[:500]})
                runlog.log("llm_call", role="planner", tools_count=0)
            except (LLMError, RuntimeError):
                review["challenges"].append(
                    {"review_text": "Review completed deterministically."}
                )
        else:
            review["challenges"].append(
                {"review_text": "Review completed deterministically (no LLM)."}
            )

        # Confidence and evidence validity rules
        if (
            not investigation.get("evidence_valid", False)
            or final_diagnosis.get("confidence", 0.0) < 0.7
        ) and final_diagnosis.get("decision") == "act":
            final_diagnosis["decision"] = "escalate"
            final_diagnosis["reasoning"] = (
                f"{final_diagnosis.get('reasoning', '')} "
                "[Planner: downgraded to escalate due to insufficient confidence or evidence]"
            ).strip()

        decision = final_diagnosis.get("decision", "escalate")

        # Deterministic category -> allowlisted action mapping
        proposal: Proposal | None = None
        if decision == "act" and component:
            proposal = self._map_proposal(category, component, runlog)
            if proposal is None:
                final_diagnosis["decision"] = "escalate"

        prevention = PREVENTION.get(
            category,
            "Add regression tests and telemetry alerting for the failing component.",
        )

        return {
            "review": review,
            "diagnosis_final": final_diagnosis,  # type: ignore[typeddict-item]
            "proposal": proposal,
            "prevention": prevention,
        }

    def _map_proposal(
        self,
        category: str,
        component: str,
        runlog: RunLog,
    ) -> Proposal | None:
        action: str | None = None
        target = component
        params: dict[str, Any] = {}
        risk: str = "low"

        if category == "container_down":
            # Check if container is known to be Exited
            action = "restart_service"
            for event in runlog.events("tool_result"):
                for ev in event.get("result", {}).get("evidence", []):
                    if component in ev.get("id", "") and "Exited" in ev.get("text", ""):
                        action = "start_service"
                        break
        elif category == "dependency_unavailable":
            action = "restart_service"
        elif category == "app_error":
            action = "disable_failure_flags"
        elif category == "resource_memory":
            action = "restart_service"
        elif category == "resource_cpu":
            action = "restart_service"
            risk = "medium"
        elif category == "traffic_surge":
            action = "throttle_load"
            target = "load-generator"
            params = {"users": 5}
        else:
            return None

        prop: Proposal = {
            "action": action,
            "target": target,
            "params": params,
            "reason": f"Remediate {category} on {target}",
            "expected_effect": f"Restore {target} service health and recover errors",
            "rollback_hint": (
                "idempotent"
                if action in ("restart_service", "start_service")
                else "restore previous state"
            ),
            "risk": risk,  # type: ignore[typeddict-item]
        }

        # Check in-code allowlist
        sample_req: ActionRequest = {
            "action": prop["action"],
            "target": prop["target"],
            "params": prop["params"],
            "reason": prop["reason"],
            "run_id": runlog.run_id,
            "dry_run": True,
        }
        allowed, detail, norm_target, _ = validate_action(sample_req)
        if not allowed:
            runlog.log(
                "decision", stage="proposal_rejected_by_allowlist", detail=detail
            )
            return None

        prop["target"] = norm_target
        return prop


class HumanGate:
    """Approval gate checking kill switch and enforcing explicit human approval."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()

    def kill_switch_engaged(self) -> bool:
        """Check if kill switch file exists or KILL_SWITCH=1 is set in env."""
        return killswitch.engaged(self.settings)

    def request_approval(
        self,
        proposal: Proposal | None,
        diagnosis: Diagnosis,
        run_id: str,
        channel: str = "cli",
        input_fn: Any = None,
    ) -> Approval:
        """Request human approval via CLI or UI."""
        ts = datetime.now(UTC).isoformat()

        if self.kill_switch_engaged():
            return {
                "approved": False,
                "by": "kill_switch",
                "ts": ts,
                "comment": "Kill switch engaged — all actions blocked",
            }

        if proposal is None or diagnosis.get("decision") != "act":
            return {
                "approved": False,
                "by": "system",
                "ts": ts,
                "comment": "No action proposed; human approval skipped",
            }

        return human_approval.request_approval(
            proposal,
            diagnosis,
            channel,  # type: ignore[arg-type]
            run_id=run_id,
            settings=self.settings,
            input_fn=input_fn,
        )


def execute_action(
    req: ActionRequest,
    settings: Settings,
    *,
    runlog: RunLog,
    approval: Approval | None,
    dry_run: bool,
    channel: str = "cli",
    diagnosis: Diagnosis | None = None,
) -> ActionResult:
    """Every action goes through the F23 gate: kill switch, allowlist, the human's
    approval (asked here if `approval` is None, e.g. for a rollback), dry run, and
    only then the Action API itself."""
    return executor.execute_request(
        req,
        runlog,
        dry_run,
        approval=approval,
        settings=settings,
        channel=channel,  # type: ignore[arg-type]
        diagnosis=diagnosis,
    )


# Recovery is verified from telemetry (F24): read-only docker inspect, Jaeger error
# spans and Prometheus, polled until healthy for 3 polls or VERIFY_WINDOW_S.
verify_recovery = verify_and_maybe_rollback


class Operator:
    """Deterministic Python executor: executes approved actions via Action API and writes report."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()

    def _verify(
        self,
        investigation: Investigation,
        diagnosis: Diagnosis,
        action_result: ActionResult,
        runlog: RunLog,
    ) -> Verification:
        """F24 verify for an executed action; a dry run is never 'recovered'."""
        if action_result.get("dry_run") or not action_result.get("executed"):
            return _verification(False, "skipped_dry_run", "dry run: nothing executed")
        try:
            return verify(
                investigation["alert"],
                diagnosis,
                action_result,
                timeout_s=VERIFY_TIMEOUT_S,
                poll_s=VERIFY_POLL_S,
                runlog=runlog,
            )
        except Exception as exc:  # noqa: BLE001
            return _verification(
                False, "not_recovered_escalated", f"verify failed: {exc}"
            )

    def execute_and_report(
        self,
        investigation: Investigation,
        plan: Plan,
        approval: Approval,
        runlog: RunLog,
        dry_run: bool = True,
        channel: str = "cli",
        input_fn: Any = None,
    ) -> Execution:
        """Execute approved proposal via Action API, verify, and write incident report."""
        proposal = plan["proposal"]
        diagnosis = plan["diagnosis_final"]
        action_result: ActionResult | None = None
        verification: dict[str, Any] | None = None
        status = "no_action"

        # Check kill switch before touching Action API
        kill_switch = killswitch.engaged(self.settings)

        if kill_switch or approval.get("by") == "kill_switch":
            status = "aborted"
        elif diagnosis.get("decision") == "no_incident":
            status = "no_action"
        elif diagnosis.get("decision") == "escalate" or proposal is None:
            status = "escalated"
        elif not approval.get("approved"):
            status = "aborted"
        elif proposal is not None and approval.get("approved"):
            # Action execution through the Action API
            action_req: ActionRequest = {
                "action": proposal["action"],
                "target": proposal["target"],
                "params": proposal["params"],
                "reason": proposal["reason"],
                "run_id": runlog.run_id,
                "dry_run": dry_run,
            }
            action_result = execute_action(
                action_req,
                self.settings,
                runlog=runlog,
                approval=approval,
                dry_run=dry_run,
                channel=channel,
                diagnosis=diagnosis,
            )
            runlog.log(
                "action",
                action_id=action_result.get("ts"),
                request=action_req,
                allowed=action_result["allowed"],
                executed=action_result["executed"],
                ok=action_result["ok"],
            )

            # Verification: did the system actually recover? (F24). A rollback,
            # if needed, is offered to the human through the same gate.
            if action_result["ok"]:
                approve_rollback, execute_rollback = executor.rollback_gate(
                    runlog,
                    diagnosis=diagnosis,
                    channel=channel,  # type: ignore[arg-type]
                    dry_run=dry_run,
                    settings=self.settings,
                    input_fn=input_fn,
                )
                verification = verify_recovery(
                    investigation["alert"],
                    diagnosis,
                    action_result,
                    approve_rollback=approve_rollback,
                    execute_rollback=execute_rollback,
                    runlog=runlog,
                    timeout_s=self.settings.VERIFY_WINDOW_S,
                )
                recovered_or_dry = verification["outcome"] in (
                    "recovered",
                    "skipped_dry_run",
                )
                status = "success" if recovered_or_dry else "escalated"
            else:
                rollback_res = None
                if action_result.get("executed") and action_result.get("rollback"):
                    rollback_req = action_result["rollback"]
                    # The human approves the rollback the same way as the action.
                    rollback_res = execute_action(
                        rollback_req,
                        self.settings,
                        runlog=runlog,
                        approval=None,
                        dry_run=dry_run,
                        channel=channel,
                        diagnosis=diagnosis,
                    )
                    runlog.log(
                        "action",
                        action_id=rollback_res.get("ts"),
                        request=rollback_req,
                        allowed=rollback_res["allowed"],
                        executed=rollback_res["executed"],
                        ok=rollback_res["ok"],
                        stage="rollback",
                    )
                verification = {
                    "recovered": False,
                    "target": proposal["target"],
                    "checks": [{"signal": "action_api_executed", "ok": False}],
                    "rollback_executed": bool(
                        rollback_res and rollback_res.get("executed")
                    ),
                }
                status = "escalated"

        # Write incident report
        json_path, _ = report_run(
            investigation["alert"],
            diagnosis,
            runlog,
            out_dir=Path(self.settings.RUNS_DIR) / "reports",
            actions=[action_result] if action_result else [],
            verification=verification or {},
        )

        return {
            "investigation": investigation,
            "plan": plan,
            "approval": approval,
            "action_result": action_result,
            "verification": verification,
            "report_path": str(json_path),
            "status": status,  # type: ignore[typeddict-item]
        }


def run_incident(
    alert: Alert | dict | str,
    channel: str = "cli",
    dry_run: bool = True,
    settings: Settings | None = None,
    input_fn: Any = None,
    llm: LLM | None = None,
    runlog: RunLog | None = None,
) -> Execution:
    """Execute the full three-agent remediation lifecycle with typed hand-offs."""
    settings = settings or load_settings()
    runlog = runlog or RunLog(runs_dir=settings.RUNS_DIR)

    if llm is None:
        try:
            llm = LLM(settings)
        except (LLMError, ValueError):
            llm = None

    if isinstance(alert, str):
        alert_obj = _create_manual_alert(alert)
    elif isinstance(alert, dict):
        alert_obj = alert  # type: ignore[assignment]
    else:
        alert_obj = alert

    runlog.log("alert", alert=alert_obj)

    # Agent 1: Detective
    detective = Detective(llm=llm)
    investigation = detective.investigate(alert_obj, runlog)
    d = investigation["diagnosis"]
    runlog.log(
        "handoff",
        **{
            "from": "detective",
            "to": "planner",
            "summary": (
                f"Diagnosed {d.get('category')} on component {d.get('component')} "
                f"(confidence: {d.get('confidence', 0):.2f}, decision: {d.get('decision')})"
            ),
        },
    )

    # Agent 2: Planner
    planner = Planner(llm=llm)
    plan = planner.review_and_plan(investigation, runlog)
    proposal = plan["proposal"]
    final_diagnosis = plan["diagnosis_final"]

    # Hand-off to HumanGate or direct to Operator if no action proposed
    human_gate = HumanGate(settings=settings)
    if proposal is not None and final_diagnosis.get("decision") == "act":
        runlog.log(
            "handoff",
            **{
                "from": "planner",
                "to": "human_gate",
                "summary": f"Proposed {proposal['action']} on {proposal['target']} ({proposal['reason']})",
            },
        )
        approval = human_gate.request_approval(
            proposal, final_diagnosis, runlog.run_id, channel=channel, input_fn=input_fn
        )
        runlog.log(
            "handoff",
            **{
                "from": "human_gate",
                "to": "operator",
                "summary": f"Approval status: {approval['approved']} (by: {approval['by']})",
            },
        )
    else:
        runlog.log(
            "handoff",
            **{
                "from": "planner",
                "to": "operator",
                "summary": f"No action proposed ({final_diagnosis.get('decision')}) — routing directly to operator",
            },
        )
        approval = human_gate.request_approval(
            None, final_diagnosis, runlog.run_id, channel=channel, input_fn=input_fn
        )

    # Agent 3: Operator
    operator = Operator(settings=settings)
    execution = operator.execute_and_report(
        investigation,
        plan,
        approval,
        runlog,
        dry_run=dry_run,
        channel=channel,
        input_fn=input_fn,
    )

    runlog.log("finish", status=execution["status"], report=execution["report_path"])
    return execution

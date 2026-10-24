"""Audit log of all action requests and executions (append-only).

Every call to the Action API is recorded to runs/actions.jsonl for forensic
traceability and real-time UI display.
"""

import json
from pathlib import Path

from oncall.models import ActionResult


def log_action(
    result: ActionResult,
    action_id: str,
    log_path: Path | str = "runs/actions.jsonl",
) -> None:
    """Append one action execution record to runs/actions.jsonl."""
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "action_id": action_id,
        "run_id": result["request"].get("run_id", ""),
        "request": result["request"],
        "allowed": result["allowed"],
        "dry_run": result["dry_run"],
        "executed": result["executed"],
        "ok": result["ok"],
        "detail": result["detail"],
        "rollback": result.get("rollback"),
        "ts": result["ts"],
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def read_action_log(
    log_path: Path | str = "runs/actions.jsonl",
    run_id: str | None = None,
) -> list[dict]:
    """Read actions from the log, optionally filtered by run_id."""
    path = Path(log_path)
    if not path.is_file():
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if run_id is None or entry.get("run_id") == run_id:
                records.append(entry)
    return records

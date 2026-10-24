import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oncall import main as cli
from oncall.agent.runlog import RunLog
from oncall.safety import executor
from oncall.safety import verify as verify_mod


def _verified(outcome):
    return MagicMock(
        return_value={
            "recovered": outcome == "recovered",
            "checks": [],
            "polls": 1,
            "duration_s": 0.0,
            "rolled_back": False,
            "rollback_result": None,
            "outcome": outcome,
            "notes": "",
        }
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / ".killswitch"))
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    api = MagicMock(
        side_effect=lambda req, settings=None: {
            "request": req,
            "allowed": True,
            "executed": True,
            "dry_run": False,
            "ok": True,
            "detail": f"Started container {req['target']}",
            "before": {},
            "after": {},
            "rollback": None,
            "ts": "t",
        }
    )
    monkeypatch.setattr(executor, "_action_api_execute", lambda: api)
    return tmp_path, api


def _run_dir(tmp_path):
    runs = [p for p in (tmp_path / "runs").glob("*.jsonl") if p.name != "actions.jsonl"]
    assert len(runs) == 1
    return RunLog(run_id=runs[0].stem, runs_dir=str(tmp_path / "runs"))


ARGS = ["act", "--action", "start_service", "--target", "valkey-cart"]


def test_execute_after_exact_yes_then_verify_and_report(env, monkeypatch):
    tmp_path, api = env
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    verify = _verified("recovered")
    monkeypatch.setattr(verify_mod, "verify", verify)
    assert cli.main([*ARGS, "--execute", "--reason", "cart cannot reach redis"]) == 0
    api.assert_called_once()
    assert api.call_args.args[0]["target"] == "valkey-cart"
    verify.assert_called_once()
    run = _run_dir(tmp_path)
    proposal = run.events("handoff")[0]["proposal"]
    assert proposal["source"] == "human" and proposal["action"] == "start_service"
    assert run.events("action")[-1]["result"]["executed"] is True
    report = [e for e in run.events("decision") if e.get("stage") == "report"]
    saved = json.loads(Path(report[0]["json_path"]).read_text(encoding="utf-8"))
    assert saved["decision"] == "act"
    assert saved["reasoning"].startswith("human-directed action")
    assert saved["alert"]["source"] == "human"
    assert saved["actions"][0]["executed"] is True


def test_anything_but_yes_is_rejected(env, monkeypatch):
    _, api = env
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    monkeypatch.setattr(verify_mod, "verify", _verified("not_recovered_escalated"))
    assert cli.main([*ARGS, "--execute"]) == 1
    api.assert_not_called()


def test_dry_run_by_default_calls_nothing(env, monkeypatch):
    _, api = env
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    monkeypatch.setattr(verify_mod, "verify", _verified("skipped_dry_run"))
    assert cli.main(ARGS) == 0
    api.assert_not_called()


def test_kill_switch_blocks_before_asking(env, monkeypatch):
    tmp_path, api = env
    (tmp_path / ".killswitch").touch()
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "yes")
    monkeypatch.setattr(verify_mod, "verify", _verified("not_recovered_escalated"))
    assert cli.main([*ARGS, "--execute"]) == 1
    assert asked == []
    api.assert_not_called()


def test_not_allowlisted_target_is_blocked(env, monkeypatch):
    _, api = env
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    monkeypatch.setattr(verify_mod, "verify", _verified("not_recovered_escalated"))
    args = ["act", "--action", "start_service", "--target", "prometheus", "--execute"]
    assert cli.main(args) == 1
    api.assert_not_called()

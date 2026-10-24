"""Kill switch: when engaged, no action reaches the Action API.

It is a file (KILL_SWITCH_FILE, default `.killswitch` in the working directory)
or the environment variable KILL_SWITCH=1. The executor checks it before asking
a human and again immediately before calling the Action API, so engaging it at
any moment stops the agent from changing anything. Humans flip it with
`python -m oncall.main killswitch on|off|status`.
"""

import os
from datetime import UTC, datetime
from pathlib import Path

from oncall.config import Settings, load_settings

ENV_VAR = "KILL_SWITCH"


def switch_path(settings: Settings | None = None) -> Path:
    """Where the kill-switch file lives."""
    settings = settings or load_settings()
    return Path(settings.KILL_SWITCH_FILE)


def engaged(settings: Settings | None = None) -> bool:
    """True when the file exists or KILL_SWITCH=1 is set. Never raises."""
    if os.environ.get(ENV_VAR, "").strip() == "1":
        return True
    return switch_path(settings).exists()


def engage(settings: Settings | None = None) -> Path:
    """Create the kill-switch file (idempotent) and return its path."""
    path = switch_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"engaged {datetime.now(UTC).isoformat()}\n", encoding="utf-8")
    return path


def disengage(settings: Settings | None = None) -> bool:
    """Remove the kill-switch file. Returns True if it existed."""
    path = switch_path(settings)
    if not path.exists():
        return False
    path.unlink()
    return True


def status(settings: Settings | None = None) -> str:
    """One human-readable line about the kill switch."""
    settings = settings or load_settings()
    path = switch_path(settings)
    if os.environ.get(ENV_VAR, "").strip() == "1":
        return f"kill switch ENGAGED ({ENV_VAR}=1 in the environment)"
    if path.exists():
        return f"kill switch ENGAGED ({path})"
    return f"kill switch off ({path} absent, {ENV_VAR} unset)"

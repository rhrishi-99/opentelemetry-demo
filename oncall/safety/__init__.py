"""Safety module: allowlists, the human gate, action execution, and audit logging.

The Action API is deliberately not re-exported here: only
oncall/safety/executor.py may call it, after the kill switch and human approval.
"""

from oncall.safety.actionlog import log_action, read_action_log
from oncall.safety.allowlist import DEMO_SERVICES, validate_action

__all__ = [
    "DEMO_SERVICES",
    "log_action",
    "read_action_log",
    "validate_action",
]

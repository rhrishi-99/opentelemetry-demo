"""In-code allowlist for system remediations (RULES.md hard rule 3).

The Action API executes ONLY actions that pass this allowlist. Any
non-allowlisted action, forbidden target, or invalid parameter set is
rejected deterministically before execution.
"""

from typing import Any

from oncall.models import ActionRequest

# Allowlisted demo services that can be restarted, started, or flag-remediated.
# Observability stack, flagd, and load generator are excluded from service restart.
DEMO_SERVICES: frozenset[str] = frozenset(
    {
        "accounting",
        "ad",
        "astronomy-db",
        "cart",
        "checkout",
        "currency",
        "email",
        "fraud-detection",
        "frontend",
        "image-provider",
        "kafka",
        "payment",
        "product-catalog",
        "quote",
        "recommendation",
        "shipping",
        "valkey-cart",
    }
)

# Common aliases mapped to canonical container/service names.
SERVICE_ALIASES: dict[str, str] = {
    "accountingservice": "accounting",
    "adservice": "ad",
    "cart-cache": "valkey-cart",
    "cartservice": "cart",
    "checkoutservice": "checkout",
    "currencyservice": "currency",
    "emailservice": "email",
    "fraud-detection-service": "fraud-detection",
    "frauddetectionservice": "fraud-detection",
    "loadgen": "load-generator",
    "payment-service": "payment",
    "paymentservice": "payment",
    "product-catalog-service": "product-catalog",
    "productcatalog": "product-catalog",
    "productcatalogservice": "product-catalog",
    "quoteservice": "quote",
    "recommendationservice": "recommendation",
    "shippingservice": "shipping",
    "valkey": "valkey-cart",
}

DISALLOWED_TARGETS: frozenset[str] = frozenset(
    {
        "flagd",
        "flagd-ui",
        "frontend-proxy",
        "grafana",
        "jaeger",
        "opamp-server",
        "opensearch",
        "otel-collector",
        "prometheus",
        "telemetry-docs",
    }
)

ALLOWLISTED_ACTIONS: frozenset[str] = frozenset(
    {
        "disable_failure_flags",
        "restart_service",
        "restore_flags",
        "scale_service",
        "start_service",
        "throttle_load",
    }
)


def normalize_target(target: str | None) -> str:
    """Normalize a target name by stripping whitespace, lowercasing, and applying aliases."""
    if not target:
        return ""
    cleaned = target.strip().lower()
    return SERVICE_ALIASES.get(cleaned, cleaned)


def validate_action(
    req: ActionRequest,
) -> tuple[bool, str, str, dict[str, Any]]:
    """Validate an ActionRequest against the deterministic safety allowlist.

    Returns:
        (allowed: bool, detail: str, normalized_target: str, normalized_params: dict)
    """
    action = (req.get("action") or "").strip()
    raw_target = req.get("target") or ""
    target = normalize_target(raw_target)
    params = req.get("params") or {}

    if action == "scale_service":
        return False, "scale_service is not supported on Docker Compose", target, params

    if action not in ALLOWLISTED_ACTIONS:
        return False, f"action {action!r} is not in allowlist", target, params

    if action == "throttle_load":
        if target != "load-generator":
            return (
                False,
                f"throttle_load target must be 'load-generator', got {raw_target!r}",
                target,
                params,
            )
        users = params.get("users")
        if not isinstance(users, int) or isinstance(users, bool) or users < 1:
            return (
                False,
                "throttle_load requires params.users to be an integer >= 1",
                target,
                params,
            )
        return True, "Action permitted by allowlist", target, params

    if action in (
        "restart_service",
        "start_service",
        "disable_failure_flags",
        "restore_flags",
    ):
        if target in DISALLOWED_TARGETS or target == "load-generator":
            return (
                False,
                f"target {raw_target!r} is disallowed (observability, client or system container)",
                target,
                params,
            )
        if target not in DEMO_SERVICES:
            return (
                False,
                f"target {raw_target!r} is not an allowlisted demo service",
                target,
                params,
            )

        if action == "restore_flags":
            if not (params.get("backup_file") or params.get("backup_path")):
                return (
                    False,
                    "restore_flags requires params.backup_file or params.backup_path",
                    target,
                    params,
                )

        return True, "Action permitted by allowlist", target, params

    return False, f"action {action!r} not permitted", target, params

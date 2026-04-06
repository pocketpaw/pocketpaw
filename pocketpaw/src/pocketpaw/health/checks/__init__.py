"""Health check package -- modular checks with a unified registry.

All public symbols are re-exported here so that existing imports like
``from pocketpaw.health.checks import HealthCheckResult`` continue to work.
"""

from pocketpaw.health.checks.api_keys import (
    check_api_key_format,
    check_api_key_primary,
    check_backend_deps,
    check_secrets_encrypted,
)
from pocketpaw.health.checks.config import (
    check_config_exists,
    check_config_permissions,
    check_config_valid_json,
)
from pocketpaw.health.checks.connectivity import check_llm_reachable
from pocketpaw.health.checks.integrations import check_gws_binary, check_version_update
from pocketpaw.health.checks.result import HealthCheckResult
from pocketpaw.health.checks.storage import (
    check_audit_log_writable,
    check_disk_space,
    check_memory_dir_accessible,
)

# Sync checks (run at startup, fast)
STARTUP_CHECKS = [
    check_config_exists,
    check_config_valid_json,
    check_config_permissions,
    check_api_key_primary,
    check_api_key_format,
    check_backend_deps,
    check_secrets_encrypted,
    check_disk_space,
    check_audit_log_writable,
    check_memory_dir_accessible,
    check_version_update,
]

# Optional integration checks (only useful when specific presets are enabled)
INTEGRATION_CHECKS = [
    check_gws_binary,
]

# Async checks (run in background, may be slow)
CONNECTIVITY_CHECKS = [
    check_llm_reachable,
]

__all__ = [
    "CONNECTIVITY_CHECKS",
    "INTEGRATION_CHECKS",
    "STARTUP_CHECKS",
    "HealthCheckResult",
    "check_api_key_format",
    "check_api_key_primary",
    "check_audit_log_writable",
    "check_backend_deps",
    "check_config_exists",
    "check_config_permissions",
    "check_config_valid_json",
    "check_disk_space",
    "check_gws_binary",
    "check_llm_reachable",
    "check_memory_dir_accessible",
    "check_secrets_encrypted",
    "check_version_update",
]

"""Auth domain — re-exports for backward compatibility.

The router is intentionally NOT re-exported here. Importing it would
trigger ``auth.router`` → ``auth.service`` → ``_core.context`` →
``auth`` (this module) — a circular import. Callers that need the
router must do ``from ee.cloud.auth.router import router`` directly;
``ee/cloud/__init__.py`` already does this.
"""

from pocketpaw_ee.cloud.auth.core import (  # noqa: F401
    SECRET,
    TOKEN_LIFETIME,
    UserCreate,
    UserManager,
    UserRead,
    bearer_backend,
    cookie_backend,
    current_active_user,
    current_optional_user,
    ensure_default_agent_all_workspaces,
    fastapi_users,
    get_jwt_strategy,
    get_user_db,
    get_user_manager,
    seed_admin,
    seed_default_agent,
    seed_workspace,
)
from pocketpaw_ee.cloud.auth.domain import (  # noqa: F401
    AuthUser,
    WorkspaceMembershipRef,
)
from pocketpaw_ee.cloud.auth.dto import (  # noqa: F401
    ProfileOut,
    ProfileUpdateRequest,
    SetWorkspaceRequest,
    auth_user_to_profile_out,
)

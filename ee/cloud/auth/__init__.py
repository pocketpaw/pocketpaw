"""Auth domain — re-exports for backward compatibility."""

from ee.cloud.auth.core import (  # noqa: F401
    SECRET,
    TOKEN_LIFETIME,
    UserCreate,
    UserManager,
    UserRead,
    bearer_backend,
    cookie_backend,
    current_active_user,
    current_optional_user,
    fastapi_users,
    get_jwt_strategy,
    get_user_db,
    get_user_manager,
    seed_admin,
    seed_workspace,
)
from ee.cloud.auth.router import router  # noqa: F401

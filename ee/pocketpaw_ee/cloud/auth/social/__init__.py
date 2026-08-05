"""Consumer social sign-in (Google / GitHub).

Created 2026-07-29 (AM-2). Distinct from ``auth/sso/`` — that is
workspace-scoped enterprise OIDC configured by an admin; this is the
consumer "Continue with Google/GitHub" path on the auth dialog. They share
the single-use state store in ``auth/_oauth_state.py``.
"""

from pocketpaw_ee.cloud.auth.social.router import router

__all__ = ["router"]

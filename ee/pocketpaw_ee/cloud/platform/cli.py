"""Grant, revoke and list platform roles from a shell.

Created: 2026-09-14 (feat/platform-authority-axis) — chunk 1 of the Paw Admin PRD.

    python -m pocketpaw_ee.cloud.platform.cli list
    python -m pocketpaw_ee.cloud.platform.cli grant ops@example.com operator --reason "..."
    python -m pocketpaw_ee.cloud.platform.cli revoke ops@example.com --reason "..."

WHY A CLI AND NOT A ROUTE. Granting platform access through the platform API is
a chicken-and-egg: the route would itself be operator-gated, so the FIRST
operator could never be created through it. There is a
``platform.role.grant`` action registered for the eventual route that lets one
operator promote another, but the bootstrap case needs shell access to the
deployment — which is the correct bar for minting an account that can read every
tenant's data and move their money.

WHY IT LIVES IN THE PACKAGE. The deployed image contains the installed package
and no ``scripts/`` directory, so a standalone script would exist on a
developer's laptop and nowhere it is actually needed. ``python -m`` works
wherever the package is installed.

WHY IT WRITES AN AUDIT ROW. A grant made from a shell is exactly the event
someone will later need to reconstruct, and it is the one path with no HTTP
request behind it. The row records ``actor_id="cli"`` because there is no
authenticated operator — the accountability is the shell access itself, and
pretending otherwise by attributing it to the target would be worse than saying
plainly that it came from a terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from pocketpaw_ee.guards.platform import PlatformRole

# The deployed cloud app reads this, NOT MONGODB_URI. Getting this wrong points
# the tool at a local dev database and reports cheerful success against nothing.
_MONGO_ENV = "CLOUD_MONGODB_URI"
_DEFAULT_URI = "mongodb://localhost:27017/paw-enterprise"


def _mongo_uri() -> str:
    uri = os.environ.get(_MONGO_ENV) or os.environ.get("MONGODB_URI")
    if not uri:
        print(
            f"warning: neither {_MONGO_ENV} nor MONGODB_URI is set; falling back to {_DEFAULT_URI}",
            file=sys.stderr,
        )
        return _DEFAULT_URI
    return uri


async def _connect() -> None:
    from pocketpaw_ee.cloud.shared.db import init_cloud_db

    await init_cloud_db(_mongo_uri())


async def _find_user(email: str):
    from pocketpaw_ee.cloud.models.user import User

    # Case-insensitive: operators type their own address from memory, and a
    # silent "no such user" because of a capital letter is a bad first
    # experience with a tool you reach for during an incident.
    user = await User.find_one({"email": {"$regex": f"^{_escape(email)}$", "$options": "i"}})
    if user is None:
        print(f"error: no user with email {email!r}", file=sys.stderr)
        raise SystemExit(1)
    return user


def _escape(value: str) -> str:
    import re

    return re.escape(value)


async def _audit(user, action: str, reason: str, before: str | None, after: str | None) -> None:
    from pocketpaw_ee.cloud.models.platform_audit import PlatformAuditEvent

    await PlatformAuditEvent(
        actor_id="cli",
        actor_email="",
        actor_platform_role="",
        action=action,
        target_type="user",
        target_workspace=None,
        target_user=str(user.id),
        reason=reason,
        before={"platform_role": before},
        after={"platform_role": after},
        status="applied",
    ).insert()


async def cmd_list() -> int:
    from pocketpaw_ee.cloud.models.user import User

    await _connect()
    holders = await User.find({"platform_role": {"$ne": None}}).to_list()
    if not holders:
        print("No user holds a platform role.")
        print("Grant the first one with: ... grant <email> operator --reason <why>")
        return 0
    width = max(len(u.email) for u in holders)
    for user in sorted(holders, key=lambda u: (u.platform_role or "", u.email)):
        print(f"{user.email:<{width}}  {user.platform_role}")
    return 0


async def cmd_grant(email: str, role: str, reason: str) -> int:
    try:
        resolved = PlatformRole.from_str(role)
    except ValueError:
        valid = ", ".join(r.value for r in PlatformRole)
        print(f"error: unknown platform role {role!r}. Valid: {valid}", file=sys.stderr)
        return 1

    await _connect()
    user = await _find_user(email)
    before = user.platform_role

    if before == resolved.value:
        print(f"{user.email} already holds {resolved.value}; nothing to do.")
        return 0

    user.platform_role = resolved.value
    await user.save()
    await _audit(user, "platform.role.grant", reason, before, resolved.value)

    was = before or "none"
    print(f"{user.email}: {was} -> {resolved.value}")
    return 0


async def cmd_revoke(email: str, reason: str) -> int:
    await _connect()
    user = await _find_user(email)
    before = user.platform_role

    if before is None:
        print(f"{user.email} holds no platform role; nothing to do.")
        return 0

    user.platform_role = None
    await user.save()
    await _audit(user, "platform.role.revoke", reason, before, None)

    print(f"{user.email}: {before} -> none")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pocketpaw_ee.cloud.platform.cli",
        description="Manage platform operator roles.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="Show every user holding a platform role")

    grant = sub.add_parser("grant", help="Grant a platform role")
    grant.add_argument("email")
    grant.add_argument("role", choices=[r.value for r in PlatformRole])
    grant.add_argument("--reason", required=True, help="Why, for the audit trail")

    revoke = sub.add_parser("revoke", help="Remove a user's platform role")
    revoke.add_argument("email")
    revoke.add_argument("--reason", required=True, help="Why, for the audit trail")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "list":
        return asyncio.run(cmd_list())
    if args.command == "grant":
        return asyncio.run(cmd_grant(args.email, args.role, args.reason))
    if args.command == "revoke":
        return asyncio.run(cmd_revoke(args.email, args.reason))

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

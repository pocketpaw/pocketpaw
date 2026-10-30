#!/usr/bin/env bash
# docker-entrypoint.enterprise.sh — first-run secret bootstrap for the
# sovereign per-tenant Enterprise image.
# Created: 2026-06-10 (sov/w1a-deploy).
#
# WHY: W0e's auth boot (ee/cloud/auth/core.py::_resolve_secret) HARD-FAILS in
# production posture (POCKETPAW_ENV=production, which the enterprise image sets)
# unless a real AUTH_SECRET is present in the environment BEFORE the Python
# process imports the auth module. To keep the deploy "dead-simple" — a plain
# `docker compose up` with no hand-crafted secrets — this entrypoint:
#   1. Generates a strong AUTH_SECRET on first run if the operator didn't set
#      one, and PERSISTS it to the data volume (/home/pocketpaw/.pocketpaw) so
#      it is STABLE across restarts. A per-process random secret would
#      invalidate every admin session on every restart, so persistence matters.
#   2. Leaves the initial ADMIN_PASSWORD to W0e's seed_admin(), which already
#      generates a strong one and prints it ONCE to stdout (visible via
#      `docker compose logs`). We do NOT generate it here — duplicating that
#      logic would risk drifting from the in-app gate.
#   3. Does NOT touch AUTH_SECRET if the operator already supplied one (env
#      wins), and does NOT regress the production fail-fast: if generation is
#      somehow disabled and no secret resolves, the app still refuses to boot.
#   4. Loudly WARNs (does not fail) when POCKETPAW_LICENSE_PUBLIC_KEY is unset,
#      because the license layer (W1a) will then refuse to start in production —
#      this surfaces the cause before the stack trace does.
#
# The generated secret lives at $SECRET_FILE with mode 600, owned by the
# non-root pocketpaw user. Delete it (or the volume) to force regeneration.
set -euo pipefail

# The data dir is fixed to the non-root user's ~/.pocketpaw (config.py derives
# it from Path.home(); it is the pocketpaw-data named volume in compose).
# POCKETPAW_DATA_DIR is an entrypoint-local override only, not an app config knob.
SECRET_DIR="${POCKETPAW_DATA_DIR:-/home/pocketpaw/.pocketpaw}"
SECRET_FILE="${SECRET_DIR}/auth_secret"

mkdir -p "${SECRET_DIR}"

# ── AUTH_SECRET: env wins → persisted file → generate + persist ───────────────
if [ -n "${AUTH_SECRET:-}" ]; then
  echo "entrypoint: AUTH_SECRET supplied by operator env — using it." >&2
elif [ -s "${SECRET_FILE}" ]; then
  AUTH_SECRET="$(cat "${SECRET_FILE}")"
  export AUTH_SECRET
  echo "entrypoint: reusing persisted AUTH_SECRET from ${SECRET_FILE}." >&2
else
  AUTH_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
  export AUTH_SECRET
  ( umask 177; printf '%s' "${AUTH_SECRET}" > "${SECRET_FILE}" )
  echo "entrypoint: generated a new AUTH_SECRET and persisted it to ${SECRET_FILE}." >&2
  echo "entrypoint: it is stable across restarts; remove that file to rotate." >&2
fi

# ── License key posture: warn early so the cause is obvious ───────────────────
if [ -z "${POCKETPAW_LICENSE_PUBLIC_KEY:-}" ]; then
  echo "entrypoint: WARNING — POCKETPAW_LICENSE_PUBLIC_KEY is unset. In production" >&2
  echo "entrypoint:   posture the license layer refuses to verify against the" >&2
  echo "entrypoint:   committed DEV key (it is forgeable). Generate a keypair:" >&2
  echo "entrypoint:     docker compose exec app python -m pocketpaw_ee.cloud.mint generate-keypair" >&2
  echo "entrypoint:   then set POCKETPAW_LICENSE_PUBLIC_KEY and mint your license." >&2
fi

# Hand off to the app (or whatever command the operator passed).
exec "$@"

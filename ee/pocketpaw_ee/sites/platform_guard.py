# ee/pocketpaw_ee/sites/platform_guard.py: the platform-owned entry module that wraps
# a Paw Site Worker's own entry. The site author can rewrite their code, but not this
# module or the plain_text vars it reads (platform values win over owner values), so
# the limits that cost US money are enforced here and not only in recipe code.
#
# One wrapper does every job a deploy needs (``bundle_deploy`` adds it after every
# check; drafts reach it through ``draft_worker.guard_module``):
#   * draft key (``draft_key``): 404 unless ``X-Paw-Draft-Key`` matches the per-draft
#     binding, checked FIRST and stripped before the site's code sees the request.
#   * Durable Object tiers (``do_limits``), read from platform vars:
#       - ``PAW_DO_SUSPENDED=1``: 503 for every request the Worker handles, and
#         ``scheduled`` / ``queue`` handlers are skipped. Assets Cloudflare serves
#         before the Worker runs still serve.
#       - ``PAW_DO_THROTTLED=1``: 429 for every WebSocket upgrade.
# ``export *`` re-exports the entry's named exports, so Durable Object classes stay
# exported. Residuals (documented in docs/deployment/sites-bundle-deploys.md): DO
# alarms already scheduled inside an object are not stopped by a fetch wrapper, a
# ``run_worker_first`` asset path gets the 503 while suspended, and the per-room
# peer cap (``ROOM_MAX_PEERS``) is still enforced by the recipe's own code.
from __future__ import annotations

import json
from typing import Any

PLATFORM_GUARD_MODULE = "__paw_platform_guard.mjs"

_SUSPENDED_BODY = (
    "This site is paused for today: it used far more than its daily allowance. "
    "It will be back tomorrow."
)
_THROTTLED_BODY = (
    "Realtime is paused for this site today: it reached its daily usage limit. Try again tomorrow."
)

_TEMPLATE = """// Paw platform entry: wraps the site's own entry. Platform-owned; the site's
// code cannot change this module or the platform vars it reads.
import * as app from __SPEC__;
export * from __SPEC__;
const inner = app.default;
const isClass = typeof inner === "function";
const wrapped = isClass ? {} : { ...inner };
const TEXT = { "content-type": "text/plain; charset=utf-8" };
function call(name, arg, env, ctx) {
  return isClass ? new inner(ctx, env)[name](arg) : inner[name](arg, env, ctx);
}
__KEY_HELPERS__wrapped.fetch = async (request, env, ctx) => {
__KEY_CHECK____LIMITS_FETCH__  return call("fetch", request, env, ctx);
};
__LIMITS_HANDLERS__export default wrapped;
"""

_KEY_HELPERS = """const HEADER = __HEADER__;
const enc = new TextEncoder();
function allowed(got, want) {
  if (typeof got !== "string" || typeof want !== "string" || !want) return false;
  const a = enc.encode(got);
  const b = enc.encode(want);
  return a.byteLength === b.byteLength && crypto.subtle.timingSafeEqual(a, b);
}
"""

_KEY_CHECK = """  if (!allowed(request.headers.get(HEADER), env.__BINDING__)) {
    return new Response("Not found", { status: 404 });
  }
  const headers = new Headers(request.headers);
  headers.delete(HEADER);
  request = new Request(request, { headers });
"""

_LIMITS_FETCH = """  if (env.PAW_DO_SUSPENDED === "1") {
    const headers = { ...TEXT, "retry-after": "3600" };
    return new Response(__SUSPENDED__, { status: 503, headers });
  }
  const upgrade = (request.headers.get("upgrade") || "").toLowerCase();
  if (env.PAW_DO_THROTTLED === "1" && upgrade === "websocket") {
    return new Response(__THROTTLED__, { status: 429, headers: TEXT });
  }
"""

_LIMITS_HANDLERS = """for (const name of ["scheduled", "queue"]) {
  const has = isClass
    ? typeof inner.prototype?.[name] === "function"
    : typeof inner?.[name] === "function";
  if (!has) continue;
  wrapped[name] = async (arg, env, ctx) => {
    if (env.PAW_DO_SUSPENDED === "1") return;
    return call(name, arg, env, ctx);
  };
}
"""


def wrapper_code(
    main_module: str, *, do_limits: bool, draft_key: tuple[str, str] | None = None
) -> str:
    """The wrapper's source. ``draft_key`` is ``(header, binding name)``."""
    code = _TEMPLATE.replace("__SPEC__", json.dumps("./" + main_module))
    if draft_key is not None:
        header, binding = draft_key
        helpers = _KEY_HELPERS.replace("__HEADER__", json.dumps(header))
        code = code.replace("__KEY_HELPERS__", helpers)
        code = code.replace("__KEY_CHECK__", _KEY_CHECK.replace("__BINDING__", binding))
    else:
        code = code.replace("__KEY_HELPERS__", "").replace("__KEY_CHECK__", "")
    if do_limits:
        limits = _LIMITS_FETCH.replace("__SUSPENDED__", json.dumps(_SUSPENDED_BODY)).replace(
            "__THROTTLED__", json.dumps(_THROTTLED_BODY)
        )
        code = code.replace("__LIMITS_FETCH__", limits)
        code = code.replace("__LIMITS_HANDLERS__", _LIMITS_HANDLERS)
    else:
        code = code.replace("__LIMITS_FETCH__", "").replace("__LIMITS_HANDLERS__", "")
    return code


def wrapper_module(
    main_module: str,
    *,
    do_limits: bool,
    draft_key: tuple[str, str] | None = None,
    name: str = PLATFORM_GUARD_MODULE,
) -> Any:
    from pocketpaw_ee.sites.cloudflare_client import WorkerModule, module_content_type

    code = wrapper_code(main_module, do_limits=do_limits, draft_key=draft_key)
    return WorkerModule(name=name, content=code.encode(), content_type=module_content_type(name))


__all__ = ["PLATFORM_GUARD_MODULE", "wrapper_code", "wrapper_module"]

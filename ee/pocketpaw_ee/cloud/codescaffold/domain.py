# domain.py — Pure scaffold rules: the recipe catalog, prompt matching, naming,
# and the capability requirements a composed project implies (CS-1).
#
# Created 2026-07-21 (feat/codescaffold). No I/O, no subprocess, no FastAPI —
# everything here is a pure function of a prompt and the catalog, so the matching
# policy can be tested without node on the box.
#
# THE ONE DECISION WORTH RESTATING. Planning is DETERMINISTIC — keyword rules
# over a three-entry catalog, not a model call. Two reasons, and the second is
# the load-bearing one:
#
#   1. It works today. Every model path in Code Mode is currently blocked on the
#      deployment's transport (see the codeagent notes), and a scaffolder that
#      cannot run is not a scaffolder.
#   2. It is EXPLAINABLE. Every recipe this returns carries the phrase in the
#      user's own prompt that selected it, so the confirmation step can say
#      "I'll set up auth, because you said 'sign-in'" instead of asking someone
#      to trust a black box before it writes files into their project.
#
# That said, this is a floor and not a ceiling: three recipes with distinctive
# vocabulary is exactly the case keyword matching handles well, and it degrades
# the moment the catalog grows. The upgrade path is to replace `match_recipes`
# with a model call that returns the same `(ids, reasons)` pair — the seam is the
# return type, and everything downstream already treats reasons as required.
from __future__ import annotations

import re
from typing import NamedTuple

# ── The catalog ─────────────────────────────────────────────────────────────
# Mirrors `_template/_runner/compose.mjs`'s MANIFESTS by id. The duplication is
# deliberate and the comment there says the same thing from the other side: that
# file decides what the ENGINE can apply, this one decides what a PROMPT is
# allowed to ask for. Adding a recipe should be two conscious edits, because the
# thing being added writes code into somebody's project.

# The only starter today. Named rather than assumed so the wire shape does not
# have to change when a second one appears.
STARTER = "sveltekit-cloudflare"


class Recipe(NamedTuple):
    """One composable feature. `keywords` are matched case-insensitively against
    the prompt on word boundaries; `requires` mirrors the engine's own manifest
    so the plan can report the full closure without shelling out to node."""

    id: str
    capability: str
    summary: str
    keywords: tuple[str, ...]
    requires: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()


CATALOG: tuple[Recipe, ...] = (
    Recipe(
        id="db",
        capability="database",
        summary="A Cloudflare D1 database with Drizzle",
        # "database"/"db" are the direct asks. The rest are the nouns people use
        # when they mean persistence without saying the word: an app that
        # "stores bookings" needs a database whether or not it says so.
        keywords=("database", "db", "sql", "store", "storage", "persist", "table", "records"),
    ),
    Recipe(
        id="auth",
        capability="authentication",
        summary="Email and password accounts, with a protected dashboard",
        keywords=(
            "auth",
            "authentication",
            "sign-in",
            "sign in",
            "signin",
            "sign-up",
            "sign up",
            "signup",
            "login",
            "log in",
            "account",
            "accounts",
            "user",
            "users",
            "register",
            "registration",
        ),
        requires=("db",),
        secrets=("AUTH_SECRET",),
    ),
    Recipe(
        id="stripe",
        capability="payments",
        summary="Stripe Checkout with a verified webhook",
        keywords=(
            "stripe",
            "payment",
            "payments",
            "pay",
            "checkout",
            "billing",
            "subscription",
            "subscriptions",
            "purchase",
            "sell",
        ),
        requires=("db",),
        secrets=("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"),
    ),
)

BY_ID: dict[str, Recipe] = {r.id: r for r in CATALOG}


class Match(NamedTuple):
    """A chosen recipe and the evidence that chose it."""

    id: str
    #: Why it is here — either the prompt phrase that matched, or the recipe
    #: that pulled it in. Never empty; a choice with no reason is a bug.
    reason: str


def _mentions(prompt: str, keyword: str) -> bool:
    """Whole-word, case-insensitive containment.

    Word boundaries matter more than they look. Substring matching puts "pay" in
    "paycheck" and — the one that actually bites — "user" in "users", which is
    fine, versus "db" in "adblock", which is not. Hyphens and spaces in a keyword
    are treated as interchangeable so "sign-in", "sign in" and "signin" all land.
    """
    pattern = (
        r"\b" + r"[\s\-]?".join(re.escape(part) for part in re.split(r"[\s\-]+", keyword)) + r"\b"
    )
    return re.search(pattern, prompt, re.IGNORECASE) is not None


def match_recipes(prompt: str) -> list[Match]:
    """Pick the recipes a prompt is asking for, with the closure of `requires`.

    Returned in dependency order (a required recipe before the one that needs
    it), which is the order the engine will apply them in — so the confirmation
    UI reads the same way the build runs.

    A dependency pulled in implicitly is reported as such rather than silently
    added: "a booking app with sign-in" selects `auth` on the word "sign-in" and
    `db` because auth needs it, and the user should see both.
    """
    direct: list[Match] = []
    for recipe in CATALOG:
        hit = next((k for k in recipe.keywords if _mentions(prompt, k)), None)
        if hit is not None:
            direct.append(Match(recipe.id, f'you said "{hit}"'))

    chosen: dict[str, str] = {}

    def add(recipe_id: str, reason: str) -> None:
        # First reason wins: a direct prompt match is more useful to show than
        # "required by X", and directs are walked first.
        if recipe_id in chosen:
            return
        chosen[recipe_id] = reason
        for dep in BY_ID[recipe_id].requires:
            add(dep, f"needed by {recipe_id}")

    for match in direct:
        add(match.id, match.reason)

    return [Match(r.id, chosen[r.id]) for r in CATALOG if r.id in chosen]


def secrets_for(recipe_ids: list[str]) -> list[str]:
    """The secret NAMES a composed project will need. Names only — that is the
    template's contract and the reason a composed project can be handed around
    without carrying anything sensitive. Values are CE-track work and never enter
    the source map."""
    seen: list[str] = []
    for rid in recipe_ids:
        for name in BY_ID[rid].secrets if rid in BY_ID else ():
            if name not in seen:
                seen.append(name)
    return seen


class Requirements(NamedTuple):
    """What a composed project needs from a RUNTIME. Mirrors
    `websandbox.dto.RuntimeRequirementsResponse` field for field, deliberately:
    the registry already matches that shape against adapter capabilities, and a
    second vocabulary for the same question would need a translation layer that
    could disagree with itself."""

    install: bool
    nativeToolchain: bool
    rawSockets: bool
    reasons: list[str]


def requirements_for(recipe_ids: list[str]) -> Requirements:
    """The capability demands of a composed project.

    This is the plan emitting REQUIREMENTS rather than picking a runtime — the
    whole point of the registry. Nothing here names Daytona or WebContainers;
    the matcher decides, and it will decide differently the day an in-tab runtime
    can run workerd.

    Every flag that is true carries a reason, per the `reasons` discipline
    `websandbox/requirements.py` established: a routing decision the user can see
    (fast in-tab runtime versus a slower VM) but not have explained is a decision
    nobody can debug.
    """
    reasons = [
        "the project installs its dependencies from npm -> install",
        # True for the BASE, before any recipe. SvelteKit builds through Vite,
        # which resolves to esbuild/rollup native binaries.
        "the SvelteKit build runs Vite (esbuild/rollup native binaries) -> nativeToolchain",
    ]
    # Every composed project targets Cloudflare Workers, so `wrangler dev` —
    # and therefore workerd, a native binary — is on the path for all of them,
    # not just the ones that asked for a database. Stated separately from the
    # Vite reason because it survives even if the build toolchain changes.
    reasons.append(
        "it runs on Cloudflare Workers, so the dev server is wrangler/workerd -> nativeToolchain"
    )
    if "db" in recipe_ids:
        # Worth its own line even though the flag is already true: when the
        # matcher rejects an in-tab runtime, "your database needs it" is the
        # reason a user will recognise.
        reasons.append("D1 is served by workerd in local dev -> nativeToolchain")

    return Requirements(
        install=True,
        nativeToolchain=True,
        # D1 is reached through `platform.env.DB`, a binding — NOT a TCP
        # connection string. This is the flag a Postgres-backed template would
        # raise and this one genuinely does not.
        rawSockets=False,
        reasons=reasons,
    )


# ── Naming ──────────────────────────────────────────────────────────────────

# Words that carry no identity. Dropped so "a booking app with sign-in" names
# itself "booking" rather than "a-booking-app-with".
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "app",
        "application",
        "site",
        "website",
        "page",
        "build",
        "make",
        "create",
        "with",
        "and",
        "for",
        "that",
        "this",
        "my",
        "me",
        "please",
        "using",
        "use",
        "it",
        "to",
        "of",
        "in",
        "on",
        "simple",
        "basic",
        "small",
    }
)

_MAX_NAME_WORDS = 3
_MAX_NAME_CHARS = 40

#: Used when a prompt yields no usable words, and by the compose path when the
#: caller sends no name. Public because both callers need the same answer.
FALLBACK_PROJECT_NAME = "new-project"


def _feature_words() -> frozenset[str]:
    """Every word appearing in a recipe keyword.

    Excluded from names because they describe what the project HAS, not what it
    IS — the recipe list already says "auth" and "payments", so repeating it in
    the name costs the only three words available. "a booking app with sign-in"
    should be `booking`, not `booking-sign`.
    """
    words: set[str] = set()
    for recipe in CATALOG:
        for keyword in recipe.keywords:
            words.update(re.split(r"[\s\-]+", keyword.lower()))
    return frozenset(words)


def derive_project_name(prompt: str) -> str:
    """A short kebab-case name from the prompt.

    Deliberately dumb and deliberately bounded: this becomes a directory name and
    a wrangler worker name, so it must be a safe slug no matter what was typed —
    including an empty prompt, an emoji, or a paragraph.
    """
    words = [w for w in re.split(r"[^A-Za-z0-9]+", prompt.lower()) if w]
    skip = _STOPWORDS | _feature_words()
    kept = [w for w in words if w not in skip][:_MAX_NAME_WORDS]
    # Nothing but stopwords and feature words ("build me an app with sign-in").
    # There is no identity in that prompt, so name it rather than assembling a
    # slug out of the noise.
    if not kept:
        return FALLBACK_PROJECT_NAME
    name = "-".join(kept)[:_MAX_NAME_CHARS].strip("-")
    # A name must not start with a digit: it is used as a package/worker name.
    if name and name[0].isdigit():
        name = f"p-{name}"
    return name or FALLBACK_PROJECT_NAME


__all__ = [
    "BY_ID",
    "FALLBACK_PROJECT_NAME",
    "CATALOG",
    "Match",
    "Recipe",
    "Requirements",
    "STARTER",
    "derive_project_name",
    "match_recipes",
    "requirements_for",
    "secrets_for",
]

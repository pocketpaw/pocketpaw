# domain.py — The starter catalog and prompt matching (CS-1, rewritten CS-1b).
#
# Created 2026-07-21. REWRITTEN 2026-07-22: the vendored SvelteKit/D1 template and
# its recipe engine are gone; starters now come from PINNED, OFFICIAL npm
# tarballs. The old approach is recoverable at commit d233253f if the recipe
# depth is ever wanted back.
#
# ── Why npm tarballs and not `git clone` ────────────────────────────────────
# The obvious reading of "just clone a GitHub repo" does not survive contact with
# these projects. Measured, not assumed:
#
#   * React and Vue have NO standalone template repo — they live inside
#     `vitejs/vite`, whose tarball is 12.9 MB to obtain a 20 KB template.
#   * Next.js is the same story inside `vercel/next.js`: 51 MB.
#   * `sveltejs/kit-template-default` does exist standalone, but its last push
#     was 2025-11-21 — eight months stale.
#
# Every one of these projects DOES ship its templates as real files inside a
# small, versioned npm package: `create-vite` is 81 KB and carries sixteen of
# them. That package is literally the bytes `npm create vite` writes, it is
# pinnable (a git branch moves under you; a version does not), and it arrives as
# a tarball — which is the shape the runtime materializer already consumes.
#
# ── What was given up ───────────────────────────────────────────────────────
# The recipes. `auth` and `stripe` used to arrive working; a Vite starter is a
# blank app. The agent has to write those features itself now, against a scaffold
# rather than onto one. That is the trade this rewrite makes, deliberately.
from __future__ import annotations

import re
from typing import NamedTuple

#: Bumped when the extraction shape changes, so a cached tarball from an older
#: build cannot be reused with new extraction rules.
CATALOG_EPOCH = "v1"


class Starter(NamedTuple):
    """One framework starter, sourced from a pinned npm package.

    ``integrity`` is the registry's own Subresource-Integrity string and is
    VERIFIED before a single byte is extracted. This code ends up running in a
    user's sandbox, so "we downloaded whatever the registry served" is not good
    enough — a pinned version without a hash still trusts the network.

    ``dotfile_prefix`` is per-package on purpose and is not a detail: npm strips
    a real ``.gitignore`` out of a published tarball, so every one of these
    projects smuggles it under a different alias — ``_gitignore`` in create-vite,
    ``gitignore`` in create-next-app. Get it wrong and every scaffolded project
    commits ``node_modules`` on its first commit.
    """

    id: str
    label: str
    summary: str
    package: str
    version: str
    integrity: str
    #: Path inside the extracted tarball, below the npm ``package/`` root.
    subdir: str
    #: Filename prefix the package uses to smuggle dotfiles past npm.
    dotfile_prefix: str
    keywords: tuple[str, ...]
    #: The port this starter's dev server listens on by default.
    dev_port: int
    #: Files WE supply, merged over the extracted tree.
    #:
    #: Needed by exactly one starter and worth the field. `create-next-app` ships
    #: no `package.json` at all — its CLI GENERATES one, resolving `next` and
    #: `react` versions at run time from flags. Pure extraction therefore yields
    #: a Next project with no dependencies and no `dev` script. Supplying it here
    #: keeps the fix visible and refreshable instead of hidden in a code path.
    extra_files: tuple[tuple[str, str], ...] = ()


# The `package.json` create-next-app WOULD have generated. It ships none of its
# own — the CLI writes one at run time, resolving `next` and `react` from npm —
# so pure extraction yields a Next project with no dependencies and no `dev`
# script. Caret ranges rather than exact pins, so `npm install` picks up patches
# and this constant is not the thing that goes stale. Kept whole and readable
# because the day it needs updating, it needs reading first.
_NEXT_PACKAGE_JSON = """{
  "name": "app",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "dev": "next dev",
    "build": "next build",
    "start": "next start"
  },
  "dependencies": {
    "next": "^15.5.0",
    "react": "^19.0.0",
    "react-dom": "^19.0.0"
  },
  "devDependencies": {
    "@tailwindcss/postcss": "^4",
    "@types/node": "^20",
    "@types/react": "^19",
    "@types/react-dom": "^19",
    "tailwindcss": "^4",
    "typescript": "^5"
  }
}
"""


# ── The catalog ─────────────────────────────────────────────────────────────
# Versions are pinned to the newest release at least SEVEN DAYS old, per the
# workspace supply-chain rule. That is why `create-next-app` is pinned to 15.5.20
# rather than 16.2.11 — the 16.x line was published inside the window. Refresh
# with `scripts/refresh_starters.py`, which re-checks the age rule.
STARTERS: tuple[Starter, ...] = (
    Starter(
        id="react",
        label="React",
        summary="React 19 with Vite and TypeScript",
        package="create-vite",
        version="9.1.1",
        integrity=(
            "sha512-5iqlfg6gmxRLxkYu4lZDcdeLj32usAvyec9Hb47j4OYcuSyHRwwjLi3s"
            "bi7bcyV9QrFomWDsXhamkJzwgutNpQ=="
        ),
        subdir="template-react-ts",
        dotfile_prefix="_",
        keywords=("react", "reactjs", "react.js", "jsx", "tsx"),
        dev_port=5173,
    ),
    Starter(
        id="vue",
        label="Vue",
        summary="Vue 3 with Vite and TypeScript",
        package="create-vite",
        version="9.1.1",
        integrity=(
            "sha512-5iqlfg6gmxRLxkYu4lZDcdeLj32usAvyec9Hb47j4OYcuSyHRwwjLi3s"
            "bi7bcyV9QrFomWDsXhamkJzwgutNpQ=="
        ),
        subdir="template-vue-ts",
        dotfile_prefix="_",
        # `create-vue` is deliberately NOT the source here: it composes fragments
        # (base + router + pinia) at generation time rather than shipping a flat
        # template, so there is nothing in it to extract.
        keywords=("vue", "vuejs", "vue.js"),
        dev_port=5173,
    ),
    Starter(
        id="svelte",
        label="Svelte",
        summary="Svelte 5 with Vite and TypeScript",
        package="create-vite",
        version="9.1.1",
        integrity=(
            "sha512-5iqlfg6gmxRLxkYu4lZDcdeLj32usAvyec9Hb47j4OYcuSyHRwwjLi3s"
            "bi7bcyV9QrFomWDsXhamkJzwgutNpQ=="
        ),
        subdir="template-svelte-ts",
        dotfile_prefix="_",
        # This is Svelte, NOT SvelteKit, and that is a real limitation rather than
        # a preference. SvelteKit's official scaffolder (`sv`) does not ship flat
        # templates — its `dist/templates/minimal` is a generator manifest
        # (`files.types=typescript.json` + an `assets/` tree with no package.json)
        # that only the CLI can assemble. Extracting it would mean reimplementing
        # that assembly, which is the vendoring problem this rewrite removed.
        keywords=("svelte", "sveltekit", "svelte.js"),
        dev_port=5173,
    ),
    Starter(
        id="next",
        label="Next.js",
        summary="Next.js App Router with TypeScript and Tailwind",
        package="create-next-app",
        version="15.5.20",
        integrity=(
            "sha512-EtVdrmqQffcjdP0QafaiEEXZ5rr/Bqj7+L6ElHexBuAOG7zVgB4MQdsV"
            "7M9r1a+kznFZ6+wn2HyGAF2J/xcG/Q=="
        ),
        subdir="dist/templates/app-tw/ts",
        # create-next-app ships it as a bare "gitignore" — no underscore. A
        # single shared prefix across the catalog would silently miss this one.
        dotfile_prefix="",
        keywords=("next", "nextjs", "next.js", "ssr", "app router"),
        dev_port=3000,
        # The ranges create-next-app 15.5.x resolves for a TypeScript + Tailwind
        # app. Carets rather than exact pins, so `npm install` picks up patches
        # without this file being the thing that goes stale.
        extra_files=(("package.json", _NEXT_PACKAGE_JSON),),
    ),
)

BY_ID: dict[str, Starter] = {s.id: s for s in STARTERS}

#: What an unrecognised prompt gets. React is the widest-reach default, and
#: guessing beats refusing — the user is shown the choice and can change it
#: before anything is built.
DEFAULT_STARTER_ID = "react"


class Match(NamedTuple):
    """The chosen starter and the evidence that chose it."""

    starter: Starter
    reason: str
    #: False when nothing in the prompt matched and the default was used. The UI
    #: should present a guess differently from a match.
    matched: bool


def _mentions(prompt: str, keyword: str) -> bool:
    """Whole-word, case-insensitive containment.

    Boundaries matter: substring matching finds "vue" inside "revue" and — the
    one that would actually bite — "next" inside "nextdoor". Dots and spaces in a
    keyword are treated as optional so "next.js", "nextjs" and "next js" all land.
    """
    parts = [re.escape(p) for p in re.split(r"[\s.]+", keyword) if p]
    pattern = r"(?<![A-Za-z0-9])" + r"[\s.]?".join(parts) + r"(?![A-Za-z0-9])"
    return re.search(pattern, prompt, re.IGNORECASE) is not None


def match_starter(prompt: str) -> Match:
    """Pick the starter a prompt is asking for.

    Deterministic keyword matching rather than a model call: it runs today, and
    it is explainable — the user is told which of their own words chose the
    framework, before anything is written into their project.

    Ordering note: `next` is checked BEFORE the Vite family. "a Next.js app with
    React" names both, and Next is the more specific claim — it already IS React.
    """
    for starter in _MATCH_ORDER:
        hit = next((k for k in starter.keywords if _mentions(prompt, k)), None)
        if hit is not None:
            return Match(starter, f'you said "{hit}"', True)

    default = BY_ID[DEFAULT_STARTER_ID]
    return Match(default, "no framework was named, so this is the default", False)


#: `next` first — see `match_starter`. Everything else keeps catalog order.
_MATCH_ORDER: tuple[Starter, ...] = (
    BY_ID["next"],
    BY_ID["react"],
    BY_ID["vue"],
    BY_ID["svelte"],
)


class Requirements(NamedTuple):
    """What a starter needs from a RUNTIME. Mirrors
    `websandbox.dto.RuntimeRequirementsResponse` field for field so the client's
    existing capability matcher consumes it unchanged."""

    install: bool
    nativeToolchain: bool
    rawSockets: bool
    reasons: list[str]


def requirements_for(starter: Starter) -> Requirements:
    """The capability demands of a starter.

    Emits REQUIREMENTS, never a runtime name — the client's registry decides. The
    interesting change since the Cloudflare template: none of these starters needs
    `workerd`. That template could only ever run in a VM; a Vite or Next dev
    server runs perfectly well in an in-tab WebContainer, which is precisely what
    WebContainers were built to demo.

    `nativeToolchain` is still true, because Vite and Next both resolve to
    esbuild/rollup binaries at install time — and the WebContainers adapter
    honestly declares it can run those.
    """
    return Requirements(
        install=True,
        nativeToolchain=True,
        # No database driver, no connection string. This is the flag that would
        # rule out an in-tab runtime, and none of these starters raises it.
        rawSockets=False,
        reasons=[
            "the project installs its dependencies from npm -> install",
            f"the {starter.label} build resolves esbuild/rollup native binaries -> nativeToolchain",
        ],
    )


# ── Naming ──────────────────────────────────────────────────────────────────

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
        "project",
        "new",
        "some",
        "clone",
    }
)

_MAX_NAME_WORDS = 3
_MAX_NAME_CHARS = 40

#: Used when a prompt yields no usable words, and by compose when the caller
#: sends no name.
FALLBACK_PROJECT_NAME = "new-project"


def _framework_words() -> frozenset[str]:
    """Every word appearing in a starter keyword.

    Excluded from names because they describe what the project is BUILT WITH, not
    what it IS — the starter is already named in the plan, so repeating it spends
    one of only three name words. "a react booking app" should be `booking`.
    """
    words: set[str] = set()
    for starter in STARTERS:
        for keyword in starter.keywords:
            words.update(re.split(r"[\s.]+", keyword.lower()))
    return frozenset(words)


def derive_project_name(prompt: str) -> str:
    """A short kebab-case name from the prompt.

    This becomes a directory name and a `package.json` name, so it must be a safe
    slug for ANY input — an empty prompt, an emoji, a paragraph, a path traversal.
    """
    words = [w for w in re.split(r"[^A-Za-z0-9]+", prompt.lower()) if w]
    skip = _STOPWORDS | _framework_words()
    kept = [w for w in words if w not in skip][:_MAX_NAME_WORDS]
    if not kept:
        return FALLBACK_PROJECT_NAME
    name = "-".join(kept)[:_MAX_NAME_CHARS].strip("-")
    # npm package names may not begin with a digit or a dot.
    if name and not name[0].isalpha():
        name = f"p-{name}"
    return name or FALLBACK_PROJECT_NAME


__all__ = [
    "BY_ID",
    "CATALOG_EPOCH",
    "DEFAULT_STARTER_ID",
    "FALLBACK_PROJECT_NAME",
    "Match",
    "Requirements",
    "STARTERS",
    "Starter",
    "derive_project_name",
    "match_starter",
    "requirements_for",
]

# tests/ee/sites/project_cli_fake.py — a stand-in for the paw-sites CLI the project
# tools shell out to (``starters`` / ``recipes`` / ``template-copy`` / ``apply-recipe``).
# It replaces ``project_tools._create_subprocess_exec``, reads the real argv, and acts
# on the real temp dirs the tools create, so the tests exercise the argv, the temp-dir
# materialize / read-back and the JSON parsing, not a mocked-out helper.
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TEMPLATE_FILES: dict[str, str] = {
    "package.json": json.dumps(
        {"name": "app", "dependencies": {"astro": "7.3.0"}, "scripts": {"build": "astro build"}},
        indent=2,
    )
    + "\n",
    "AGENTS.md": "# AGENTS.md: astro template\n\nPages go in src/pages.\n",
    "src/pages/index.astro": "---\n---\n<h1>Hello</h1>\n",
    "wrangler.jsonc": '{\n  "name": "app"\n}\n',
    "bun.lock": '{"lockfileVersion": 1}\n',
    ".dev.vars.example": "",
}

STARTERS = [
    {
        "slug": slug,
        "name": slug.title(),
        "summary": f"The {slug} template.",
        "when_to_use": [f"the user asks for {slug}"],
        "stack": [f"{slug}@1"],
        "target": "worker",
        "backend": {"builtin": []},
        "recipes": ["d1-drizzle", "better-auth", "r2"],
    }
    for slug in ("astro", "tanstack-start", "vite-react-hono", "next", "sveltekit")
]

RECIPES = [
    {
        "id": "d1-drizzle",
        "name": "D1 with Drizzle",
        "summary": "A D1 database through Drizzle.",
        "applies_to": ["astro", "tanstack-start", "vite-react-hono", "next", "sveltekit"],
        "plan": "free",
        "bindings": [{"type": "d1", "name": "DB"}],
        "secrets": [],
        "env": [],
    },
    {
        "id": "better-auth",
        "name": "Accounts with better-auth",
        "summary": "Email and password accounts.",
        "applies_to": ["astro", "tanstack-start", "vite-react-hono", "next", "sveltekit"],
        "requires": ["d1-drizzle"],
        "plan": "free",
        "bindings": [],
        "secrets": [
            {"name": "BETTER_AUTH_SECRET", "description": "Signs cookies.", "required": True}
        ],
        "env": [{"name": "BETTER_AUTH_URL", "description": "Origin.", "public": True}],
    },
    {
        "id": "r2",
        "name": "R2 storage",
        "summary": "An R2 bucket for uploads.",
        "applies_to": ["astro", "tanstack-start", "vite-react-hono", "next", "sveltekit"],
        "plan": "site",
        "bindings": [{"type": "r2", "name": "UPLOADS"}],
        "secrets": [],
        "env": [],
    },
    {
        "id": "realtime-room",
        "name": "Realtime room",
        "summary": "Multiplayer rooms over WebSockets on a SQLite Durable Object.",
        "applies_to": ["astro", "next", "tanstack-start", "vite-react-hono"],
        "unsupported": {
            "sveltekit": "adapter-cloudflare writes its worker to the wrangler `main` and "
            "cannot export a Durable Object class from it."
        },
        "plan": "site",
        "bindings": [{"type": "do", "name": "ROOM", "class_name": "Room"}],
        "migrations": [{"tag": "realtime-room-v1", "new_sqlite_classes": ["Room"]}],
        "secrets": [],
        "env": [],
    },
]


class _Proc:
    def __init__(self, stdout: dict[str, Any] | None, code: int = 0) -> None:
        self._stdout = (json.dumps(stdout) + "\n").encode() if stdout is not None else b""
        self.returncode = code
        self.pid = 4242

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""

    def kill(self) -> None:
        pass

    async def wait(self) -> int:
        return self.returncode


def _arg(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


class FakeCli:
    """Plays the CLI. ``mode`` switches apply-recipe to ``conflict`` / ``error``."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.mode = "ok"
        self.template_files = dict(TEMPLATE_FILES)
        self.seen_project: dict[str, str] = {}

    async def __call__(self, *argv: str, **_kw: Any) -> _Proc:
        args = list(argv)
        self.calls.append(args)
        for cmd in ("starters", "recipes", "template-copy", "apply-recipe"):
            if cmd in args:
                return getattr(self, cmd.replace("-", "_"))(args)
        return _Proc({"error": "unknown command"}, 2)

    def starters(self, _args: list[str]) -> _Proc:
        return _Proc({"starters": STARTERS, "invalid": []})

    def recipes(self, _args: list[str]) -> _Proc:
        return _Proc({"recipes": RECIPES, "invalid": []})

    def template_copy(self, args: list[str]) -> _Proc:
        slug = args[args.index("template-copy") + 1]
        out = Path(_arg(args, "--out") or "")
        for rel, text in self.template_files.items():
            target = out / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
        return _Proc(
            {"ok": True, "slug": slug, "dir": str(out), "files": sorted(self.template_files)}
        )

    def apply_recipe(self, args: list[str]) -> _Proc:
        recipe_id = args[args.index("apply-recipe") + 1]
        root = Path(_arg(args, "--template-dir") or "")
        self.seen_project = {
            p.relative_to(root).as_posix(): p.read_text("utf-8")
            for p in root.rglob("*")
            if p.is_file()
        }
        base = {
            "recipe": recipe_id,
            "version": "1.0.0",
            "template": _arg(args, "--template"),
            "plan": "free",
            "dryRun": "--dry-run" in args,
            "alreadyApplied": False,
            "written": [],
            "unchanged": [],
            "conflicts": [],
            "errors": [],
            "packagesAdded": [],
            "migrations": [],
            "bindingRequests": [],
            "secretRequests": [
                {"name": "BETTER_AUTH_SECRET", "description": "Signs cookies.", "required": True}
            ],
            "envRequests": [{"name": "BETTER_AUTH_URL", "description": "Origin."}],
            "agentTasks": [{"task": "Wire getUser.", "file": "src/server/auth/index.ts"}],
            "verify": [],
        }
        if self.mode == "conflict":
            # A file left on disk and named in ``written``: the tools must still take
            # nothing from a result that is not ok.
            (root / "AGENTS.md").write_bytes(b"# half-applied\n")
            return _Proc(
                {
                    **base,
                    "ok": False,
                    "written": ["AGENTS.md"],
                    "conflicts": [
                        {
                            "path": "src/server/auth/index.ts",
                            "message": "exists with different content",
                        }
                    ],
                },
                1,
            )
        if self.mode == "error":
            return _Proc({**base, "ok": False, "errors": ['requires recipe "d1-drizzle"']}, 1)
        written = [
            "AGENTS.md",
            "package.json",
            "paw.recipes.json",
            "src/server/auth/better-auth.ts",
        ]
        if base["dryRun"]:
            return _Proc({**base, "ok": True, "written": written})
        pkg = json.loads((root / "package.json").read_text("utf-8"))
        pkg.setdefault("dependencies", {})["better-auth"] = "1.7.7"

        def write(rel: str, text: str) -> None:  # bytes: no newline translation on Windows
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(text.encode("utf-8"))

        write("package.json", json.dumps(pkg, indent=2) + "\n")
        agents = (root / "AGENTS.md").read_bytes().decode("utf-8")
        write("AGENTS.md", agents + "\n## better-auth\n")
        write("paw.recipes.json", json.dumps({"template": "astro", "recipes": [{"id": recipe_id}]}))
        write("src/server/auth/better-auth.ts", "export const auth = 1;\n")
        return _Proc({**base, "ok": True, "written": written, "packagesAdded": ["better-auth"]})

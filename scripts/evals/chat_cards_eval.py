"""Local eval: does the Paw OS chat agent pick Ripple's purpose-built widget?

Runs the Ripple gallery prompts (ripple's recorded fixtures) through the chat
system prompt pocketpaw really builds (``build_behavior_instructions`` for the
claude_agent_sdk backend, plus the per-turn ``chat_turn_widgets`` block under
the knowledge-base header, the way ``run_core`` passes it), on the local Claude
Code CLI headless (empty temp cwd, like ripple's ``record-scenario.ts``), with
no built-in tools and one stdio MCP server, ``pocketpaw_widgets``, serving the
real ``get_widget_spec`` / ``get_inline_widget_help`` tools (this script with
``--mcp-serve``), as the claude_agent_sdk backend does in-process. The reply goes through ``run_core``'s own fence
extraction (normalizer + card gate) and the table shows, per prompt, the card's
main widget, node count, gate verdict and PASS when the main widget is the
gallery's purpose-built one and the gate passes.

Spends Claude Code usage, so nothing runs it implicitly and no test imports it.
Results go to ``--out`` (default: a temp dir), never into git.

    uv run python scripts/evals/chat_cards_eval.py                # 10 prompts, 1 run
    uv run python scripts/evals/chat_cards_eval.py --runs 2 --only bike-gears,plane-3d
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# Gallery id -> the purpose-built widget(s) the gallery answers with.
EXPECTED: dict[str, set[str]] = {
    "bike-gears": {"figure"},
    "heart-pump": {"figure"},
    "plane-3d": {"model-viewer"},
    "tokyo-trip": {"itinerary"},
    "bill-splitter": {"bill-split"},
    "savings-calculator": {"growth-projection"},
    "flashcards": {"flashcard-deck"},
    "hiit-workout": {"interval-workout"},
    "meal-plan": {"meal-plan"},
    # The gallery composed a form; a question card or a booking is as good.
    "book-appointment": {"ask-user-questions", "form", "form-layout", "booking", "wizard-layout"},
}
FIXTURES = "packages/svelte/src/routes/live/fixtures"


def _ripple_repo(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    here = Path(__file__).resolve()
    for base in here.parents:
        if (base / "ripple" / ".git").exists():
            return base / "ripple"
    sys.exit("ripple repo not found; pass --ripple <path>")


def _fixture(repo: Path, ref: str, fid: str) -> dict[str, Any]:
    raw = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{FIXTURES}/{fid}.json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(raw)


def _nodes(node: Any) -> list[dict[str, Any]]:
    """Every node of a ui tree, breadth first."""
    out, queue = [], [node]
    while queue:
        n = queue.pop(0)
        if isinstance(n, dict) and "type" in n:
            out.append(n)
            queue.extend(c for c in n.get("children") or [] if isinstance(c, dict))
            tpl = n.get("template")
            if isinstance(tpl, dict):
                queue.append(tpl)
    return out


def _main_widget(ui: Any) -> str:
    """The first node, breadth first, that is not a layout atom (CORE)."""
    from pocketpaw_ee.paw_bar.widget_relevance import CORE

    for n in _nodes(ui):
        if n["type"] not in CORE:
            return n["type"]
    nodes = _nodes(ui)
    return nodes[0]["type"] if nodes else "-"


def system_prompt(message: str) -> str:
    """The chat agent's system prompt for ``message``: instructions, then the
    knowledge layer holding the per-turn widget block (empty before the fix)."""
    from pocketpaw_ee.cloud.chat import agent_service as svc

    from pocketpaw.prompt.passthrough import _KNOWLEDGE_HEADER

    ctx = svc.ScopeContext(
        kind=svc.ScopeKind.DM,
        scope_id="eval",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
    )
    behavior = svc.build_behavior_instructions(ctx, backend_name="claude_agent_sdk")
    turn = getattr(svc, "chat_turn_widgets", lambda *_a, **_k: "")(behavior, message, [])
    return behavior + (f"\n\n{_KNOWLEDGE_HEADER}{turn}" if turn else "")


def mcp_serve() -> None:
    """Serve the real widget lookup tools over stdio MCP."""
    from mcp.server.fastmcp import FastMCP

    from pocketpaw.tools.builtin.widget_spec import InlineWidgetHelpTool, WidgetSpecTool

    server = FastMCP("pocketpaw_widgets")

    def bind(tool: Any) -> Any:
        async def call(types: list[str]) -> str:
            return await tool.execute(types=types)

        return call

    for tool in (WidgetSpecTool(), InlineWidgetHelpTool()):
        server.add_tool(bind(tool), name=tool.name, description=tool.description)
    server.run()


def run_one(fid: str, prompt: str, model: str, out: Path, run: int) -> dict[str, Any]:
    from pocketpaw_ee.cloud.chat.runs.run_core import _extract_ripple_attachment
    from pocketpaw_ee.paw_bar.card_spec import rejection_reason, render_card
    from pocketpaw_ee.paw_bar.chat_profile import CHAT_PROFILE

    system = system_prompt(prompt)
    with tempfile.TemporaryDirectory(prefix="chat-cards-eval-") as cwd:
        sys_file = Path(cwd) / "system.txt"
        sys_file.write_text(system, encoding="utf-8")
        mcp = {
            "mcpServers": {
                "pocketpaw_widgets": {
                    "command": sys.executable,
                    "args": [str(Path(__file__).resolve()), "--mcp-serve"],
                }
            }
        }
        t0 = time.monotonic()
        proc = subprocess.run(
            [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "json",
                "--model",
                model,
                "--system-prompt-file",
                str(sys_file),
                "--tools",
                "",
                "--mcp-config",
                json.dumps(mcp),
                "--allowedTools",
                "mcp__pocketpaw_widgets__get_widget_spec,"
                "mcp__pocketpaw_widgets__get_inline_widget_help",
                "--setting-sources",
                "local",
                "--strict-mcp-config",
                "--disable-slash-commands",
                "--no-session-persistence",
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=600,
        )
        secs = time.monotonic() - t0
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        envelope = {"result": "", "error": proc.stderr[-400:]}
    text = str(envelope.get("result") or "")
    (out / f"{fid}.{run}.txt").write_text(text, encoding="utf-8")
    _rest, spec = _extract_ripple_attachment(text)
    row: dict[str, Any] = {
        "id": fid,
        "run": run,
        "secs": round(secs),
        "chars": len(system),
        "cost": envelope.get("total_cost_usd"),
        "turns": envelope.get("num_turns"),
        "main": "-",
        "types": [],
        "nodes": 0,
        "gate": "no card",
        "pass": False,
    }
    if isinstance(spec, dict) and isinstance(spec.get("ui"), dict):
        nodes = _nodes(spec["ui"])
        body = json.dumps(spec)
        verbs = CHAT_PROFILE.host_events
        ok = render_card(body, None, verbs=verbs, profile=CHAT_PROFILE) is not None
        main = _main_widget(spec["ui"])
        row.update(
            main=main,
            types=sorted({n["type"] for n in nodes}),
            nodes=len(nodes),
            gate="PASS"
            if ok
            else f"refused: {rejection_reason(body, verbs=verbs, profile=CHAT_PROFILE)}",
        )
        row["pass"] = ok and main in EXPECTED[fid]
    return row


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ripple", help="ripple repo (default: found above this file)")
    ap.add_argument("--ref", default="origin/main")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--only", help="comma-separated fixture ids")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", help="where replies and results.json go (default: temp dir)")
    ap.add_argument("--mcp-serve", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.mcp_serve:
        return mcp_serve()
    logging.disable(logging.WARNING)

    repo = _ripple_repo(args.ripple)
    ids = args.only.split(",") if args.only else list(EXPECTED)
    out = Path(args.out or tempfile.mkdtemp(prefix="chat-cards-eval-out-"))
    out.mkdir(parents=True, exist_ok=True)
    prompts = {fid: _fixture(repo, args.ref, fid)["prompt"] for fid in ids}
    jobs = [(fid, run) for fid in ids for run in range(1, args.runs + 1)]
    with ThreadPoolExecutor(args.jobs) as pool:
        rows = list(pool.map(lambda j: run_one(j[0], prompts[j[0]], args.model, out, j[1]), jobs))

    (out / "results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print("| prompt | run | expected | main widget | nodes | turns | gate | result |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(
            f"| {r['id']} | {r['run']} | {'/'.join(sorted(EXPECTED[r['id']]))[:30]} | "
            f"{r['main']} | {r['nodes']} | {r['turns']} | {r['gate'][:60]} | {'PASS' if r['pass'] else 'FAIL'} |"
        )
    passed = sum(r["pass"] for r in rows)
    cost = sum(r["cost"] or 0 for r in rows)
    print(
        f"\n{passed}/{len(rows)} pass; system prompt {rows[0]['chars']:,} chars (first row); "
        f"${cost:.2f} reported cost; median {sorted(r['secs'] for r in rows)[len(rows) // 2]}s "
        f"per reply; replies in {out}"
    )


if __name__ == "__main__":
    main()

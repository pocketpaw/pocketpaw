# Bundled Claude Code Skills

**Status:** Shipped 2026-05-14 via PR for `feat/pocket-creator-skill`.
**Lives at:** `src/pocketpaw/claude_skills/` (the Python module) and
`src/pocketpaw/claude_skills/_bundled/<skill-name>/` (the skill content).

## What this is

PocketPaw ships **Claude Code SDK skills** — markdown documents that
the Claude Code chat agent can load on demand. They're distinct from
PocketPaw's existing `pocketpaw.skills` module (which integrates with
the AgentSkills / skills.sh ecosystem and is invoked by PocketPaw's
runtime). Claude Code skills are picked up automatically by the
Claude Code CLI when the user types `/skill-name` or when the chat
agent's system prompt nudges it to delegate.

## Why ship them with PocketPaw

The pocket-creation workflow ships with **~12k tokens of design
guidance** (pattern-first decision tree, 150-widget catalog, rich
widget-by-pattern map, composition recipes, canonical examples). If
that content sits in the chat agent's always-on system prompt, every
chat turn pays for it — even turns that have nothing to do with
pockets. By moving it into a skill that loads on demand, the chat
agent's steady-state context drops by ~12k tokens and only pays the
cost when the user actually wants a pocket.

## The auto-install flow

On every PocketPaw dashboard boot, `dashboard_lifecycle.startup_event`
calls `install_bundled_skills()`, which:

1. Iterates `src/pocketpaw/claude_skills/_bundled/<name>/` directories
2. For each bundled skill, mirrors its file tree to
   `~/.claude/skills/<name>/` preserving subdirectory structure
3. Per file, compares SHA-256 hash of source vs destination:
   - Destination missing → copy, status `installed`
   - Hash differs → overwrite, status `updated`
   - Hash matches → no-op, status `skipped`
4. Logs the install summary at INFO. Failures logged at WARNING and
   surfaced via `InstallResult.status == "failed"`, never raised.

The whole operation is best-effort. A permission error on
`~/.claude/skills/` doesn't block dashboard boot or pocket creation —
just means the chat agent falls back to the MCP-tool flow.

## Opt-out

```bash
export POCKETPAW_AUTO_INSTALL_CLAUDE_SKILLS=false
```

Use this if:
- You've manually customized a skill file and don't want PocketPaw to
  overwrite it on the next boot
- You're running in an environment where `~/.claude/skills/` is
  read-only (CI, locked-down machines)
- You want to test pocket creation with the skill disabled

With auto-install off, the MCP-tool surface (`pocket_specialist__create`)
still works — pocket creation just won't benefit from the skill's
loaded-on-demand context economy.

## Manual install

If auto-install is off OR the boot logs show
`Bundled-skills install failed`, you can stage the files manually:

```bash
# from the pocketpaw repo root:
mkdir -p ~/.claude/skills
cp -r src/pocketpaw/claude_skills/_bundled/* ~/.claude/skills/
```

Verify:

```bash
ls ~/.claude/skills/pocketpaw-create-pocket/
# → SKILL.md
```

The next time you chat with the PocketPaw agent and ask to create a
pocket, Claude Code should pick up the skill.

## Adding a new bundled skill

1. Create a directory under
   `src/pocketpaw/claude_skills/_bundled/<your-skill-name>/`
2. Drop a `SKILL.md` inside with YAML frontmatter:

   ```markdown
   ---
   name: <your-skill-name>
   description: |
     One-paragraph description of what the skill does and when the
     chat agent should invoke it.
   ---

   # Workflow

   ...
   ```

3. Re-boot PocketPaw. The installer discovers the new directory
   automatically (no code changes) and copies it to the user's
   `~/.claude/skills/`.

No registration needed — directory iteration is the discovery
primitive.

## Currently bundled

| Name | Purpose |
| --- | --- |
| `pocketpaw-create-pocket` | Pattern-first pocket creation workflow with 150-widget catalog reference, rich-widgets-by-pattern map, and the canonical invocation flow. |

Planned (not yet shipped):
- `pocketpaw-edit-pocket` — edit existing pocket via the edit specialist
- `pocketpaw-audit-pocket` — review an existing pocket for design issues
- `pocketpaw-migrate-dashboard` — convert dashboard-style pockets to the right pattern

## How this fits with the broader two-track strategy

PocketPaw's `claude_skills/` only benefits Claude Code SDK users.
Other backends (codex_cli, openai_agents, deep_agents, langchain_react)
fall back to the inline MCP-tool flow with the full specialist prompt.
The skill is additive — it provides better context economy for one
backend without changing the behavior of the others.

If we later want a comparable optimization for the other backends,
the right shape is probably an opt-in skills file format that the
respective SDKs (OpenAI Assistants, Google ADK, etc.) can parse —
similar to how the AgentSkills format (skills.sh) is consumed by
PocketPaw's `pocketpaw.skills` module today.

## Implementation notes

- Module: `src/pocketpaw/claude_skills/`
- Installer: `src/pocketpaw/claude_skills/installer.py`
- Bundled content: `src/pocketpaw/claude_skills/_bundled/<name>/SKILL.md`
- Config flag: `auto_install_claude_skills: bool = True`
  (`POCKETPAW_AUTO_INSTALL_CLAUDE_SKILLS` env var)
- Wired into: `dashboard_lifecycle.startup_event`
- Tests: `tests/test_claude_skills_installer.py`
- Hatchling config: `src/pocketpaw` already includes non-Python files
  recursively, so `SKILL.md` ships in the wheel without extra config.

# Bundled Skills

**Status:** Shipped 2026-05-14 via PR for `feat/pocket-creator-skill`.
**Lives at:** `src/pocketpaw/bundled_skills/` (the Python installer) and
`src/pocketpaw/bundled_skills/_bundled/<skill-name>/` (the skill content).

## What this is

PocketPaw ships **AgentSkills-format SKILL.md files** (the same format
the skills.sh ecosystem uses — YAML frontmatter + markdown body) and
auto-installs them to `~/.claude/skills/<name>/SKILL.md` on every
dashboard boot. The Python module here is the **shipping side**;
PocketPaw's existing `pocketpaw.skills` module is the **runtime side**
(loader / executor that consumes the installed files).

The install destination (`~/.claude/skills/`) is one of the three
paths PocketPaw's own `SkillLoader.SKILL_PATHS` scans, so bundled
skills are available to the **non-SDK** backends — codex_cli,
openai_agents, deep_agents, langchain_react — via PocketPaw's
slash-command dispatcher in `dashboard_ws.py`, plus the desktop
dashboard.

The default `claude_agent_sdk` backend is the exception: it launches
with `setting_sources=[]` for persona isolation, which disables the
SDK's filesystem skill discovery — `~/.claude/skills/` is invisible to
it (verified 2026-06-03 — a slash hits the SDK as an unknown command
and the run returns with no assistant turn; the SDK `skills=` option is
gated by `setting_sources` too). So that backend gets the bundled
skills a different way: the `_bundled/` directory is also a Claude Code
**local plugin** (`.claude-plugin/plugin.json` + `skills/`), and the
backend passes it via the SDK `plugins=` option, which loads regardless
of `setting_sources`. The skills then load by slash command and by
natural-language intent, without leaking the rest of `~/.claude`
(CLAUDE.md, output styles) into the agent. Toggle with
`POCKETPAW_SDK_LOAD_BUNDLED_SKILLS` (default on).

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

1. Iterates `src/pocketpaw/bundled_skills/_bundled/<name>/` directories
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
export POCKETPAW_AUTO_INSTALL_BUNDLED_SKILLS=false
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
cp -r src/pocketpaw/bundled_skills/_bundled/* ~/.claude/skills/
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
   `src/pocketpaw/bundled_skills/_bundled/<your-skill-name>/`
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
| `pocketpaw-edit-pocket` | READ / EDIT / CHAT path-selection + the Type A / B / C edit decision tree for delegating to `pocket_specialist__edit`. Routes simple state edits, structural edits, and open-ended redesigns through the right shape of specialist call. |

Planned (not yet shipped):
- `pocketpaw-audit-pocket` — review an existing pocket for design issues
- `pocketpaw-migrate-dashboard` — convert dashboard-style pockets to the right pattern

## How this fits across chat backends

Bundled skills work for **every** backend because the AgentSkills
format is universal — but the *route* differs by backend, because
the default SDK backend can't see `~/.claude/skills/`:

| Backend | Discovery | Invocation |
| --- | --- | --- |
| `claude_agent_sdk` | `_bundled/` passed as a local plugin via the SDK `plugins=` option (NOT `~/.claude/skills/` — `setting_sources=[]` disables that) | Natural-language intent OR `/<skill>` slash command, handled inside the SDK run |
| `codex_cli`, `openai_agents`, `deep_agents`, `langchain_react` | PocketPaw's `SkillLoader.SKILL_PATHS` (includes `~/.claude/skills/`) | `/<skill>` slash command in chat UI → `dashboard_ws.py` → `SkillExecutor.execute_skill` |

Both routes read the same single-source SKILL.md files under
`_bundled/skills/`. The `~/.claude/skills/` mirror and the local
plugin are two installs of one set of files, so a skill added once is
picked up by every backend.

## Implementation notes

- Module: `src/pocketpaw/bundled_skills/`
- Installer: `src/pocketpaw/bundled_skills/installer.py`
- Bundled content: `src/pocketpaw/bundled_skills/_bundled/<name>/SKILL.md`
- Config flag: `auto_install_bundled_skills: bool = True`
  (`POCKETPAW_AUTO_INSTALL_BUNDLED_SKILLS` env var)
- Wired into: `dashboard_lifecycle.startup_event`
- Tests: `tests/test_bundled_skills_installer.py`
- Runtime consumer: `pocketpaw.skills.SkillLoader` (existing —
  scans `~/.claude/skills/` via `SKILL_PATHS`) and
  `pocketpaw.skills.SkillExecutor` (existing — runs skill body
  through `AgentRouter`)
- Hatchling config: `src/pocketpaw` already includes non-Python files
  recursively, so `SKILL.md` ships in the wheel without extra config.

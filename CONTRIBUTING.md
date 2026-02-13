# Contributing to PocketPaw

Thanks for your interest in contributing to PocketPaw! This guide will help you get started.

## Branch Strategy

> **All pull requests must target the `dev` branch.**
>
> PRs opened against `main` will be closed. The `main` branch is updated only via merge from `dev` when a release is ready.

## Before You Start

- **Search [existing issues](https://github.com/pocketpaw/pocketpaw/issues)** to see if your bug or feature has already been reported.
- **Check [open pull requests](https://github.com/pocketpaw/pocketpaw/pulls)** to make sure someone isn't already working on the same thing.
- If an issue exists, comment on it to let others know you're picking it up.
- If no issue exists, consider opening one first to discuss the approach before writing code.

## Getting Started

1. **Fork** the repository and clone your fork.
2. **Create a feature branch** off `dev`:
   ```bash
   git checkout dev
   git pull origin dev
   git checkout -b feat/your-feature
   ```
3. **Install dependencies**:
   ```bash
   uv sync --dev
   ```

## Development Workflow

```bash
# Run the app (web dashboard)
uv run pocketpaw

# Run tests
uv run pytest --ignore=tests/e2e

# Lint
uv run ruff check .

# Format
uv run ruff format .

# Type check
uv run mypy .
```

## Making Changes

- Keep PRs focused — one feature or fix per PR.
- Follow existing code conventions (async everywhere, protocol-oriented interfaces).
- Add tests for new functionality. Run the full suite before opening a PR.
- Ruff config: line-length 100, target Python 3.11, lint rules E/F/I/UP.

## Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add Spotify playback tool
fix: handle empty WebSocket message
docs: update channel adapter guide
refactor: simplify model router thresholds
test: add coverage for injection scanner
```

## Pull Request Checklist

- [ ] Branch is based on `dev` (not `main`)
- [ ] PR targets the `dev` branch
- [ ] Tests pass (`uv run pytest --ignore=tests/e2e`)
- [ ] Linting passes (`uv run ruff check .`)
- [ ] New config fields are added to `Settings.save()` dict
- [ ] New tools are registered in the appropriate policy group

## Project Structure

```
src/pocketclaw/          # Core package (internal name: pocketclaw)
  agents/                # Agent backends + routing
  bus/adapters/          # Channel adapters (Discord, Slack, etc.)
  tools/builtin/         # Built-in tools
  integrations/          # OAuth, Gmail, Calendar, etc.
  memory/                # Memory stores (file, mem0)
  security/              # Guardian AI, audit, injection scanner
  mcp/                   # MCP server integration
frontend/                # Vanilla JS/CSS/HTML dashboard
tests/                   # pytest test suite
```

## Need Help?

Open an issue or start a discussion. We're happy to help you find the right place to contribute.

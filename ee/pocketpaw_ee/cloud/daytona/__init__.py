"""Daytona integration — VM/container workspaces for cloud projects.

Provides a client wrapper around the Daytona SDK, config module for
reading env-based configuration, pre-built image definitions, a persistent
workspace-to-sandbox mapping store, a context resolver for routing tool
calls to the active sandbox, and Daytona-aware tool wrappers that replace
the OSS local-filesystem tools when a sandbox is provisioned.

Modules:
  client   — wraps the official ``daytona`` SDK (AsyncDaytona / AsyncSandbox)
  config   — reads DAYTONA_API_URL / DAYTONA_API_KEY from environment
  image    — pre-built Docker image definitions (Python, Node, GCC, Docker, UV)
  store    — persistent project_key → sandbox_id mapping (JSON file)
  context  — resolves the active sandbox for the current chat/project context
  tools    — Daytona-aware ReadFile/WriteFile/EditFile/ListDir/Shell/RunPython
  router   — FastAPI endpoints for sandbox lifecycle + sync + terminal

Moved from OSS to EE: 2026-06-24
Updated: 2026-07-01 — added store, context, tools modules for sandbox-aware
    tool routing.
"""

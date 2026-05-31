# Created: 2026-05-17 — pocketpaw#1118 P1. Cloud-side planner entity.
#   Wraps OSS ``pocketpaw.deep_work.planner.PlannerAgent`` so cloud
#   workspaces can plan a Project without ever touching the OSS
#   MissionControlManager (local filesystem) — outputs are materialized
#   into the cloud Project / Task / FileUpload primitives instead.
"""Planner entity — cloud-side wrapper around the OSS deep_work planner."""

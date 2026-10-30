"""Agents domain — package init.

The router is intentionally NOT re-exported here (avoids the same
circular-import pitfall as auth/workspace). Callers needing the router
import directly from ``ee.cloud.agents.router``.
"""

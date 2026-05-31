"""Workspace domain — package init.

The router is intentionally NOT re-exported here. Importing it would
trigger a chain through the service into ``_core.context`` and back,
risking a circular import. Callers needing the router import directly
from ``ee.cloud.workspace.router``; ``ee/cloud/__init__.py`` already
does this.
"""

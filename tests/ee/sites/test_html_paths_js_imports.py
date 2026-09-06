# tests/ee/sites/test_html_paths_js_imports.py
# Created: 2026-09-06 (feat/fx-mcp-server) — ``html_path_is_referenced`` counts ES
# module imports (static and dynamic), so a file reached only through
# ``import ... from`` (an ``_fx/`` effect's vendor dep) is not flagged unreferenced.
"""ES-import references in the html-track reachability scan."""

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.sites.html_paths import html_path_is_referenced  # noqa: E402


def test_static_and_dynamic_imports_reference_files() -> None:
    source = {
        "index.html": '<script type="module" src="/_fx/effects/aurora/index.js"></script>',
        "_fx/effects/aurora/index.js": (
            "import paper from '../../vendor/paper.js';\n"
            "import './style.css';\n"
            'const lazy = () => import("/_fx/vendor/lenis.js");\n'
        ),
        "_fx/vendor/paper.js": "",
        "_fx/effects/aurora/style.css": "",
        "_fx/vendor/lenis.js": "",
        "_fx/vendor/orphan.js": "",
    }
    assert html_path_is_referenced(source, "_fx/vendor/paper.js")
    assert html_path_is_referenced(source, "_fx/effects/aurora/style.css")
    assert html_path_is_referenced(source, "_fx/vendor/lenis.js")
    assert not html_path_is_referenced(source, "_fx/vendor/orphan.js")

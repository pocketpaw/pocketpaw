# tests/cloud/kb/test_kb_binary_contract.py — the kb binary on this machine
# must honour the pre-compiled-article contract.
#
# Created 2026-08-29 after content search returned nothing for a phrase that
# was demonstrably on page 1 of an indexed PDF.
#
# The chain: the kb binary on PATH was built 2026-04-14, predating
# `ingest --article-json`. kb-go parses flags BY HAND and silently IGNORES
# unknown ones, so the old binary accepted the call, stored the payload
# VERBATIM, and returned success. ingest_text_to_scope detects that (the
# receipt lacks `compiled_with`) and raises — correctly and loudly. But the
# upload listener wraps ingest in `except Exception: log; return`, so the
# raise became a log line nobody read, no `kb_article_id` was ever recorded,
# and content search could never resolve a hit back to a file. The feature
# looked switched off rather than broken — this workspace's signature failure.
#
# These tests exercise the REAL binary. They are skipped when it is absent
# (CI images without kb-go) rather than faked, because a mock proves the mock.
from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from pocketpaw_ee.cloud.agents.knowledge import KB_BIN, extract_ingest_article_id

pytestmark = pytest.mark.skipif(
    not shutil.which(KB_BIN) and not (KB_BIN and shutil.os.path.exists(KB_BIN)),
    reason=f"kb binary not present at {KB_BIN!r}",
)

SCOPE = "workspace:__kb_contract_probe__"

ARTICLE = {
    "title": "Fastener Overview",
    "summary": "Widgets and sprockets are industrial fasteners.",
    "content": "# Fastener Overview\n\nWidgets and sprockets are fasteners.",
    "concepts": ["widget", "sprocket"],
    "categories": ["reference"],
    "source": "ProbeFile.pdf",
}


def _ingest(payload: dict) -> dict:
    proc = subprocess.run(
        [KB_BIN, "ingest", "--article-json", "--scope", SCOPE, "--json"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"kb ingest failed: {proc.stderr[:300]}"
    return json.loads(proc.stdout)


def test_the_binary_accepts_article_json_at_all():
    """An old binary ignores the flag and stores the payload verbatim."""
    receipt = _ingest({"raw_text": "Widgets and sprockets are fasteners.", "article": ARTICLE})
    assert isinstance(receipt, dict), "receipt is not JSON — kb was called without --json"


def test_the_receipt_carries_compiled_with():
    """THE detector. Its absence is how a stale binary announces itself, and
    the only reason the failure was catchable at all."""
    receipt = _ingest({"raw_text": "Widgets and sprockets are fasteners.", "article": ARTICLE})
    assert "compiled_with" in receipt, (
        "kb ingest --article-json returned no `compiled_with`: this binary predates "
        "the pre-compiled-article contract and stored the payload VERBATIM. Rebuild "
        "from the workspace kb-go checkout — content search silently indexes nothing "
        "until you do."
    )


def test_the_receipt_carries_a_resolvable_article_id():
    """No id means the listener records no kb_article_id, which means content
    search can never resolve a hit back to a file row."""
    receipt = _ingest({"raw_text": "Widgets and sprockets are fasteners.", "article": ARTICLE})
    assert extract_ingest_article_id(receipt), f"no article id in receipt: {receipt!r}"


def test_two_documents_do_not_collide_on_one_article_id():
    """Guards the failure I mistakenly reported before measuring it: ids come
    from the compiled TITLE, so distinct documents must land distinct ids."""
    a = _ingest(
        {
            "raw_text": "An invoice for Q1 consulting.",
            "article": {**ARTICLE, "title": "Invoice Q1", "source": "Invoice.pdf"},
        }
    )
    b = _ingest(
        {
            "raw_text": "A story about a beach.",
            "article": {**ARTICLE, "title": "Beach Story", "source": "Story.pdf"},
        }
    )
    assert extract_ingest_article_id(a) != extract_ingest_article_id(b), (
        "two documents resolved to the same article id — the second overwrote the first"
    )

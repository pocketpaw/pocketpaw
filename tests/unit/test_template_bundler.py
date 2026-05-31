# tests/unit/test_template_bundler.py
# Created: 2026-05-28 (feat/wave-4a-cli-registry) — RED-first tests for
# the RFC 03 v2 template bundler: content-addressed tarballs with
# optional Ed25519 signatures. Covers:
#   * pack_template:    validates + emits a deterministic <slug>-<ver>.template.tar.gz
#   * unpack_template:  round-trips, verifies hash, verifies signature
#   * compute_template_diff: structured diff with destructive flagging
"""Tests for ``pocketpaw.bundled_templates.bundler``.

The bundler is the library half of the Wave 4a Registry shape: pack a
template into a signed, content-addressed bundle; unpack a bundle back
into an installable on-disk template; diff an installed template
against a new one with per-field destructiveness tagging.

These tests exercise the pure functions directly — the CLI subcommand
wiring is covered by ``test_cli_template.py``.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml

# RED on import until Phase 2 lands the module.
from pocketpaw.bundled_templates.bundler import (
    InstallResult,
    TemplateDiff,
    compute_template_diff,
    pack_template,
    unpack_template,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_v2_dict(name: str = "demo-pocket", version: str = "1.0.0") -> dict[str, Any]:
    """Smallest dict that round-trips through the PocketTemplate model."""
    return {
        "schema_version": "2",
        "name": name,
        "version": version,
        "pattern": "app",
        "vertical": "productivity",
        "display_name": "Demo Pocket",
        "description": "A minimal template used only by bundler tests.",
        "shape": "data-grid",
        "icon": "list",
        "color": "#7c9c63",
        "state": {
            "entity_type": "Task",
            "columns": [
                {"field": "title", "widget": "text"},
                {"field": "status", "widget": "badge"},
            ],
        },
        "actions": [],
        "connectors": [],
        "skill_refs": [],
    }


def _template_with_action(
    *, action_name: str, instinct_policy: str = "auto", outcomes: list[str] | None = None
) -> dict[str, Any]:
    """A v2 template with one action — used by diff tests."""
    base = _minimal_v2_dict(name="diff-pocket")
    base["actions"] = [
        {
            "name": action_name,
            "label": "Do thing",
            "kind": "single-row",
            "instinct_policy": instinct_policy,
            "outcomes_emitted": outcomes or [],
        }
    ]
    if outcomes:
        # Top-level outcomes is list[str] per PocketTemplate.outcomes.
        base["outcomes"] = list(outcomes)
    return base


def _write_template_dir(tmp_path: Path, data: dict[str, Any], slug: str = "demo-pocket") -> Path:
    """Materialize a source-form template directory on disk."""
    source = tmp_path / slug
    source.mkdir(parents=True, exist_ok=True)
    (source / "template.pocket.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )
    return source


def _read_manifest(bundle_path: Path) -> dict[str, Any]:
    """Pull manifest.json out of a packed tarball for assertion."""
    with tarfile.open(bundle_path, "r:gz") as tar:
        member = tar.getmember("manifest.json")
        f = tar.extractfile(member)
        assert f is not None
        return json.loads(f.read().decode("utf-8"))


# ===========================================================================
# pack_template
# ===========================================================================


class TestPackTemplate:
    def test_pack_yaml_file_writes_tarball_with_canonical_name(self, tmp_path: Path) -> None:
        source = _write_template_dir(tmp_path, _minimal_v2_dict())
        out = pack_template(source, output_path=tmp_path)
        assert out.exists()
        assert out.name == "demo-pocket-1.0.0.template.tar.gz"

    def test_pack_with_yaml_file_input_works(self, tmp_path: Path) -> None:
        """Author can publish either a directory OR the YAML file directly."""
        source = _write_template_dir(tmp_path, _minimal_v2_dict())
        yaml_file = source / "template.pocket.yaml"
        out = pack_template(yaml_file, output_path=tmp_path)
        assert out.exists()
        assert out.name == "demo-pocket-1.0.0.template.tar.gz"

    def test_pack_emits_manifest_with_listing_fields(self, tmp_path: Path) -> None:
        """Manifest carries the listing fields enumerated in RFC §Distributed."""
        source = _write_template_dir(tmp_path, _minimal_v2_dict())
        bundle = pack_template(source, output_path=tmp_path)
        manifest = _read_manifest(bundle)
        for key in (
            "name",
            "version",
            "display_name",
            "description",
            "vertical",
            "icon",
            "color",
            "screenshots",
            "bundle_hash",
            "published_at",
        ):
            assert key in manifest, f"manifest missing field {key!r}"
        assert manifest["name"] == "demo-pocket"
        assert manifest["version"] == "1.0.0"
        assert manifest["bundle_hash"].startswith("sha256:")

    def test_pack_is_deterministic(self, tmp_path: Path) -> None:
        """Same input -> identical content hash."""
        a_dir = _write_template_dir(tmp_path / "a", _minimal_v2_dict())
        b_dir = _write_template_dir(tmp_path / "b", _minimal_v2_dict())
        bundle_a = pack_template(a_dir, output_path=tmp_path / "out_a")
        bundle_b = pack_template(b_dir, output_path=tmp_path / "out_b")
        manifest_a = _read_manifest(bundle_a)
        manifest_b = _read_manifest(bundle_b)
        assert manifest_a["bundle_hash"] == manifest_b["bundle_hash"]

    def test_pack_refuses_invalid_template(self, tmp_path: Path) -> None:
        """Pydantic validation gates publishing — bad YAML must fail loudly."""
        bad = _minimal_v2_dict()
        del bad["version"]
        source = _write_template_dir(tmp_path, bad)
        with pytest.raises(Exception) as exc:
            pack_template(source, output_path=tmp_path)
        msg = str(exc.value).lower()
        assert "version" in msg or "valid" in msg

    def test_pack_bundles_extra_files(self, tmp_path: Path) -> None:
        """Optional README, screenshots/, skills/, tests/ are packed in."""
        source = _write_template_dir(tmp_path, _minimal_v2_dict())
        (source / "README.md").write_text("# demo", encoding="utf-8")
        (source / "screenshots").mkdir()
        (source / "screenshots" / "grid.png").write_bytes(b"PNG-fake")
        (source / "skills").mkdir()
        (source / "skills" / "helper").mkdir()
        (source / "skills" / "helper" / "SKILL.md").write_text("# helper", encoding="utf-8")
        (source / "tests").mkdir()
        (source / "tests" / "sample_inputs.json").write_text("[]", encoding="utf-8")

        bundle = pack_template(source, output_path=tmp_path)
        with tarfile.open(bundle, "r:gz") as tar:
            names = set(tar.getnames())
        assert "README.md" in names
        assert "screenshots/grid.png" in names
        assert "skills/helper/SKILL.md" in names
        assert "tests/sample_inputs.json" in names

    def test_pack_unsigned_by_default(self, tmp_path: Path) -> None:
        """No signing key supplied -> no signature in manifest."""
        source = _write_template_dir(tmp_path, _minimal_v2_dict())
        bundle = pack_template(source, output_path=tmp_path)
        manifest = _read_manifest(bundle)
        assert manifest.get("signature") in (None, "")

    def test_pack_with_signing_key_emits_signature(self, tmp_path: Path) -> None:
        """Supplying an Ed25519 private key produces a signature + pubkey."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        sk = Ed25519PrivateKey.generate()
        seed = sk.private_bytes_raw()

        source = _write_template_dir(tmp_path, _minimal_v2_dict())
        bundle = pack_template(source, output_path=tmp_path, signing_key=seed)
        manifest = _read_manifest(bundle)
        assert manifest.get("signature"), "signature must be present"
        assert manifest.get("signing_public_key"), "public key must be present"


# ===========================================================================
# unpack_template
# ===========================================================================


class TestUnpackTemplate:
    def test_round_trip_pack_then_unpack(self, tmp_path: Path) -> None:
        source = _write_template_dir(tmp_path, _minimal_v2_dict())
        bundle = pack_template(source, output_path=tmp_path)
        dest_root = tmp_path / "installed"
        result = unpack_template(bundle, dest_root)
        assert isinstance(result, InstallResult)
        assert result.slug == "demo-pocket"
        assert result.version == "1.0.0"
        assert result.destination.exists()
        assert (result.destination / "template.pocket.yaml").exists()
        assert (result.destination / "manifest.json").exists()
        assert result.hash_verified is True
        # unsigned bundle -> signature_verified is None (skipped)
        assert result.signature_verified is None

    def test_unpack_with_extra_files_preserves_structure(self, tmp_path: Path) -> None:
        source = _write_template_dir(tmp_path, _minimal_v2_dict())
        (source / "README.md").write_text("# demo", encoding="utf-8")
        (source / "screenshots").mkdir()
        (source / "screenshots" / "grid.png").write_bytes(b"PNG-fake")
        bundle = pack_template(source, output_path=tmp_path)
        dest_root = tmp_path / "installed"
        result = unpack_template(bundle, dest_root)
        assert (result.destination / "README.md").exists()
        assert (result.destination / "screenshots" / "grid.png").exists()

    def test_unpack_rejects_tampered_bundle(self, tmp_path: Path) -> None:
        """If we tamper with a packed file, the recomputed content hash
        no longer matches manifest.bundle_hash and unpack must refuse."""
        source = _write_template_dir(tmp_path, _minimal_v2_dict())
        bundle = pack_template(source, output_path=tmp_path)

        # Repack the tarball with one file mutated. Easiest way: explode,
        # mutate, recompress.
        tampered_dir = tmp_path / "tampered"
        tampered_dir.mkdir()
        with tarfile.open(bundle, "r:gz") as tar:
            tar.extractall(tampered_dir, filter="data")
        (tampered_dir / "template.pocket.yaml").write_text(
            "schema_version: '2'\nname: tampered\n", encoding="utf-8"
        )
        tampered_bundle = tmp_path / "tampered.tar.gz"
        with tarfile.open(tampered_bundle, "w:gz") as tar:
            for path in sorted(tampered_dir.rglob("*")):
                if path.is_file():
                    tar.add(path, arcname=str(path.relative_to(tampered_dir)))

        with pytest.raises(Exception) as exc:
            unpack_template(tampered_bundle, tmp_path / "installed")
        assert "hash" in str(exc.value).lower() or "mismatch" in str(exc.value).lower()

    def test_unpack_signed_bundle_verifies_with_matching_pubkey(self, tmp_path: Path) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        sk = Ed25519PrivateKey.generate()
        seed = sk.private_bytes_raw()
        pubkey_bytes = sk.public_key().public_bytes_raw()

        source = _write_template_dir(tmp_path, _minimal_v2_dict())
        bundle = pack_template(source, output_path=tmp_path, signing_key=seed)
        result = unpack_template(bundle, tmp_path / "installed", verify_key=pubkey_bytes)
        assert result.signature_verified is True

    def test_unpack_signed_bundle_fails_with_wrong_pubkey(self, tmp_path: Path) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        sk = Ed25519PrivateKey.generate()
        seed = sk.private_bytes_raw()
        other_pub = Ed25519PrivateKey.generate().public_key().public_bytes_raw()

        source = _write_template_dir(tmp_path, _minimal_v2_dict())
        bundle = pack_template(source, output_path=tmp_path, signing_key=seed)
        with pytest.raises(Exception) as exc:
            unpack_template(bundle, tmp_path / "installed", verify_key=other_pub)
        assert "signature" in str(exc.value).lower()

    def test_unsigned_bundle_with_verify_key_passes_with_warning(self, tmp_path: Path) -> None:
        """No signature in manifest + a verify key supplied -> hash still
        verifies, signature_verified is False (not None) to indicate the
        key was provided but no signature existed."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        pubkey = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
        source = _write_template_dir(tmp_path, _minimal_v2_dict())
        bundle = pack_template(source, output_path=tmp_path)
        # No signature on the bundle but a verify key supplied: we still
        # treat hash as authoritative; signature_verified is False.
        result = unpack_template(bundle, tmp_path / "installed", verify_key=pubkey)
        assert result.hash_verified is True
        assert result.signature_verified is False


# ===========================================================================
# compute_template_diff
# ===========================================================================


class TestComputeTemplateDiff:
    def test_identical_templates_produce_empty_diff(self) -> None:
        a = _minimal_v2_dict()
        b = _minimal_v2_dict()
        diff = compute_template_diff(a, b)
        assert isinstance(diff, TemplateDiff)
        assert diff.added_fields == []
        assert diff.removed_fields == []
        assert diff.changed_fields == []
        assert diff.actions_added == []
        assert diff.actions_removed == []
        assert diff.is_destructive is False

    def test_added_action_is_non_destructive(self) -> None:
        a = _minimal_v2_dict()
        b = _template_with_action(action_name="add_task")
        diff = compute_template_diff(a, b)
        assert "add_task" in diff.actions_added
        assert diff.is_destructive is False

    def test_removed_action_is_destructive(self) -> None:
        a = _template_with_action(action_name="add_task")
        b = _minimal_v2_dict()
        # Strip the actions to make slug consistent
        a["name"] = "diff-pocket"
        diff = compute_template_diff(a, b)
        assert "add_task" in diff.actions_removed
        assert diff.is_destructive is True

    def test_changed_instinct_policy_is_destructive(self) -> None:
        a = _template_with_action(action_name="op", instinct_policy="auto")
        b = _template_with_action(action_name="op", instinct_policy="require_approval")
        diff = compute_template_diff(a, b)
        assert any(
            "op" in c["path"] and "instinct" in c["path"].lower() for c in diff.actions_changed
        )
        assert diff.is_destructive is True

    def test_added_outcome_is_non_destructive(self) -> None:
        a = _template_with_action(action_name="op", outcomes=["done"])
        b = _template_with_action(action_name="op", outcomes=["done", "skipped"])
        diff = compute_template_diff(a, b)
        # "skipped" should appear in outcomes_added
        assert "skipped" in diff.outcomes_added
        assert diff.is_destructive is False

    def test_removed_outcome_is_destructive(self) -> None:
        a = _template_with_action(action_name="op", outcomes=["done", "skipped"])
        b = _template_with_action(action_name="op", outcomes=["done"])
        diff = compute_template_diff(a, b)
        assert "skipped" in diff.outcomes_removed
        assert diff.is_destructive is True

    def test_changed_top_level_field_classed_as_non_destructive_by_default(self) -> None:
        a = _minimal_v2_dict()
        b = _minimal_v2_dict()
        b["description"] = "totally different copy"
        b["display_name"] = "Demo Pocket v2"
        diff = compute_template_diff(a, b)
        assert any(c["path"] == "description" for c in diff.changed_fields)
        assert any(c["path"] == "display_name" for c in diff.changed_fields)
        assert diff.is_destructive is False

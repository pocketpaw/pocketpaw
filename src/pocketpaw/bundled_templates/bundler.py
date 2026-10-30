# src/pocketpaw/bundled_templates/bundler.py
# Created: 2026-05-28 (feat/wave-4a-cli-registry) — RFC 03 v2 Registry
# shape, library half. Implements:
#   * pack_template(source_dir | yaml_file)        -> Path to a signed,
#                                                     content-addressed
#                                                     <slug>-<ver>.template.tar.gz
#   * unpack_template(bundle_path, destination)    -> InstallResult with
#                                                     hash + (optional)
#                                                     signature verification
#   * compute_template_diff(installed, new)        -> TemplateDiff with
#                                                     destructive flagging
#
# Bundle layout:
#   template.pocket.yaml   (the YAML metadata)
#   manifest.json          (listing fields + bundle_hash + optional sig)
#   README.md              (optional)
#   screenshots/           (optional pass-through dir)
#   skills/                (optional pass-through dir — bundled Skills)
#   tests/                 (optional pass-through dir — sample_inputs etc)
#
# Hash strategy: SHA-256 over a deterministic walk of the bundle's
# contents (excluding manifest.json itself — chicken-and-egg).
#
# Signing: Ed25519 via the `cryptography` package (already a direct
# dep of pocketpaw). Signature is over the raw bundle_hash bytes.
# The public key is recorded in the manifest as hex for self-contained
# verification.
"""Author-facing template bundler — pack / unpack / diff.

Pure library code. The CLI wiring in
:mod:`pocketpaw.cli.template` dispatches to this module's functions for
the new ``publish``, ``install`` and ``upgrade`` subactions added in
Wave 4a.

Wave 4a explicitly ships **no Registry transport** — there is no
network client here, no remote push, no auth flow. ``pack_template``
writes a tarball to local disk; ``unpack_template`` reads one back.
The Registry server lives in a later wave. The bundle itself is
self-contained: hash and signature are inside ``manifest.json``, so a
bundle copied around by any means (USB drive, S3, an email
attachment) can be verified offline.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Result + diff dataclasses
# ---------------------------------------------------------------------------

# Top-level YAML keys treated as the "listing fields" — these are the
# columns the Registry surfaces alongside the install button. Keep in
# sync with RFC 03 §"Registry shape — file vs distributed vs installed",
# subsection "Distributed".
_LISTING_FIELDS: tuple[str, ...] = (
    "name",
    "version",
    "display_name",
    "description",
    "vertical",
    "icon",
    "color",
    "screenshots",
)

# Manifest file name inside the tarball. The actual content hash is
# computed across every other file, so this entry is part of the bundle
# but not part of the hash input.
_MANIFEST_NAME = "manifest.json"
_TEMPLATE_YAML_NAME = "template.pocket.yaml"


@dataclass(frozen=True)
class InstallResult:
    """Outcome of an ``unpack_template`` call.

    ``hash_verified`` is always True on a successful unpack (mismatches
    raise). ``signature_verified`` is:
      - ``True``   — signature present, verify_key supplied, signature OK
      - ``False``  — verify_key supplied but bundle is unsigned (or sig
                     was empty); the unpack still succeeded on hash alone
      - ``None``   — no verify_key supplied; we did not attempt to verify
    """

    slug: str
    version: str
    destination: Path
    hash_verified: bool
    signature_verified: bool | None


@dataclass
class TemplateDiff:
    """Structured diff between two template dicts.

    The diff carries top-level field-level entries plus three first-class
    sub-section diffs (actions, triggers, instinct rules) so the upgrade
    flow can quickly classify each entry as destructive or not without
    re-walking the trees.

    Per-entry ``destructive: bool`` is set on the entries inside
    ``actions_changed`` / ``triggers_changed`` / ``instinct_rules_changed``
    (each entry is ``{"path": str, "old": Any, "new": Any, "destructive": bool}``).
    The top-level ``is_destructive`` is True iff any destructive change
    was found anywhere.
    """

    added_fields: list[dict[str, Any]] = field(default_factory=list)
    removed_fields: list[dict[str, Any]] = field(default_factory=list)
    changed_fields: list[dict[str, Any]] = field(default_factory=list)

    actions_added: list[str] = field(default_factory=list)
    actions_removed: list[str] = field(default_factory=list)
    actions_changed: list[dict[str, Any]] = field(default_factory=list)

    triggers_added: list[str] = field(default_factory=list)
    triggers_removed: list[str] = field(default_factory=list)
    triggers_changed: list[dict[str, Any]] = field(default_factory=list)

    instinct_rules_added: list[str] = field(default_factory=list)
    instinct_rules_removed: list[str] = field(default_factory=list)
    instinct_rules_changed: list[dict[str, Any]] = field(default_factory=list)

    outcomes_added: list[str] = field(default_factory=list)
    outcomes_removed: list[str] = field(default_factory=list)

    @property
    def is_destructive(self) -> bool:
        """True if any destructive change is present anywhere in the diff."""
        if self.actions_removed or self.triggers_removed or self.instinct_rules_removed:
            return True
        if self.outcomes_removed:
            return True
        for changed in (
            self.actions_changed,
            self.triggers_changed,
            self.instinct_rules_changed,
        ):
            if any(e.get("destructive") for e in changed):
                return True
        return False


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BundleError(Exception):
    """Raised when bundle pack / unpack fails for any reason.

    Sub-cases (signature failure, hash mismatch, validation failure)
    share the same exception type so callers can match on the message
    text or ``isinstance`` and keep the surface small.
    """


# ---------------------------------------------------------------------------
# pack_template
# ---------------------------------------------------------------------------


def pack_template(
    source: Path,
    output_path: Path | None = None,
    *,
    signing_key: bytes | None = None,
) -> Path:
    """Bundle a template directory (or YAML file) into a signed tarball.

    Args:
        source: Either a path to ``template.pocket.yaml`` directly OR a
            directory containing it (plus optional ``README.md``,
            ``screenshots/``, ``skills/``, ``tests/`` siblings).
        output_path: Directory the bundle should be written into. The
            canonical name ``<slug>-<version>.template.tar.gz`` is
            appended. If omitted, defaults to ``./``.
        signing_key: 32-byte Ed25519 seed (raw private key). When
            supplied, the manifest carries a signature over the
            content hash and the matching public key. When omitted,
            the bundle is unsigned (consumers see a WARNING during
            install but the hash is still verifiable).

    Returns:
        The path to the written tarball.

    Raises:
        BundleError: if validation, hashing, or IO fails.
    """

    source = Path(source)
    if not source.exists():
        raise BundleError(f"source not found: {source}")

    # Resolve the YAML location + the staging "tree" that goes into the
    # bundle. If the user pointed at a YAML file directly, the tree is
    # just that one file; otherwise we walk the directory.
    if source.is_file():
        yaml_file = source
        tree_root = source.parent
        tree_files = [yaml_file]
    elif source.is_dir():
        yaml_file = source / _TEMPLATE_YAML_NAME
        if not yaml_file.is_file():
            raise BundleError(f"directory {source} is missing {_TEMPLATE_YAML_NAME}")
        tree_root = source
        tree_files = [p for p in source.rglob("*") if p.is_file()]
    else:
        raise BundleError(f"source is not a file or directory: {source}")

    # --- 1. Validate the YAML through the Pydantic chokepoint ---
    template_dict = _load_yaml(yaml_file)
    _validate_template_strict(template_dict, yaml_file)

    slug = str(template_dict["name"])
    version = str(template_dict["version"])

    # --- 2. Stage the bundle contents in a tmp dir, then compute hash ---
    if output_path is None:
        output_path = Path(".")
    output_path = Path(output_path)
    output_path.mkdir(parents=True, exist_ok=True)
    bundle_path = output_path / f"{slug}-{version}.template.tar.gz"

    with tempfile.TemporaryDirectory(prefix="pocketpaw-pack-") as _stage:
        stage = Path(_stage)
        # Copy all source files into the stage with their relative paths.
        # We always write template.pocket.yaml at the bundle root, even
        # if the caller passed a YAML file from a deeper path — the
        # bundle layout is normalized.
        _stage_files(yaml_file, tree_root, tree_files, stage)

        # The manifest covers every file we just staged. Hash *before*
        # writing manifest.json so the hash input is stable.
        content_hash = _hash_tree(stage)

        manifest = _build_manifest(
            template_dict=template_dict,
            content_hash=content_hash,
            signing_key=signing_key,
        )
        (stage / _MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        # --- 3. Tar it up ---
        _write_tarball(stage, bundle_path)

    return bundle_path


# ---------------------------------------------------------------------------
# unpack_template
# ---------------------------------------------------------------------------


def unpack_template(
    bundle_path: Path,
    destination: Path,
    *,
    verify_key: bytes | None = None,
) -> InstallResult:
    """Unpack + verify a template bundle into ``destination/<slug>/``.

    Hash verification is always performed; signature verification is
    attempted only when ``verify_key`` is supplied AND the manifest
    actually carries a signature.

    Args:
        bundle_path: Path to a ``.template.tar.gz`` produced by
            ``pack_template``.
        destination: Directory under which the per-slug install dir
            will be created. Will be created if it doesn't exist.
        verify_key: 32-byte Ed25519 public key. If the bundle is
            signed AND this key matches the signing key, signature
            verification passes. If supplied but the bundle is
            unsigned, ``InstallResult.signature_verified`` is False
            (the bundle still installs — hash is authoritative).

    Returns:
        InstallResult describing the install + verification status.

    Raises:
        BundleError: on missing bundle, malformed tar, missing
            manifest, hash mismatch, or — when ``verify_key`` is given
            AND the bundle IS signed — bad signature.
    """

    bundle_path = Path(bundle_path)
    if not bundle_path.is_file():
        raise BundleError(f"bundle not found: {bundle_path}")

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="pocketpaw-unpack-") as _stage:
        stage = Path(_stage)
        try:
            with tarfile.open(bundle_path, "r:gz") as tar:
                _safe_extract(tar, stage)
        except (tarfile.TarError, OSError) as exc:
            raise BundleError(f"failed to read bundle {bundle_path}: {exc}") from exc

        manifest_path = stage / _MANIFEST_NAME
        if not manifest_path.is_file():
            raise BundleError(f"bundle {bundle_path} missing {_MANIFEST_NAME}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BundleError(f"bundle {bundle_path} has malformed manifest: {exc}") from exc

        # --- Hash check ---
        # The manifest is excluded from the hash input by _hash_tree.
        recomputed = _hash_tree(stage)
        declared = str(manifest.get("bundle_hash", ""))
        if not declared:
            raise BundleError("manifest is missing bundle_hash")
        if recomputed != declared:
            raise BundleError(
                f"bundle hash mismatch: manifest says {declared}, recomputed {recomputed}"
            )

        # --- Signature check (optional) ---
        signature_verified: bool | None
        sig_hex = str(manifest.get("signature", "") or "")
        if verify_key is None:
            signature_verified = None
        elif not sig_hex:
            # verify_key supplied but bundle is unsigned; treat as
            # "checked but no signature available". Caller surfaces a
            # WARNING in the CLI.
            signature_verified = False
        else:
            try:
                _verify_signature(verify_key, sig_hex, declared)
            except BundleError:
                raise
            except Exception as exc:  # noqa: BLE001 — defensive
                raise BundleError(f"signature verification failed: {exc}") from exc
            signature_verified = True

        # --- Materialize into destination/<slug>/ ---
        slug = str(manifest.get("name", ""))
        version = str(manifest.get("version", ""))
        if not slug:
            raise BundleError("manifest is missing 'name' (slug)")
        dest_dir = destination / slug
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Mirror every staged file into the destination. We
        # intentionally re-copy manifest.json so the installed dir
        # retains the trust metadata.
        for staged in stage.rglob("*"):
            if not staged.is_file():
                continue
            relative = staged.relative_to(stage)
            target = dest_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(staged.read_bytes())

    return InstallResult(
        slug=slug,
        version=version,
        destination=dest_dir,
        hash_verified=True,
        signature_verified=signature_verified,
    )


# ---------------------------------------------------------------------------
# compute_template_diff
# ---------------------------------------------------------------------------


def compute_template_diff(installed: dict[str, Any], new: dict[str, Any]) -> TemplateDiff:
    """Structured diff between two template dicts.

    The diff is shallow at the top level for non-list fields and
    list-keyed (by ``name`` / ``id``) for actions, triggers, and
    instinct rules. Destructiveness lives at the structural level
    (removed action, removed outcome, changed instinct policy) — pure
    field rewrites (description, display_name, version) are tagged
    non-destructive.

    Args:
        installed: The current on-disk template dict.
        new: The proposed template dict.

    Returns:
        A populated TemplateDiff. Empty diff for identical inputs.
    """

    diff = TemplateDiff()

    _diff_top_level_scalars(installed, new, diff)
    _diff_actions(installed, new, diff)
    _diff_triggers(installed, new, diff)
    _diff_instinct_rules(installed, new, diff)
    _diff_outcomes(installed, new, diff)

    return diff


# ---------------------------------------------------------------------------
# Implementation helpers — pack
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML file into a dict. Raises BundleError on failure."""
    import yaml  # noqa: PLC0415 — lazy

    try:
        raw = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError) as exc:
        raise BundleError(f"failed to read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BundleError(f"{path}: expected a mapping at top level")
    return data


def _validate_template_strict(data: dict[str, Any], path: Path) -> None:
    """Refuse to publish a template that fails Pydantic validation.

    Auto-promotes v1 -> v2 BEFORE validation so a v1 template on disk
    can still be published. The bundle always stores the post-promotion
    YAML so consumers don't pay the translation cost again.
    """

    from pocketpaw.bundled_templates.loader import _promote_v1_to_v2  # noqa: PLC0415

    merged = _promote_v1_to_v2(data) if _is_v1(data) else data

    from pydantic import ValidationError  # noqa: PLC0415

    from pocketpaw.bundled_templates.schema import PocketTemplate  # noqa: PLC0415

    try:
        PocketTemplate.model_validate(merged)
    except ValidationError as exc:
        raise BundleError(f"{path} failed template validation: {exc.errors()}") from exc

    # Mutate the caller's dict in-place to the v2 shape so the staged
    # YAML is the post-promotion form.
    if _is_v1(data):
        data.clear()
        data.update(merged)


def _is_v1(meta: dict[str, Any]) -> bool:
    sv = meta.get("schema_version")
    return sv is None or str(sv) == "1"


def _stage_files(
    yaml_file: Path,
    tree_root: Path,
    tree_files: list[Path],
    stage: Path,
) -> None:
    """Copy source files into the stage with their normalized paths.

    ``template.pocket.yaml`` always lands at the bundle root, regardless
    of where it sat in the source tree.
    """

    yaml_file = yaml_file.resolve()
    # Always write the YAML at the bundle root.
    (stage / _TEMPLATE_YAML_NAME).write_bytes(yaml_file.read_bytes())

    for src in tree_files:
        src_resolved = src.resolve()
        if src_resolved == yaml_file:
            continue
        try:
            relative = src.relative_to(tree_root)
        except ValueError:
            # File outside the staging root (e.g. yaml file passed
            # standalone) — skip silently.
            continue
        # Skip the bundler's own manifest if a stale one exists in the
        # source tree (e.g. unpacking, editing, re-packing).
        if str(relative) == _MANIFEST_NAME:
            continue
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src.read_bytes())


def _hash_tree(root: Path) -> str:
    """SHA-256 over a deterministic walk of every file under ``root``.

    The manifest itself is excluded (chicken-and-egg). The hash input
    is a stream of ``<relative_path>\\0<file_sha256>\\0`` chunks ordered
    by relative path.
    """

    h = hashlib.sha256()
    files = sorted(
        (p for p in root.rglob("*") if p.is_file() and p.name != _MANIFEST_NAME),
        key=lambda p: p.relative_to(root).as_posix(),
    )
    for path in files:
        rel = path.relative_to(root).as_posix().encode("utf-8")
        file_h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda f=f: f.read(65536), b""):
                file_h.update(chunk)
        h.update(rel)
        h.update(b"\x00")
        h.update(file_h.hexdigest().encode("ascii"))
        h.update(b"\x00")
    return f"sha256:{h.hexdigest()}"


def _build_manifest(
    *,
    template_dict: dict[str, Any],
    content_hash: str,
    signing_key: bytes | None,
) -> dict[str, Any]:
    """Assemble the manifest.json payload from listing fields + hash."""

    manifest: dict[str, Any] = {}
    for key in _LISTING_FIELDS:
        if key in template_dict:
            manifest[key] = template_dict[key]
        elif key == "screenshots":
            manifest[key] = []
        else:
            manifest[key] = None

    manifest["bundle_hash"] = content_hash
    manifest["published_at"] = datetime.now(tz=UTC).isoformat()
    manifest["signature"] = None
    manifest["signing_public_key"] = None

    if signing_key is not None:
        sig_hex, pub_hex = _sign_hash(signing_key, content_hash)
        manifest["signature"] = sig_hex
        manifest["signing_public_key"] = pub_hex

    return manifest


def _sign_hash(seed: bytes, content_hash: str) -> tuple[str, str]:
    """Sign the content hash with Ed25519. Returns (sig_hex, pub_hex).

    Accepts either a raw 32-byte seed OR a 64-byte hex-encoded seed
    (some operators store keys in hex on disk — be lenient).
    """

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: PLC0415
        Ed25519PrivateKey,
    )

    seed_bytes = _normalize_key_bytes(seed, expected_len=32, kind="signing key")
    sk = Ed25519PrivateKey.from_private_bytes(seed_bytes)
    signature = sk.sign(content_hash.encode("utf-8"))
    pub = sk.public_key().public_bytes_raw()
    return signature.hex(), pub.hex()


def _verify_signature(verify_key: bytes, sig_hex: str, content_hash: str) -> None:
    """Raise BundleError if the signature doesn't match the content hash."""

    from cryptography.exceptions import InvalidSignature  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: PLC0415
        Ed25519PublicKey,
    )

    key_bytes = _normalize_key_bytes(verify_key, expected_len=32, kind="verify key")
    try:
        signature = bytes.fromhex(sig_hex)
    except ValueError as exc:
        raise BundleError(f"signature is not valid hex: {exc}") from exc

    pk = Ed25519PublicKey.from_public_bytes(key_bytes)
    try:
        pk.verify(signature, content_hash.encode("utf-8"))
    except InvalidSignature as exc:
        raise BundleError("signature verification failed: bad signature") from exc


def _normalize_key_bytes(key: bytes, *, expected_len: int, kind: str) -> bytes:
    """Accept either raw 32-byte keys or hex-encoded 64-char keys."""
    if len(key) == expected_len:
        return key
    # Try hex
    try:
        decoded = bytes.fromhex(key.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise BundleError(
            f"{kind} must be {expected_len} raw bytes or {expected_len * 2} hex chars; "
            f"got {len(key)} bytes"
        ) from exc
    if len(decoded) != expected_len:
        raise BundleError(f"{kind} hex must decode to {expected_len} bytes; got {len(decoded)}")
    return decoded


def _write_tarball(stage: Path, bundle_path: Path) -> None:
    """Write the staged tree to ``bundle_path`` deterministically.

    Sorted file order so the *tarball* itself is closer to deterministic
    (gzip headers carry a timestamp by default which we can't easily
    suppress without dropping to GzipFile — the content hash is the
    authoritative identifier, so this is good enough).
    """

    files = sorted(
        (p for p in stage.rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(stage).as_posix(),
    )
    with tarfile.open(bundle_path, "w:gz") as tar:
        for path in files:
            tar.add(path, arcname=path.relative_to(stage).as_posix())


def _safe_extract(tar: tarfile.TarFile, destination: Path) -> None:
    """Extract a tarball, rejecting path-traversal entries.

    Python 3.12+ ships ``tarfile.data_filter`` which handles this for
    us, but we keep an explicit check so the rejection error speaks our
    vocabulary (BundleError, not python's generic OutsideDestinationError).
    """

    destination = destination.resolve()
    for member in tar.getmembers():
        # Reject absolute paths and parent-relative escapes.
        target = (destination / member.name).resolve()
        try:
            target.relative_to(destination)
        except ValueError as exc:
            raise BundleError(
                f"refusing to extract {member.name!r} — path escapes destination"
            ) from exc
        if member.issym() or member.islnk():
            raise BundleError(
                f"refusing to extract symlink {member.name!r} — not allowed in template bundles"
            )
    # filter="data" is the Python 3.12+ safe-extraction filter; we've
    # already vetted the members for path-escape + symlink, but the
    # filter is an extra defense and silences the 3.14 deprecation.
    tar.extractall(destination, filter="data")


# ---------------------------------------------------------------------------
# Implementation helpers — diff
# ---------------------------------------------------------------------------


# Top-level fields whose changes are always classified as non-destructive.
# Everything else (state shape, joined_entities) is reported as a changed
# field but counts as non-destructive at the top level — destructiveness
# is concentrated in the action / trigger / instinct sub-sections, which
# bind to actual runtime behavior.
_NON_DESTRUCTIVE_TOP_LEVEL_FIELDS: frozenset[str] = frozenset(
    {
        "schema_version",
        "name",
        "version",
        "display_name",
        "description",
        "vertical",
        "pattern",
        "shape",
        "icon",
        "color",
        "screenshots",
    }
)


def _diff_top_level_scalars(a: dict[str, Any], b: dict[str, Any], diff: TemplateDiff) -> None:
    """Diff the top-level scalar / dict fields (excluding the lists we
    diff structurally below)."""

    structural_keys = {"actions", "triggers", "instinct_rules", "outcomes"}
    keys = (set(a) | set(b)) - structural_keys
    for key in sorted(keys):
        if key not in a:
            diff.added_fields.append({"path": key, "new": b[key]})
        elif key not in b:
            diff.removed_fields.append({"path": key, "old": a[key]})
        elif a[key] != b[key]:
            diff.changed_fields.append({"path": key, "old": a[key], "new": b[key]})


def _diff_actions(a: dict[str, Any], b: dict[str, Any], diff: TemplateDiff) -> None:
    """Diff the actions list, keyed by ``name``."""

    a_by_name = _by_name(a.get("actions", []))
    b_by_name = _by_name(b.get("actions", []))

    for name in sorted(set(b_by_name) - set(a_by_name)):
        diff.actions_added.append(name)
    for name in sorted(set(a_by_name) - set(b_by_name)):
        diff.actions_removed.append(name)

    for name in sorted(set(a_by_name) & set(b_by_name)):
        old = a_by_name[name]
        new = b_by_name[name]
        if old == new:
            continue
        # Walk shallow keys to surface per-field changes
        for field_name in sorted(set(old) | set(new)):
            old_val = old.get(field_name)
            new_val = new.get(field_name)
            if old_val == new_val:
                continue
            destructive = field_name == "instinct_policy"
            diff.actions_changed.append(
                {
                    "path": f"actions[{name}].{field_name}",
                    "old": old_val,
                    "new": new_val,
                    "destructive": destructive,
                }
            )


def _diff_triggers(a: dict[str, Any], b: dict[str, Any], diff: TemplateDiff) -> None:
    """Diff the triggers list, keyed by ``name``."""

    a_by_name = _by_name(a.get("triggers", []))
    b_by_name = _by_name(b.get("triggers", []))

    for name in sorted(set(b_by_name) - set(a_by_name)):
        diff.triggers_added.append(name)
    for name in sorted(set(a_by_name) - set(b_by_name)):
        diff.triggers_removed.append(name)

    for name in sorted(set(a_by_name) & set(b_by_name)):
        old = a_by_name[name]
        new = b_by_name[name]
        if old == new:
            continue
        for field_name in sorted(set(old) | set(new)):
            old_val = old.get(field_name)
            new_val = new.get(field_name)
            if old_val == new_val:
                continue
            # Changes to a trigger's `type` / `cron` / `when` are
            # behavioral — flag as destructive so the upgrade flow
            # prompts. Field-rename or label tweaks aren't.
            destructive = field_name in {"type", "cron", "when"}
            diff.triggers_changed.append(
                {
                    "path": f"triggers[{name}].{field_name}",
                    "old": old_val,
                    "new": new_val,
                    "destructive": destructive,
                }
            )


def _diff_instinct_rules(a: dict[str, Any], b: dict[str, Any], diff: TemplateDiff) -> None:
    """Diff the instinct_rules.rules list, keyed by ``id`` (or ``name``).

    Any change to ``policy`` is destructive; structure changes inside
    a rule are also destructive (the rule is a policy primitive).
    """

    a_rules = (a.get("instinct_rules") or {}).get("rules", []) or []
    b_rules = (b.get("instinct_rules") or {}).get("rules", []) or []

    a_by_id = _by_name(a_rules, key_fields=("id", "name"))
    b_by_id = _by_name(b_rules, key_fields=("id", "name"))

    for name in sorted(set(b_by_id) - set(a_by_id)):
        diff.instinct_rules_added.append(name)
    for name in sorted(set(a_by_id) - set(b_by_id)):
        diff.instinct_rules_removed.append(name)

    for name in sorted(set(a_by_id) & set(b_by_id)):
        old = a_by_id[name]
        new = b_by_id[name]
        if old == new:
            continue
        for field_name in sorted(set(old) | set(new)):
            old_val = old.get(field_name)
            new_val = new.get(field_name)
            if old_val == new_val:
                continue
            # All field changes on an instinct rule are destructive —
            # rules govern dispatch / approval behaviour.
            diff.instinct_rules_changed.append(
                {
                    "path": f"instinct_rules.rules[{name}].{field_name}",
                    "old": old_val,
                    "new": new_val,
                    "destructive": True,
                }
            )


def _diff_outcomes(a: dict[str, Any], b: dict[str, Any], diff: TemplateDiff) -> None:
    """Diff the top-level outcomes list.

    Per RFC 03 v2 the catalog is ``list[str]`` — each entry is an outcome
    name. Added outcomes are non-destructive (new emission paths).
    Removed outcomes are destructive (consumers may be subscribed to
    them). We also accept ``list[dict]`` defensively for any pre-v2
    fixtures that might still carry the older shape.
    """

    def _to_name_set(items: Any) -> set[str]:
        out: set[str] = set()
        for entry in items or []:
            if isinstance(entry, str):
                out.add(entry)
            elif isinstance(entry, dict) and "name" in entry:
                out.add(str(entry["name"]))
        return out

    a_outcomes = _to_name_set(a.get("outcomes"))
    b_outcomes = _to_name_set(b.get("outcomes"))
    for name in sorted(b_outcomes - a_outcomes):
        diff.outcomes_added.append(name)
    for name in sorted(a_outcomes - b_outcomes):
        diff.outcomes_removed.append(name)


def _by_name(
    items: list[dict[str, Any]] | None,
    *,
    key_fields: tuple[str, ...] = ("name",),
) -> dict[str, dict[str, Any]]:
    """Index a list of dicts by their ``name`` (or first matching key).

    Items without any matching key are skipped — they can't appear in
    a structural diff.
    """

    out: dict[str, dict[str, Any]] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        for key in key_fields:
            if key in item:
                out[str(item[key])] = item
                break
    return out


__all__ = [
    "BundleError",
    "InstallResult",
    "TemplateDiff",
    "compute_template_diff",
    "pack_template",
    "unpack_template",
]

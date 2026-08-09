"""SG-1 acceptance tests: render, verify, fail closed, no installer, and the measurement.

Created for SG-1 (sites proving harness).

WHAT: drives the harness end to end and writes the evidence report. One test per
acceptance criterion, plus focused unit tests for ``verify``'s individual checks
(which must be provable without a Node process).

WHY the renderer build is a session fixture and not a test: building is the
ONE-TIME step whose absence from the render path is the point. Building it inside
a test would blur the line the suite exists to draw, so ``renderer`` asserts the
artifact exists and skips with the exact command if it does not.

Run:
    node tests/harness/sites_proving/node/build.mjs        # once
    uv run pytest tests/harness/sites_proving/ -v
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from .bundle import LANE_RIPPLE, RUNG_PREBUILT_SSR, Bundle, BundleManifest
from .harness import REGISTRY, EvidenceReport
from .measure import measure_sidecar_vs_per_render
from .renderer import (
    BUILD_DIR,
    NODE_DIR,
    PerRenderRenderer,
    RendererNotBuilt,
    SidecarRenderer,
    SiteTokens,
    assert_no_installer_in_render_path,
    render,
    renderer_build_info,
)
from .scenarios import A1_TOKENS, MINIMAL_HERO_SPEC
from .verify import VerifyFailed, verify

# Set PAW_SG1_BUILD=1 to let the suite build the renderer itself. Off by default:
# a test run should not silently spend 30s on a build, and CI builds explicitly.
_AUTO_BUILD = os.environ.get("PAW_SG1_BUILD") == "1"


@pytest.fixture(scope="module")
def build_info() -> dict[str, Any]:
    """The once-built renderer must exist. Skip with instructions if it does not."""
    try:
        return renderer_build_info()
    except RendererNotBuilt as exc:
        if not _AUTO_BUILD:
            pytest.skip(f"{exc}  (or set PAW_SG1_BUILD=1 to build here)")
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["node", str(NODE_DIR / "build.mjs")], cwd=str(NODE_DIR), check=True, timeout=900
        )
        return renderer_build_info()


@pytest.fixture(scope="module")
def report(build_info: dict[str, Any]) -> Any:
    """One report per module run, written at teardown.

    Module-scoped so every test contributes to a SINGLE report.json — the
    artifact SG-12 reads.
    """
    evidence = EvidenceReport()
    evidence.note("renderer", build_info)
    yield evidence
    json_path, text_path = evidence.write()
    print(f"\nevidence: {json_path}\n          {text_path}")


@pytest.fixture(scope="module")
def sidecar(build_info: dict[str, Any]) -> Any:
    with SidecarRenderer() as renderer:
        yield renderer


# --------------------------------------------------------------------------
# Acceptance: Scenario A1
# --------------------------------------------------------------------------


def test_a1_minimal_spec_renders_and_verifies(report: Any) -> None:
    """A1 — a minimal hero spec renders to HTML and verify passes."""
    record = report.run("A1")
    assert record.passed, f"A1 failed: {record.error}\n{record.traceback}"
    assert record.fallback_rung == RUNG_PREBUILT_SSR
    assert record.evidence_path, "A1 must leave an inspectable artifact"

    details = record.details
    assert details["heading_in_html"] is True
    assert details["body_copy_in_html"] is True
    assert details["form_action_in_html"] is True
    assert sorted(details["input_names_in_html"]) == ["full_name", "phone"]
    assert details["title_token_substituted"] is True
    assert details["primary_color_token_substituted"] is True

    manifest = details["manifest"]
    assert manifest["lane"] == LANE_RIPPLE
    assert manifest["entry_html"] == "index.html"
    assert manifest["renderer_version"].startswith("ripple-")


def test_bundle_manifest_contract_is_complete(sidecar: Any) -> None:
    """The manifest carries everything a later slice was promised."""
    bundle = sidecar.render(MINIMAL_HERO_SPEC, A1_TOKENS)
    manifest = bundle.manifest

    assert manifest.entry_html in bundle.files
    assert manifest.lane == LANE_RIPPLE
    assert manifest.fallback_rung == RUNG_PREBUILT_SSR
    assert manifest.renderer_version
    assert isinstance(manifest.needs_server_worker, bool)
    for asset in manifest.asset_paths:
        assert asset in bundle.files, f"manifest names {asset}, bundle lacks it"
    assert bundle.files[manifest.entry_html] == bundle.entry_bytes

    # A1's tokens configure a capture base, so the site needs a worker for the POST.
    assert manifest.needs_server_worker is True
    # And the signed key must never leak into the recorded manifest.
    serialized = json.dumps(manifest.as_dict())
    assert A1_TOKENS.signed_key not in serialized
    assert '"signed_key_present": true' in serialized


def test_one_renderer_serves_many_specs(sidecar: Any) -> None:
    """The premise: ONE built renderer, many sites — no rebuild between them."""
    before = renderer_build_info()["bundle_sha256"]

    specs = [
        {"type": "container", "children": [{"type": "heading", "props": {"text": f"Site {i}"}}]}
        for i in range(4)
    ]
    for index, spec in enumerate(specs):
        tokens = SiteTokens(
            site_id=f"multi-{index}",
            title=f"Site {index}",
            primary_color=f"#00{index}0FF",
            form_action=f"/api/submit?site={index}",
        )
        bundle = sidecar.render(spec, tokens)
        verify(bundle, expected_form_action=tokens.form_action)
        html = bundle.entry_text()
        assert f"Site {index}" in html
        assert f'action="/api/submit?site={index}"' in html
        assert tokens.primary_color in html

    assert renderer_build_info()["bundle_sha256"] == before, "the bundle was rebuilt mid-run"


# --------------------------------------------------------------------------
# Acceptance: Scenario A8 — fail closed
# --------------------------------------------------------------------------


def test_a8_malformed_spec_fails_closed(report: Any) -> None:
    """A8 — malformed/empty specs raise, and no deploy step is reached."""
    record = report.run("A8")
    assert record.passed, f"A8 failed: {record.error}\n{record.traceback}"

    details = record.details
    assert details["deploy_never_reached"] is True
    assert details["deploy_calls"] == []
    # Every malformed input must be individually accounted for.
    for label in (
        "empty-dict",
        "empty-list",
        "null",
        "empty-string",
        "children-not-a-list",
        "unknown-widget",
        "intent-no-ui",
        "ui-null",
        "ui-empty-container",
    ):
        assert label in details["refused"], f"{label} was not exercised"
        assert details["refused"][label].startswith(("VerifyFailed", "RenderFailed"))


def test_verify_rejects_ripple_empty_placeholder() -> None:
    """Regression: the silent-empty case found while building this slice.

    A `{intent:'custom'}` spec with no `ui` key makes Ripple.svelte render its own
    "No UI definition for intent" placeholder. That is real text in a real body, so
    the empty-body check counted it as CONTENT and verify PASSED a blank site. Both
    the class and the sentence are asserted, since either alone identifies it.
    """
    placeholder = '<div class="ripple-empty">No UI definition for intent: custom</div>'
    with pytest.raises(VerifyFailed, match="render error marker"):
        verify(_bundle(f"<!doctype html><html><body>{placeholder}{_FORM}</body></html>"))

    with pytest.raises(VerifyFailed, match="No UI definition for intent"):
        verify(
            _bundle(
                "<!doctype html><html><body>"
                "<div>No UI definition for intent: dashboard</div>"
                f"{_FORM}</body></html>"
            )
        )


def test_intent_only_spec_is_refused_end_to_end(sidecar: Any) -> None:
    """The same gap, through a REAL render rather than a hand-written string."""
    bundle = sidecar.render({"intent": "custom"}, SiteTokens(site_id="io", title="IO"))
    assert "ripple-empty" in bundle.entry_text(), "ripple stopped emitting the placeholder"
    with pytest.raises(VerifyFailed):
        verify(bundle, expected_form_action="/api/submit")


def test_verify_raises_rather_than_returning_a_verdict() -> None:
    """The fail-closed shape itself: verify has no falsy return to ignore."""
    empty = Bundle(
        files={"index.html": b"<!doctype html><html><body></body></html>"},
        manifest=BundleManifest(
            entry_html="index.html",
            asset_paths=(),
            needs_server_worker=False,
            lane=LANE_RIPPLE,
            renderer_version="test",
        ),
    )
    with pytest.raises(VerifyFailed):
        verify(empty)

    # And a passing verify returns None — so `if verify(b):` can never gate a deploy.
    good = Bundle(
        files={
            "index.html": (
                b"<!doctype html><html><body><h1>Hi</h1>"
                b'<form method="POST" action="/api/submit"></form></body></html>'
            )
        },
        manifest=empty.manifest,
    )
    assert verify(good) is None


# --------------------------------------------------------------------------
# verify() unit tests — each check provable without a Node process
# --------------------------------------------------------------------------


def _bundle(html: str, extra_files: dict[str, bytes] | None = None) -> Bundle:
    files: dict[str, bytes] = {"index.html": html.encode("utf-8")}
    files.update(extra_files or {})
    return Bundle(
        files=files,
        manifest=BundleManifest(
            entry_html="index.html",
            asset_paths=tuple(sorted(extra_files or {})),
            needs_server_worker=False,
            lane=LANE_RIPPLE,
            renderer_version="test",
        ),
    )


_FORM = '<form method="POST" action="/api/submit"><input name="a" /></form>'


def test_verify_rejects_missing_entry() -> None:
    bundle = Bundle(
        files={"other.html": b"<html><body>hi</body></html>"},
        manifest=BundleManifest(
            entry_html="index.html",
            asset_paths=(),
            needs_server_worker=False,
            lane=LANE_RIPPLE,
            renderer_version="test",
        ),
    )
    with pytest.raises(VerifyFailed, match="not in the bundle"):
        verify(bundle)


def test_verify_rejects_body_of_only_hydration_comments() -> None:
    """The silent-empty trap: structure and comments, zero content."""
    html = (
        "<!doctype html><html><body><!--[-->"
        '<form method="POST" action="/api/submit">'
        '<div class="ripple-root"><!--[4--><!--[0--><div data-ripple-container="true">'
        "<!--[--><!--]--></div><!--]--><!--]--></div></form><!--]--></body></html>"
    )
    with pytest.raises(VerifyFailed, match="body is empty"):
        verify(_bundle(html))


def test_verify_rejects_missing_form_action() -> None:
    html = "<!doctype html><html><body><h1>Hi</h1><form method='POST'></form></body></html>"
    with pytest.raises(VerifyFailed, match="no <form action"):
        verify(_bundle(html))


def test_verify_rejects_empty_form_action() -> None:
    html = '<!doctype html><html><body><h1>Hi</h1><form action=""></form></body></html>'
    with pytest.raises(VerifyFailed, match="action is empty"):
        verify(_bundle(html))


def test_verify_rejects_wrong_form_action_when_pinned() -> None:
    html = f"<!doctype html><html><body><h1>Hi</h1>{_FORM}</body></html>"
    with pytest.raises(VerifyFailed, match="expected <form action"):
        verify(_bundle(html), expected_form_action="/api/capture")


def test_verify_rejects_dangling_internal_link() -> None:
    html = (
        f'<!doctype html><html><body><h1>Hi</h1><a href="/missing.html">x</a>{_FORM}</body></html>'
    )
    with pytest.raises(VerifyFailed, match="not in the bundle"):
        verify(_bundle(html))


def test_verify_rejects_dangling_in_page_anchor() -> None:
    html = f'<!doctype html><html><body><h1>Hi</h1><a href="#nope">x</a>{_FORM}</body></html>'
    with pytest.raises(VerifyFailed, match="points at no id"):
        verify(_bundle(html))


def test_verify_accepts_resolvable_links_and_external_schemes() -> None:
    html = (
        '<!doctype html><html><head><link rel="stylesheet" href="/assets/theme.css" /></head>'
        '<body><h1 id="top">Hi</h1>'
        '<a href="#top">top</a>'
        '<a href="https://example.com">out</a>'
        '<a href="mailto:hi@example.com">mail</a>'
        '<a href="tel:+15551234567">call</a>'
        '<a href="/pricing/">pricing</a>'
        f"{_FORM}</body></html>"
    )
    bundle = _bundle(
        html,
        {"assets/theme.css": b":root{}", "pricing/index.html": b"<html><body>p</body></html>"},
    )
    assert verify(bundle, expected_form_action="/api/submit") is None


def test_verify_rejects_render_error_marker() -> None:
    """The out-of-catalog box: a "successful" render of a page full of errors.

    The markup here is copied from ripple 0.5.0's NodeRenderer.svelte, not
    invented — an earlier version of this test asserted on guessed prose
    ("Unknown widget type") that ripple never emits, so it passed while the check
    it guarded was disarmed. Both the attribute and the visible sentence are
    asserted separately so a copy change upstream cannot silently disarm it again.
    """
    box = (
        '<div class="text-red-500" role="alert" data-ripple-unknown-widget="no-such-widget">'
        '<strong>Widget type "no-such-widget" isn\'t in the catalog.</strong></div>'
    )
    with pytest.raises(VerifyFailed, match="render error marker"):
        verify(_bundle(f"<!doctype html><html><body><h1>Hi</h1>{box}{_FORM}</body></html>"))

    # The attribute alone must trip it, even with the prose removed.
    attr_only = '<div data-ripple-unknown-widget="x">?</div>'
    with pytest.raises(VerifyFailed, match="data-ripple-unknown-widget"):
        verify(_bundle(f"<!doctype html><html><body><h1>Hi</h1>{attr_only}{_FORM}</body></html>"))


def test_verify_rejects_non_utf8_entry() -> None:
    bundle = Bundle(
        files={"index.html": b"\xff\xfe\x00bad"},
        manifest=BundleManifest(
            entry_html="index.html",
            asset_paths=(),
            needs_server_worker=False,
            lane=LANE_RIPPLE,
            renderer_version="test",
        ),
    )
    with pytest.raises(VerifyFailed, match="not valid UTF-8"):
        verify(bundle)


# --------------------------------------------------------------------------
# Acceptance: zero installer runs during a render
# --------------------------------------------------------------------------


def test_no_installer_runs_during_a_render(report: Any, sidecar: Any) -> None:
    """Structural proof, plus a live observation on a real render.

    Structural: the render drivers and this module's render path never name an
    installer, and spawn nothing but ``node``.

    Observed: a render is timed against the install cost. ``bun install`` for this
    bundle took ~15s cold; a render that completes in well under a second cannot
    have run one. Combined with the source-level proof, a render demonstrably has
    no installer in it.
    """
    evidence = assert_no_installer_in_render_path()

    lock_before = _lockfile_state()
    bundle = sidecar.render(MINIMAL_HERO_SPEC, A1_TOKENS)
    verify(bundle, expected_form_action=A1_TOKENS.form_action)
    render_ms = bundle.manifest.extra["render_ms"]

    # An installer touches the lockfile and node_modules; nothing may change.
    assert _lockfile_state() == lock_before, "a render mutated the install state"
    assert render_ms < 1000, f"render took {render_ms}ms — suspiciously install-shaped"

    evidence["observed_render_ms"] = render_ms
    evidence["install_state_unchanged"] = True
    evidence["one_time_install_s_for_comparison"] = 15
    report.measure("no_installer_in_render_path", evidence)


def _lockfile_state() -> dict[str, Any]:
    """Mtimes/sizes of the artifacts a package installer would touch."""
    state: dict[str, Any] = {}
    for name in ("bun.lock", "bun.lockb", "package-lock.json", "package.json"):
        path = BUILD_DIR / name
        state[name] = (path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else None
    node_modules = BUILD_DIR / "node_modules"
    state["node_modules_entries"] = (
        len(list(node_modules.iterdir())) if node_modules.exists() else None
    )
    return state


def test_render_path_touches_no_sveltekit_project(sidecar: Any) -> None:
    """No per-site project is scaffolded — the cost this slice removes."""
    before = {p.name for p in BUILD_DIR.iterdir()}
    sidecar.render(MINIMAL_HERO_SPEC, A1_TOKENS)
    assert {p.name for p in BUILD_DIR.iterdir()} == before

    # And nothing SvelteKit-shaped was ever created for a site.
    for forbidden in (".svelte-kit", "svelte.config.js", "wrangler.toml"):
        assert not (BUILD_DIR / forbidden).exists(), f"{forbidden} appeared in the render path"


# --------------------------------------------------------------------------
# Acceptance: the sidecar-vs-per-render measurement
# --------------------------------------------------------------------------


def test_measure_sidecar_vs_per_render(report: Any) -> None:
    """Both arms, real numbers, every render verified."""
    measurement = measure_sidecar_vs_per_render(MINIMAL_HERO_SPEC, A1_TOKENS)
    report.measure("sidecar_vs_per_render", measurement)

    assert measurement["sidecar"]["warm"]["n"] >= 2
    assert measurement["per_render"]["warm"]["n"] >= 2
    assert measurement["sidecar"]["warm"]["median_ms"] > 0
    assert measurement["per_render"]["warm"]["median_ms"] > 0
    # The one structural claim: a fresh process per render cannot be cheaper than
    # reusing an imported bundle. If this ever inverts, the measurement is wrong.
    assert (
        measurement["per_render"]["warm"]["median_ms"] > measurement["sidecar"]["warm"]["median_ms"]
    )

    print(
        "\n  sidecar  cold(start+import)={}ms  warm median={}ms".format(
            measurement["sidecar"]["process_start_plus_import_ms"],
            measurement["sidecar"]["warm"]["median_ms"],
        )
    )
    print(
        "  per-render  cold={}ms  warm median={}ms  (+{}ms vs sidecar)".format(
            measurement["per_render"]["cold_total_ms"],
            measurement["per_render"]["warm"]["median_ms"],
            measurement["per_render_overhead_vs_sidecar_ms"],
        )
    )


def test_both_drivers_agree_on_output(build_info: dict[str, Any]) -> None:
    """The two arms must be the same work, or the measurement compares nothing."""
    tokens = SiteTokens(site_id="agree", title="Agree")
    per_render = PerRenderRenderer().render(MINIMAL_HERO_SPEC, tokens)
    with SidecarRenderer() as sc:
        resident = sc.render(MINIMAL_HERO_SPEC, tokens)

    verify(per_render, expected_form_action=tokens.form_action)
    verify(resident, expected_form_action=tokens.form_action)
    assert per_render.entry_bytes == resident.entry_bytes
    assert per_render.manifest.renderer_version == resident.manifest.renderer_version


# --------------------------------------------------------------------------
# Harness skeleton
# --------------------------------------------------------------------------


def test_registry_holds_the_sg1_scenarios() -> None:
    assert REGISTRY.ids() == ["A1", "A8"]
    assert REGISTRY.get("A8").expects_failure is True
    assert REGISTRY.get("A1").expects_failure is False
    with pytest.raises(KeyError, match="no scenario"):
        REGISTRY.get("A99")


def test_report_is_machine_readable_and_human_readable(tmp_path: Path) -> None:
    """The report contract, provable without a renderer."""
    evidence = EvidenceReport(report_dir=tmp_path, slice_id="SG-1-test")
    evidence.add(
        __import__(
            "tests.harness.sites_proving.harness", fromlist=["ScenarioRecord"]
        ).ScenarioRecord(
            id="X1",
            description="a synthetic record",
            passed=True,
            fallback_rung=RUNG_PREBUILT_SSR,
            duration_ms=12.5,
            evidence_path="artifacts/X1-index.html",
            details={"k": "v"},
        )
    )
    evidence.measure("m", {"a": 1})
    json_path, text_path = evidence.write()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["slice"] == "SG-1-test"
    assert payload["all_passed"] is True
    scenario = payload["scenarios"][0]
    assert scenario["id"] == "X1"
    assert scenario["fallback_rung"] == RUNG_PREBUILT_SSR
    assert scenario["evidence_path"] == "artifacts/X1-index.html"
    assert payload["measurements"]["m"] == {"a": 1}

    summary = text_path.read_text(encoding="utf-8")
    assert "[PASS] X1" in summary
    assert f"rung={RUNG_PREBUILT_SSR}" in summary


def test_render_convenience_entry_supports_both_drivers(build_info: dict[str, Any]) -> None:
    tokens = SiteTokens(site_id="conv", title="Conv")
    for driver in ("sidecar", "per-render"):
        bundle = render(MINIMAL_HERO_SPEC, tokens, driver=driver)
        verify(bundle, expected_form_action=tokens.form_action)
        assert bundle.manifest.extra["driver"] == driver
    with pytest.raises(ValueError, match="unknown driver"):
        render(MINIMAL_HERO_SPEC, tokens, driver="nope")

"""The SG-1 scenarios: A1 (a spec renders and verifies) and A8 (fail closed).

Created for SG-1 (sites proving harness).

WHAT: two registered scenarios plus the shared spec fixtures.

* **A1** — a minimal spec (one hero section) renders through the once-built
  renderer and ``verify`` passes.
* **A8** — malformed and empty specs FAIL CLOSED: ``verify`` raises and a
  tracked deploy step is never reached.

WHY A8 tracks a deploy call rather than just asserting the raise: "verify raised"
and "nothing proceeded" are different claims, and the second is the one that
matters. ``_DeployTripwire`` stands in for the real deploy step and records
whether it was ever called, so the scenario proves the raise actually stopped the
pipeline. The tripwire is fail-closed itself: it is only reached by code that
ignored the exception.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .bundle import RUNG_PREBUILT_SSR, Bundle
from .harness import register
from .renderer import RenderFailed, SiteTokens, render
from .verify import VerifyFailed, verify

# A1's spec: one hero section — heading, subheading, and the lead form controls
# with a submit button, which is the minimum that exercises the <form action>
# assertion. Deliberately `{ui: ...}`-free at this level: it is a BARE UINode,
# the shape pockets actually hand over, so the render path's normalization is
# under test rather than bypassed.
MINIMAL_HERO_SPEC: dict[str, Any] = {
    "type": "container",
    "theme": {"primary": "#0A84FF", "mode": "light"},
    "children": [
        {"type": "heading", "props": {"text": "Bright Smile Dental"}},
        {"type": "text", "props": {"text": "Modern dentistry in downtown Reno."}},
        {
            "type": "container",
            "children": [
                {"type": "input", "props": {"name": "full_name", "label": "Your name"}},
                {"type": "input", "props": {"name": "phone", "label": "Phone"}},
                {
                    "type": "button",
                    "props": {
                        "label": "Request appointment",
                        "variant": "primary",
                        "type": "submit",
                    },
                    "on": {
                        "click": {
                            "action": "api",
                            "url": "/api/submit",
                            "method": "POST",
                            "body": {
                                "full_name": "{state.full_name}",
                                "phone": "{state.phone}",
                            },
                        }
                    },
                },
            ],
        },
    ],
}

A1_TOKENS = SiteTokens(
    site_id="sg1-a1-hero",
    title="Bright Smile Dental",
    primary_color="#0A84FF",
    capture_api_base="https://capture.example.test",
    signed_key="test-signing-key-not-a-real-secret",
    d1_database_id="00000000-0000-0000-0000-000000000000",
    csr=False,
    form_action="/api/submit",
)

# A8's inputs. Each must be refused, and each fails for a DIFFERENT reason, so a
# single overly-broad check cannot make the scenario pass by accident:
#   empty-dict / empty-list / None -> nothing to render at all
#   bare-node-unwrapped            -> the silent-empty trap: ripple's normalizeSpec
#                                     ignores a bare {type,children}, so an
#                                     un-normalized node renders a page with a
#                                     form and NO content. The most dangerous case
#                                     precisely because the render "succeeds".
#   children-not-a-list            -> structurally wrong where ripple expects a list
#   unknown-widget                 -> renders NodeRenderer's loud error box
#   intent-no-ui                   -> the SILENT-EMPTY case, found while building
#                                     this slice: ripple UNDERSTANDS the spec and
#                                     renders its own "No UI definition for
#                                     intent" placeholder. Real text in a real
#                                     body, so a naive content check passes it and
#                                     a BLANK site verifies green.
#   ui-null / ui-empty-container   -> a well-formed envelope with nothing in it
#
# Every entry is a spec the renderer accepts as input; none is a Python-level
# type error dressed up as a test. Each is rendered for real and must be refused.
MALFORMED_SPECS: dict[str, Any] = {
    "empty-dict": {},
    "empty-list": [],
    "null": None,
    "empty-string": "",
    "children-not-a-list": {"type": "container", "children": "not a list"},
    "unknown-widget": {"ui": {"type": "container", "children": [{"type": "no-such-widget-xyz"}]}},
    "intent-no-ui": {"intent": "custom"},
    "ui-null": {"ui": None},
    "ui-empty-container": {"ui": {"type": "container", "children": []}},
}

A8_TOKENS = SiteTokens(site_id="sg1-a8-malformed", title="Malformed", csr=False)


class DeployReached(AssertionError):
    """A deploy step ran on an unverified bundle. The gate leaked."""


@dataclass
class _DeployTripwire:
    """Stands in for the deploy step A8 must never reach."""

    calls: list[str] = field(default_factory=list)

    def deploy(self, label: str) -> None:
        self.calls.append(label)
        raise DeployReached(
            f"deploy was reached for {label!r} on a spec that must have been refused"
        )

    @property
    def never_called(self) -> bool:
        return not self.calls


def _publish(spec: Any, tokens: SiteTokens, tripwire: _DeployTripwire, label: str) -> Bundle:
    """The publish shape under test: render -> verify -> deploy, in that order.

    The gate is the bare ``verify`` call: it raises, so the ``deploy`` below is
    unreachable on a bad bundle. That ordering is the thing A8 proves, and it is
    written here once so both scenarios exercise the same pipeline.
    """
    bundle = render(spec, tokens)
    verify(bundle, expected_form_action=tokens.form_action)
    tripwire.deploy(label)
    return bundle


@register("A1", "A minimal hero spec renders to HTML and verify passes")
def scenario_a1() -> tuple[Bundle, dict[str, Any]]:
    bundle = render(MINIMAL_HERO_SPEC, A1_TOKENS)
    verify(bundle, expected_form_action=A1_TOKENS.form_action)

    html = bundle.entry_text()
    # Spot-check the things a passing verify does not by itself prove: the spec's
    # own copy reached the page, and the per-site tokens were substituted.
    evidence = {
        "heading_in_html": "Bright Smile Dental" in html,
        "body_copy_in_html": "Modern dentistry in downtown Reno." in html,
        "form_action_in_html": 'action="/api/submit"' in html,
        "input_names_in_html": [
            name for name in ("full_name", "phone") if f'name="{name}"' in html
        ],
        "title_token_substituted": f"<title>{A1_TOKENS.title}</title>" in html,
        "primary_color_token_substituted": A1_TOKENS.primary_color in html,
        "entry_html_bytes": len(bundle.entry_bytes),
        "verify": "passed",
    }
    missing = [k for k, v in evidence.items() if v is False]
    if missing or len(evidence["input_names_in_html"]) != 2:
        raise AssertionError(f"A1 rendered but evidence is incomplete: {evidence}")
    return bundle, evidence


@register(
    "A8",
    "A malformed or empty spec fails verification closed; deploy is never reached",
    expects_failure=True,
)
def scenario_a8() -> tuple[None, dict[str, Any]]:
    tripwire = _DeployTripwire()
    outcomes: dict[str, str] = {}

    for label, spec in MALFORMED_SPECS.items():
        try:
            _publish(spec, A8_TOKENS, tripwire, label)
        except VerifyFailed as exc:
            outcomes[label] = f"VerifyFailed: {exc}"
        except RenderFailed as exc:
            # Also fail-closed: the renderer refused before verify was reached.
            outcomes[label] = f"RenderFailed: {exc}"
        else:
            raise AssertionError(f"{label!r} was accepted — a malformed spec must never verify")

    if not tripwire.never_called:
        raise AssertionError(f"deploy ran for {tripwire.calls}")

    return None, {
        "fallback_rung": RUNG_PREBUILT_SSR,
        "refused": outcomes,
        "deploy_calls": tripwire.calls,
        "deploy_never_reached": tripwire.never_called,
        "cases": len(outcomes),
    }

# tests/cloud/sites/test_no_gateway_in_sites.py — Paw Sites must not reach a
# payment gateway, asserted against the SOURCE rather than against a double.
#
# Created 2026-09-05 (fix/sites-plan-credits). A paid site is charged to the
# workspace credit balance; the two Dodo rails that preceded it are deleted. The
# obvious way to guard that is an injected provider whose every method raises —
# and that is what this replaced — but it can only intercept a call made THROUGH
# the injection seam. A reintroduced gateway call would build its own client, sail
# past the double, and bill a customer who had already paid from their balance.
#
# So the guard is structural: the sites package may not name the payments
# provider or its verbs at all. That fails at the moment somebody types the
# import, which is the moment it is cheapest to notice.
#
# It scans SOURCE TEXT, deliberately, rather than imports: ``publish_pocket``
# reached its provider through a lazy in-function import, so an import-graph check
# would have called the old code clean.

from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("pocketpaw_ee")

# The sites package. Resolved from the module rather than from a path literal so
# a package move does not quietly empty this test.
import pocketpaw_ee.sites as _sites  # noqa: E402

SITES_DIR = pathlib.Path(_sites.__file__).parent

# Gateway names that must not appear in the sites package. ``DodoProvider`` and
# the provider module are the client; the three verbs are the ways money is moved
# with it. ``dodo_site_products`` / ``dodo_site_addons`` are the settings that
# used to decide which rail ran — the env vars are documented as inert now, and a
# reference here would be the first step to making them mean something again.
FORBIDDEN = (
    "DodoProvider",
    "billing.providers",
    "create_subscription",
    "change_plan",
    "cancel_subscription",
    "dodo_site_products",
    "dodo_site_addons",
)

# ``renewal_sweeper`` and the runbook-facing comments explain WHY there is no
# gateway, and saying so requires naming it. Only prose is exempt, and only in
# the file below — the scan strips comments and docstrings everywhere else too,
# so an explanation anywhere is fine and a call is never.
ALLOWED_PROSE_ONLY = True


def _code_lines(path: pathlib.Path) -> list[tuple[int, str]]:
    """The file's lines with whole-line comments dropped.

    Docstrings and inline comments are handled by the caller's own filtering; the
    point of stripping here is that this module's history is full of comments
    that legitimately name the rails they describe. A comment cannot charge a
    card. A line of code can.
    """
    out: list[tuple[int, str]] = []
    in_doc = False
    delim = ""
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if in_doc:
            if delim in line:
                in_doc = False
            continue
        if line.startswith("#"):
            continue
        for d in ('"""', "'''"):
            if line.startswith(d):
                # A one-line docstring opens and closes on the same line.
                if line.count(d) == 1:
                    in_doc, delim = True, d
                line = ""
                break
        if not line:
            continue
        # Trim a trailing comment, which is where most of the surviving mentions
        # live — ``foo()  # the add-on rail used to call change_plan here``.
        if "#" in line:
            line = line.split("#", 1)[0]
        if line.strip():
            out.append((n, line))
    return out


def test_the_sites_package_names_no_payment_gateway():
    """No module under ``pocketpaw_ee/sites`` may reference the payments provider
    or its money verbs in CODE.

    Breaks on: reintroducing ``from ...providers.dodo import DodoProvider``, or a
    call to ``create_subscription`` / ``change_plan`` / ``cancel_subscription``,
    anywhere in the package — including inside a function, which is how the old
    rails imported it and how an import-graph check would have missed them.
    """
    offences: list[str] = []
    for path in sorted(SITES_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for lineno, line in _code_lines(path):
            for name in FORBIDDEN:
                if name in line:
                    rel = path.relative_to(SITES_DIR.parent.parent)
                    offences.append(f"{rel}:{lineno}: {name} in {line.strip()!r}")

    assert not offences, (
        "Paw Sites reached a payment gateway. A paid site is charged to the "
        "workspace credit balance and the Dodo rails were deleted on 2026-09-05; "
        "see docs/runbooks/2026-09-05-site-plans-on-credits.md.\n  " + "\n  ".join(offences)
    )


def test_the_scan_can_actually_fail(tmp_path):
    """The guard above is a text scan, and a text scan that silently matches
    nothing is indistinguishable from a clean tree. Feed the stripper a file that
    calls the gateway and prove it is seen — and that the same name in a comment
    and in a docstring is not.

    Without this, deleting the ``FORBIDDEN`` tuple's contents would leave a test
    that passes forever.
    """
    f = tmp_path / "sample.py"
    f.write_text(
        '"""A docstring mentioning create_subscription is fine."""\n'
        "# So is a comment mentioning change_plan.\n"
        "x = 1  # and a trailing one naming DodoProvider\n"
        "await prov.create_subscription(plan_key='site')\n",
        encoding="utf-8",
    )

    hits = [line for _, line in _code_lines(f) if any(n in line for n in FORBIDDEN)]

    assert len(hits) == 1, f"the stripper reported {hits!r}"
    assert "create_subscription" in hits[0]

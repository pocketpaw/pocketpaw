# tests/scenarios/__init__.py — Phase 4 outcome-driven scenario tests.
# Created: 2026-05-08
#
# Scenario tests prove user-journey OUTCOMES against a live pocketpaw
# runtime. Distinct from unit tests (in tests/) which mock the runtime,
# and contract tests (in paw-enterprise/tests/contract/) which pin
# response SHAPES. Scenarios pin behavioural invariants over a
# multi-step flow.
#
# Each scenario file is named test_s<N>_<slug>.py. See docs/playbooks/
# grand-smoke.md (paw-workspace) for the full Phase 4 roster.

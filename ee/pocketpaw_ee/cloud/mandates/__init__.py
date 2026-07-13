# ee/pocketpaw_ee/cloud/mandates/__init__.py — the Belt MANDATE primitive.
# Created: 2026-06-11 (feat/belt-mandates).
#
# A MANDATE is a standing JOB the Belt holds over time (an FDE retainer): it
# senses its surface via PATROLS, plans a FEW tasks per SHIFT via a FOREMAN
# (LLM judgment), routes the plan through a PLAN GATE (Instinct ``belt_plan``
# proposal), and dispatches approved tasks as normal Belt runs. 4-file entity:
# domain.py (models + value objects) / dto.py / service.py / router.py.
from pocketpaw_ee.cloud.mandates.router import router  # noqa: F401

# ee/pocketpaw_ee/cloud/instinct_approvals/bridges/__init__.py
# Created: 2026-08-06 (feat/coupling-template-approvals, T-5) — package marker
# for the instinct-approval event bridges. Mirrors ``meetings/bridges/``: a
# bridge translates an ``instinct_approvals`` domain event into another
# domain's write (today: a persisted notification) without either domain
# importing the other.

"""Event bridges for ``instinct_approvals``."""

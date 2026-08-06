# ee/pocketpaw_ee/cloud/growth/bridges/__init__.py — cross-domain wiring that
# feeds the /growth pipeline from other domains' events.
#
# Created 2026-08-06 (feat/coupling-lead-to-prospect, T-7): mirrors
# ``leads/bridges`` and ``meetings/bridges``. Growth knows nothing about who
# fills its pipeline; each bridge here subscribes to another domain's event and
# translates it into growth's vocabulary.
#
# * ``leads.py`` — ``lead.captured`` → a Prospect, so a form submitted on a
#   published Paw Site lands in /growth instead of dead-ending in the Leads
#   list for somebody to re-key by hand.

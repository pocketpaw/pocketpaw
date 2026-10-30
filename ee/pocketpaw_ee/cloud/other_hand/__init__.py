# ee/pocketpaw_ee/cloud/other_hand — the Otherhand page-snapshot HTTP surface.
#
# Created: 2026-08-25 (feat/other-hand-surface, Otherhand v1) — one endpoint,
# ``POST /other-hand/pages/{page_id}/snapshot``, and the service that backs it.
#
# Otherhand is a notebook page the user handwrites on; the agent reads the page
# and writes/draws back onto it. This package exists because of a single
# constraint in the run pipeline: THE AGENT CANNOT BE SENT AN IMAGE. Attachments
# are text-extracted (an image upload yields an empty stub), and the SDK is
# invoked with a plain ``prompt: str`` — no content blocks, no vision call, and
# no vision credentials configured. What the agent DOES have is ``Read``, in its
# default SDK tool set, which reads image files natively.
#
# So the page travels to the agent as a FILE. The frontend renders the page to a
# PNG, POSTs it here on pen-idle, and gets back an absolute path; it stamps that
# path (and ``free_y``) onto the next chat turn's surface meta, and the
# ``other_hand`` surface handler puts the path in the preamble for the agent to
# ``Read``. No new credentials, no change to the shared run pipeline.
#
# One live snapshot per page: the write OVERWRITES by ``page_id``, and v1 keeps
# no history. The router is thin (validate, delegate, return the wire dict); the
# filesystem discipline — the tenant scoping and the traversal guard — is the
# service's, in ``service.py``.

from __future__ import annotations

# Paw Sites — OSS-core form-capture ingest primitive for Paw Sites (RFC 12).
# Created 2026-05-30 (Task 3.1). Pure, dependency-free generalization of the
# paw_bar widget ingest hardening (origin pinning, honeypot, mapping
# interpolation) so the cloud Lead-capture entity and any future caller share
# one implementation. Rate-limit COUNTING stays in the store; this package only
# holds the stateless predicates + interpolation and their Pydantic models.

from pocketpaw.sites_capture.ingest import (
    interpolate_mapping,
    is_honeypot_tripped,
    origin_allowed,
)
from pocketpaw.sites_capture.models import (
    MAX_PAYLOAD_BYTES,
    SiteEventMapping,
    SiteFormConfig,
    SiteFormSubmission,
)

__all__ = [
    "MAX_PAYLOAD_BYTES",
    "SiteEventMapping",
    "SiteFormConfig",
    "SiteFormSubmission",
    "interpolate_mapping",
    "is_honeypot_tripped",
    "origin_allowed",
]

# Sense tier — provider-agnostic capability vocabulary above Connectors.
# Created: 2026-06-08 — OSS catalog half (RFC Sense tier, chunk 1).
# Exposes the curated core vocabulary, sense-id validation, and the static
# connector index. The resolver (tenant binding) is built in a later EE chunk
# and is NOT part of this package.

from pocketpaw.senses.vocabulary import (
    CORE_SENSES,
    SENSE_VOCAB_VERSION,
    CoreSense,
    SenseValidationError,
    connectors_for_sense,
    is_core_sense,
    validate_sense_id,
)

__all__ = [
    "CORE_SENSES",
    "SENSE_VOCAB_VERSION",
    "CoreSense",
    "SenseValidationError",
    "connectors_for_sense",
    "is_core_sense",
    "validate_sense_id",
]

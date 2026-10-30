"""What a channel turn hands the assembler — one object, all plain data.

Created: 2026-08-03 (PA-7a, feat/prompt-assembler-channel).

Fifteen channel blocks need inputs, and putting fifteen fields on
:class:`~pocketpaw.prompt.layer.PromptContext` would bury the four the cloud
path reads under a dozen that only Telegram and Discord ever set. So the channel
path's inputs travel together in one frozen object hung off a single
``channel_inputs`` slot, defaulted to ``None`` — a cloud assembly is unchanged
and a channel layer that somehow lands in a cloud list renders nothing rather
than raising.

THE FIRST THREE FIELDS ARE THE LATENCY DECISION, and they are why this class
exists at all rather than the layers doing their own I/O.
``AgentContextBuilder.build_system_prompt`` runs the bootstrap fetch, the memory
fetch and the kb fetch CONCURRENTLY through one ``asyncio.gather``; the kb one
shells out to a subprocess. :func:`~pocketpaw.prompt.assembler.assemble` renders
layers in a sequential ``for`` loop, so three layers each awaiting their own
fetch would pay their SUM on every channel turn where the gather pays their MAX.
The gather therefore stays in the builder and its three results arrive here as
finished strings — the same discipline ``surface_preamble`` and ``atlas_primer``
already use for content produced somewhere the layer cannot reach. The
alternative (make ``assemble`` render concurrently) was rejected: it changes the
CLOUD path's execution model to fix a channel-path problem, and the cloud layers
have never been audited for order-independence. Pinned by
``tests/test_channel_prompt_layers.py::test_the_three_io_fetches_still_run_concurrently``.

``identity_cache_key`` is the provider's own claim about its identity bytes (see
``BootstrapContext.identity_cache_key``), carried so the identity layer can key
on a claim rather than on a soul rendering whose mood and memory counters drift
every turn.

Everything else is exactly what ``build_system_prompt`` was already given by its
caller, forwarded unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChannelInputs:
    """The per-turn inputs the channel layers render against."""

    # --- resolved concurrently by the builder's asyncio.gather -------------
    identity: str = ""
    identity_cache_key: str | None = None
    memory_context: str = ""
    kb_context: str = ""

    # --- forwarded verbatim from build_system_prompt's arguments ----------
    # ``channel`` is typed ``Any`` rather than ``Channel`` for the reason
    # ``PromptContext.instance`` is: this package is imported by the prompt
    # registry, which ``bootstrap`` imports, and a hard dependency on
    # ``pocketpaw.bus`` here buys nothing the layers cannot get by importing it
    # themselves at the point of use.
    channel: Any = None
    sender_id: str | None = None
    session_key: str | None = None
    file_context: dict | None = None
    metadata: dict | None = None
    agents_md_dir: str | None = None
    skill_names: frozenset[str] | None = None

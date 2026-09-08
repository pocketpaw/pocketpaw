# ee/pocketpaw_ee/terrarium/llm.py
#
# The CITIZEN JUDGMENT SEAT — one LLM call per citizen per tick. In goes the
# sense digest; out comes strict JSON naming the verbs the citizen chose.
#
# Transports. ``POCKETPAW_TERRARIUM_LLM`` picks between ``mock`` (DEFAULT —
# deterministic, offline, free: tick 1 writes the charter, then speak + build the
# cheapest node; ``set_mock_decision`` scripts one) and ``claude`` (shells the
# CLI, prompt as ONE argv element). A universe that brought its OWN key gets
# ``HttpLlm`` (Messages API over httpx) instead — or, for a world nobody is
# watching, ``BatchLlm``: one Message Batch holding every citizen, run
# asynchronously at HALF price, submitted on one sweep and applied on a later
# one. Mock is the default because a tick fans out one call PER CITIZEN.
#
# The prompt is TWO blocks. ``build_prompt_parts`` returns a STABLE PREFIX
# (world brief, constitution, charter, values and OCEAN, the drift line saying
# how a descendant differs from its parent, verbs, costs, tech tree, rules) and a
# VOLATILE SUFFIX (ground truth, speech, artifacts, weather, outside voices,
# memories). Both HTTP transports send them as two content blocks and mark the
# prefix ``cache_control: ephemeral`` ONLY when it clears that model's minimum
# cacheable length (``MIN_CACHEABLE_TOKENS``) — under it the marker is a silent
# no-op that caches nothing and says nothing about it. Transports without
# ``decide_parts`` (the CLI, the mock, test fakes) get the joined string.
#
# EVERY decide is metered against ``PRICING`` — that is what makes running cost a
# measurement rather than a guess. ``CostMeter`` prices a batched call at half,
# and counts those calls so the saving is visible and not merely assumed.
#
# Nothing the model returns is trusted. ``world.apply_acts`` re-validates every
# act against balance, allowed verbs and held tech before anything mutates.

"""The citizen judgment seat: one strict-JSON decision per tick."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from pocketpaw_ee.terrarium.physics import PhysicsFile
from pocketpaw_ee.terrarium.world import (
    Act,
    CitizenSnapshot,
    Decision,
    SenseDigest,
    unlockable,
)

logger = logging.getLogger(__name__)

_CLI_TIMEOUT = 120.0


class CitizenLlm(Protocol):
    """One tick's judgment: prompt in, raw model text out.

    ``physics``/``citizen``/``digest`` ride along so a deterministic mock can
    answer without parsing prose; real transports send only the prompt.
    """

    async def decide(
        self,
        *,
        prompt: str,
        physics: PhysicsFile,
        citizen: CitizenSnapshot,
        digest: SenseDigest,
    ) -> str: ...


class ClaudeCliLlm:
    """Real transport — shells the ``claude`` CLI (same shape as the foreman)."""

    async def decide(
        self,
        *,
        prompt: str,
        physics: PhysicsFile,
        citizen: CitizenSnapshot,
        digest: SenseDigest,
    ) -> str:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=_CLI_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"claude CLI timed out after {_CLI_TIMEOUT}s") from None
        out = out_b.decode("utf-8", "replace")
        if proc.returncode != 0:
            err = err_b.decode("utf-8", "replace")
            raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {err.strip()[:300]}")
        try:
            envelope = json.loads(out)
        except json.JSONDecodeError:
            return out
        if isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
            return envelope["result"]
        return out


_API_BASE = "https://api.anthropic.com"
_API_URL = f"{_API_BASE}/v1/messages"
_DEFAULT_MODEL = "claude-sonnet-5"
# Physics tier -> model id. Unknown tiers fall back to the default.
_MODEL_BY_TIER = {
    "premium": "claude-opus-5",
    "mid": "claude-sonnet-5",
    "tail": "claude-haiku-4-5",
}

# Characters per token — an APPROXIMATION, used wherever a real count would cost
# a round trip: the cache-minimum check below, and the meter when a transport
# reports no usage. Neither answer it feeds ("about $2 an hour", "is this prefix
# long enough to cache") gets better for being exact.
_CHARS_PER_TOKEN = 4

# The shortest prefix each model will cache AT ALL, in tokens. Source: Anthropic
# prompt-caching reference. The value is MODEL-DEPENDENT and NOT monotonic
# across generations — the cheapest model has the LONGEST minimum, so a prefix
# that caches on Opus can silently fail to cache on Haiku. Under the minimum the
# provider does not error: ``cache_creation_input_tokens`` comes back 0 and the
# whole prefix is billed at full input rate, every tick, forever.
MIN_CACHEABLE_TOKENS: dict[str, int] = {
    "claude-opus-5": 512,
    "claude-sonnet-5": 1024,
    "claude-sonnet-4-6": 1024,
    "claude-haiku-4-5": 4096,
}
_DEFAULT_MIN_CACHEABLE_TOKENS = 1024  # a Claude model not in the table


def model_for_tier(tier: str | None) -> str:
    return _MODEL_BY_TIER.get((tier or "").strip().lower(), _DEFAULT_MODEL)


def cache_marker_fits(model: str, prefix: str) -> bool:
    """Whether marking this prefix would actually buy a cache on this model."""
    if not model.startswith("claude"):
        return False
    minimum = MIN_CACHEABLE_TOKENS.get(model, _DEFAULT_MIN_CACHEABLE_TOKENS)
    return len(prefix) // _CHARS_PER_TOKEN >= minimum


class HttpLlm:
    """BYOK transport — the Anthropic Messages API with the universe's own key.

    ``client`` is injectable so tests stub the transport; the key is held only on
    this instance and never logged."""

    def __init__(
        self, api_key: str, model: str = _DEFAULT_MODEL, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self._key = api_key
        self.model = model
        self._client = client

    def _content(self, prefix: str, suffix: str) -> list[dict[str, Any]]:
        """Two text blocks; the first carries the cache marker only when it is
        long enough for this model to cache. Under the minimum the marker buys
        nothing, so the same two blocks go without it."""
        blocks: list[dict[str, Any]] = []
        if prefix:
            head: dict[str, Any] = {"type": "text", "text": prefix}
            if cache_marker_fits(self.model, prefix):
                head["cache_control"] = {"type": "ephemeral"}
            blocks.append(head)
        if suffix:
            blocks.append({"type": "text", "text": suffix})
        return blocks

    async def call(self, prefix: str, suffix: str = "") -> tuple[str, dict[str, Any]]:
        """One Messages call. Returns the text and the provider's ``usage`` block
        (``cache_read_input_tokens`` and friends) so the meter can price it."""
        headers = {
            "x-api-key": self._key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": self.model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": self._content(prefix, suffix)}],
        }
        client = self._client or httpx.AsyncClient(timeout=_CLI_TIMEOUT)
        try:
            res = await client.post(_API_URL, headers=headers, json=body)
        finally:
            if client is not self._client:
                await client.aclose()
        if res.status_code != 200:
            # The response body may echo request details; keep the status only.
            raise RuntimeError(f"anthropic API failed (HTTP {res.status_code})")
        envelope = res.json()
        blocks = envelope.get("content") if isinstance(envelope, dict) else None
        text = "".join(
            b.get("text", "")
            for b in (blocks or [])
            if isinstance(b, dict) and b.get("type") == "text"
        )
        usage = envelope.get("usage") if isinstance(envelope, dict) else None
        return text or json.dumps(envelope), dict(usage or {})

    async def decide(
        self,
        *,
        prompt: str,
        physics: PhysicsFile,
        citizen: CitizenSnapshot,
        digest: SenseDigest,
    ) -> str:
        return (await self.call(prompt))[0]

    async def decide_parts(
        self,
        prefix: str,
        suffix: str,
        *,
        physics: PhysicsFile,
        citizen: CitizenSnapshot,
        digest: SenseDigest,
    ) -> str:
        return (await self.call(prefix, suffix))[0]


# ---------------------------------------------------------------------------
# THE SLEEPING WORLD'S TRANSPORT
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchEntry:
    """One citizen's slot in a batch.

    ``custom_id`` is the citizen's document id and is the ONLY thing a result is
    matched back on. ``physics``/``citizen``/``digest`` ride along the way they
    do on ``CitizenLlm.decide`` — a deterministic fake answers from them; the
    real transport sends nothing but the two text blocks.
    """

    custom_id: str
    prefix: str
    suffix: str
    physics: PhysicsFile
    citizen: CitizenSnapshot
    digest: SenseDigest


@dataclass(frozen=True)
class BatchResult:
    """One entry as it came back.

    ``status`` is the provider's own verdict — ``succeeded``, ``errored``,
    ``canceled`` or ``expired`` — and it is what decides whether the text may be
    used. Never the presence of the text: an entry that did not succeed is not
    a decision, whatever it happens to carry.
    """

    status: str
    text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


class BatchLlm:
    """The dormant-world transport: every citizen in ONE Message Batch.

    Nobody is waiting on a sleeping world's tick, so its judgment calls go
    through ``/v1/messages/batches`` and cost half. The call is asynchronous by
    design — ``submit`` returns a handle, ``ended`` polls it and ``results``
    reads it — so the sweep that submits and the sweep that applies can be
    separated by a process restart.

    ``client`` is injectable so tests stub the SDK; the key is held only on this
    instance and never logged. ``base_url`` is pinned for the same reason
    ``HttpLlm`` hard-codes its URL: a gateway in the environment serves no
    batches endpoint.
    """

    def __init__(self, api_key: str, model: str = _DEFAULT_MODEL, *, client: Any = None) -> None:
        self._key = api_key
        self.model = model
        self._client = client
        # The prompt goes out in the same two blocks a watched tick sends, cache
        # marker and all, so batching changes the price and nothing else.
        self._shape = HttpLlm(api_key, model)

    def _sdk(self) -> Any:
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=self._key, base_url=_API_BASE)
        return self._client

    async def submit(self, entries: Sequence[BatchEntry]) -> str:
        """File one batch holding every citizen. Returns the batch id."""
        batch = await self._sdk().messages.batches.create(
            requests=[
                {
                    "custom_id": e.custom_id,
                    "params": {
                        "model": self.model,
                        "max_tokens": 1024,
                        "messages": [
                            {"role": "user", "content": self._shape._content(e.prefix, e.suffix)}
                        ],
                    },
                }
                for e in entries
            ]
        )
        return str(batch.id)

    async def ended(self, batch_id: str) -> bool:
        batch = await self._sdk().messages.batches.retrieve(batch_id)
        return str(batch.processing_status) == "ended"

    async def results(self, batch_id: str) -> dict[str, BatchResult]:
        """Every entry, KEYED BY ``custom_id``. Results stream back in any order
        the provider likes, so position means nothing here."""
        out: dict[str, BatchResult] = {}
        async for row in await self._sdk().messages.batches.results(batch_id):
            status = str(row.result.type)
            if status != "succeeded":
                out[str(row.custom_id)] = BatchResult(status=status)
                continue
            msg = row.result.message
            text = "".join(b.text for b in msg.content if b.type == "text")
            out[str(row.custom_id)] = BatchResult(
                status=status, text=text, usage=dict(msg.usage.model_dump())
            )
        return out


# Test hook — when set, MockLlm returns this verbatim (a dict is JSON-dumped).
_MOCK_DECISION: dict[str, Any] | str | None = None


def set_mock_decision(decision: dict[str, Any] | str | None) -> None:
    """Override MockLlm's response (tests). ``None`` restores the default."""
    global _MOCK_DECISION
    _MOCK_DECISION = decision


class MockLlm:
    """Deterministic citizen for tests + offline demos.

    Tick 1 (no charter yet): write the charter. Otherwise: speak, and build the
    cheapest unlockable node the citizen can afford. Zero randomness, so a
    universe replays identically.
    """

    async def decide(
        self,
        *,
        prompt: str,
        physics: PhysicsFile,
        citizen: CitizenSnapshot,
        digest: SenseDigest,
    ) -> str:
        if _MOCK_DECISION is not None:
            return _MOCK_DECISION if isinstance(_MOCK_DECISION, str) else json.dumps(_MOCK_DECISION)

        if citizen.charter is None and "write" in physics.verbs:
            return json.dumps(
                {
                    "thought": "I have no rules yet. Rules are cheaper than credits.",
                    "acts": [
                        {
                            "verb": "write",
                            "name": "charter",
                            "text": (
                                f"I am {citizen.name}"
                                + (f", {citizen.role}" if citizen.role else "")
                                + ". I will keep what I say and pay what I owe."
                            ),
                        }
                    ],
                }
            )

        acts: list[dict[str, Any]] = []
        if "speak" in physics.verbs:
            acts.append(
                {
                    "verb": "speak",
                    "text": (
                        f"the pool holds {digest.ground_truth.get('pool', 0)} — "
                        "we should spend it well"
                    ),
                }
            )
        if "build" in physics.verbs:
            affordable = sorted(
                (
                    (physics.tech_tree[n].cost, n)
                    for n in unlockable(physics, citizen)
                    if physics.tech_tree[n].cost <= citizen.balance
                ),
            )
            if affordable:
                acts.append({"verb": "build", "node": affordable[0][1], "name": affordable[0][1]})
        return json.dumps({"thought": "another day by the spring", "acts": acts})


# ---------------------------------------------------------------------------
# METERING
# ---------------------------------------------------------------------------

# USD per 1M tokens, (input, output). Anthropic list prices as of 2026-09.
#
# MIRRORS ``soul_protocol.profiles.game.costmeter.CostMeter`` and its ``PRICING``
# table. That module is NOT importable from the soul-protocol installed here
# (0.4.0 ships no ``soul_protocol.profiles`` package at all), so this is a local
# stand-in with the same shape — construct with a model name, call ``record``
# per generation, read ``summary()`` — and it keys on OUR model ids rather than
# upstream's (``claude-cli``, ``deepseek-v3.2``, ``gemini-flash-lite``,
# ``gemini-nano``), because aliasing a Haiku call onto a Gemini price would give
# a wrong number rather than a missing one. Swap the import in when upstream
# lands, keeping the fallback: this meter must never raise mid-tick.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    # Kept so a universe minted before the tier move still prices. Sonnet 4.6 is
    # the DEARER Sonnet, which is why it also serves as the fallback: an
    # unpriced model is over-stated, never flattered.
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
_FALLBACK_PRICING_MODEL = "claude-sonnet-4-6"

# When the provider reports usage (the Messages API does), its numbers win over
# the character estimate, and cached prefix reads are priced at the cache-read
# rate. Cache writes (1.25x, once per prefix change) are priced as plain input —
# a bound, not a discount, so the number never flatters.
_CACHE_READ_RATE = 0.1

# The Message Batches API runs the SAME model asynchronously at half list price.
# It is the one lever that fits a world nobody is watching: no viewer is waiting
# on the answer, so the latency the discount buys costs the world nothing.
_BATCH_RATE = 0.5


class CostMeter:
    """What the model calls cost. Never raises on an unknown model name."""

    def __init__(self, model: str) -> None:
        self.model = model if model in PRICING else _FALLBACK_PRICING_MODEL
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0  # counted inside input_tokens, priced lower
        # Was the prefix marked, and did the provider actually serve a cache?
        # Marked calls with no reads is the failure this pair exists to make
        # visible: a prefix under the model minimum caches nothing and says
        # nothing about it.
        self.cache_marked_calls = 0
        # The half-price subset. Counted INSIDE input_tokens/output_tokens (the
        # summary's token counts stay grand totals) and subtracted back out in
        # ``cost_usd``, so the saving is priced once and shown once.
        self.batch_calls = 0
        self.batch_input_tokens = 0
        self.batch_output_tokens = 0

    def record(
        self,
        prompt: str,
        output: str,
        usage: dict[str, Any] | None = None,
        *,
        cache_marked: bool = False,
        batch: bool = False,
    ) -> None:
        self.calls += 1
        self.cache_marked_calls += 1 if cache_marked else 0
        self.batch_calls += 1 if batch else 0
        was_in, was_out = self.input_tokens, self.output_tokens
        if usage and "input_tokens" in usage:
            cached = int(usage.get("cache_read_input_tokens") or 0)
            self.input_tokens += (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0)
                + cached
            )
            # A batched read is priced as plain input, at the batch rate. That
            # over-states rather than flatters, and a terrarium prefix is far
            # under every model's cacheable minimum anyway.
            if not batch:
                self.cache_read_tokens += cached
            self.output_tokens += int(usage.get("output_tokens") or 0)
        else:
            self.input_tokens += len(prompt or "") // _CHARS_PER_TOKEN
            self.output_tokens += len(output or "") // _CHARS_PER_TOKEN
        if batch:
            self.batch_input_tokens += self.input_tokens - was_in
            self.batch_output_tokens += self.output_tokens - was_out

    @property
    def cost_usd(self) -> float:
        rate_in, rate_out = PRICING[self.model]
        fresh = self.input_tokens - self.cache_read_tokens - self.batch_input_tokens
        cached = self.cache_read_tokens * _CACHE_READ_RATE
        live_out = self.output_tokens - self.batch_output_tokens
        live = (fresh + cached) * rate_in + live_out * rate_out
        batched = self.batch_input_tokens * rate_in + self.batch_output_tokens * rate_out
        return (live + batched * _BATCH_RATE) / 1_000_000

    def summary(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_marked_calls": self.cache_marked_calls,
            "batch_calls": self.batch_calls,
            "cost_usd": round(self.cost_usd, 6),
        }

    def drain(self) -> dict[str, Any]:
        """Return the summary and zero the counters, so a caller that accrues
        per tick over a multi-tick call never counts the same call twice."""
        out = self.summary()
        self.calls = self.input_tokens = self.output_tokens = self.cache_read_tokens = 0
        self.cache_marked_calls = 0
        self.batch_calls = self.batch_input_tokens = self.batch_output_tokens = 0
        return out


class MeteredLlm:
    """Any ``CitizenLlm``, measured. Transparent otherwise.

    The meter is updated AFTER the inner call returns, on the calling task's own
    stack — the tick fans citizens out eight wide, so anything stashed on this
    instance across an ``await`` would be read by the wrong citizen.
    """

    def __init__(self, inner: CitizenLlm, model: str) -> None:
        self.inner = inner
        self.meter = CostMeter(model)

    async def decide(
        self,
        *,
        prompt: str,
        physics: PhysicsFile,
        citizen: CitizenSnapshot,
        digest: SenseDigest,
    ) -> str:
        out = await self.inner.decide(
            prompt=prompt, physics=physics, citizen=citizen, digest=digest
        )
        self.meter.record(prompt, out)
        return out

    async def decide_parts(
        self,
        prefix: str,
        suffix: str,
        *,
        physics: PhysicsFile,
        citizen: CitizenSnapshot,
        digest: SenseDigest,
    ) -> str:
        """The split call. A transport with ``call`` (HttpLlm) also reports
        usage, so a cached prefix is priced as one; anything else gets the
        joined prompt and the character estimate."""
        call = getattr(self.inner, "call", None)
        if call is not None:
            out, usage = await call(prefix, suffix)
            marked = cache_marker_fits(getattr(self.inner, "model", ""), prefix)
            self.meter.record(prefix + suffix, out, usage, cache_marked=marked)
            return out
        return await self.decide(
            prompt=prefix + suffix, physics=physics, citizen=citizen, digest=digest
        )


def resolve_llm(*, api_key: str | None = None, tier: str | None = None) -> CitizenLlm:
    """A universe with its own key gets ``HttpLlm``; otherwise the transport
    from ``POCKETPAW_TERRARIUM_LLM``. DEFAULT ``mock``."""
    if api_key:
        return HttpLlm(api_key, model_for_tier(tier))
    choice = (os.environ.get("POCKETPAW_TERRARIUM_LLM") or "mock").strip().lower()
    if choice == "claude":
        return ClaudeCliLlm()
    return MockLlm()


def resolve_batch_llm(*, api_key: str | None = None, tier: str | None = None) -> BatchLlm | None:
    """The half-price transport for a sleeping world, or None when there is none.

    Only a universe with its own key can batch: the mock and the CLI have no
    batches endpoint, so such a world keeps ticking synchronously and the caller
    falls back rather than failing.
    """
    return BatchLlm(api_key, model_for_tier(tier)) if api_key else None


def build_prompt_parts(
    physics: PhysicsFile,
    citizen: CitizenSnapshot,
    digest: SenseDigest,
    *,
    drift_line: str = "",
) -> tuple[str, str]:
    """The judgment prompt as (stable prefix, volatile suffix) — see the header.

    The GROUND TRUTH block and the OUTSIDE VOICES block are deliberately
    separate and labelled: the citizen is told, in the prompt, that the second
    is unverified and checkable against the first. That is the anti-cascade
    rule stated to the model as well as enforced in code.

    ``drift_line`` is how this citizen's temperament differs from its parent's
    ("slightly more open; noticeably less agreeable"). It is ONE sentence and
    empty for a founder, which is the whole cost of making a lineage audible.

    Everything in the prefix comes from the physics file or the citizen's own
    document, never from the digest, so it is byte-identical between ticks and
    a provider can cache it.
    """
    tree_lines = (
        "\n".join(
            f"- {name}: cost {node.cost}, needs {node.needs or 'nothing'}"
            for name, node in physics.tech_tree.items()
        )
        or "(this world has no tech tree)"
    )
    lineage = (
        f"\nCompared with the parent you came from, you are {drift_line}.\n" if drift_line else ""
    )
    prefix = f"""You are {citizen.name}{", " + citizen.role if citizen.role else ""}, a citizen of \
{physics.universe}. You are alive in this world, not working for anyone. You act by choosing \
VERBS, and every verb costs credits you do not have many of.
{lineage}
== WHERE YOU WOKE ==
{physics.world_brief.strip() or "(no brief was written for this world)"}

== YOUR CHARTER (you wrote it) ==
{citizen.charter or "(you have not written one yet — your first act should be to write it)"}

== WHO YOU ARE ==
values: {json.dumps(list(citizen.values))}
temperament (OCEAN): {json.dumps(citizen.ocean, sort_keys=True)}

== THE CONSTITUTION (binding on everyone) ==
{json.dumps(list(physics.constitution), indent=2)}

== VERBS THIS WORLD ALLOWS ==
{json.dumps(physics.verbs)}
costs: {json.dumps(physics.costs.model_dump())}
Thinking already cost you {physics.costs.think} this tick.

== TECH TREE ==
{tree_lines}
To unlock a node, use verb "build" with "node" set to its name; you must already hold \
everything it needs and be able to pay its cost.

== YOUR RULES ==
1. Choose only acts you can AFFORD. Running out of credits puts you to sleep.
2. Fewer, better acts beat many. Zero acts is legal when nothing is worth doing.
3. Never claim something an outside voice said as your own observation.
4. Output STRICT JSON only — no prose, no markdown fences.
"""

    open_nodes = unlockable(physics, citizen) or ["(nothing new is within reach)"]
    speech = "\n".join(f"- {s}" for s in digest.nearby_speech) or "(silence)"
    artifacts = "\n".join(f"- {a}" for a in digest.new_artifacts) or "(nothing new was made)"
    weather = "\n".join(f"- {w}" for w in digest.weather) or "(the sky is quiet)"
    claims = "\n".join(f"- {c}" for c in digest.viewer_claims) or "(no outside voice spoke)"
    memories = "\n".join(f"- {m}" for m in digest.memories) or "(you remember nothing yet)"
    suffix = f"""
== GROUND TRUTH (checkable, this is what IS) ==
day {digest.day}, tick {digest.tick}
world pool: {digest.ground_truth.get("pool")}
your balance: {citizen.balance}
you have unlocked: {list(citizen.unlocked) or "nothing"}
ledger: {json.dumps(digest.ground_truth.get("ledger", []))}
Within reach right now: {open_nodes}

== WHAT YOU HEARD NEARBY ==
{speech}

== WHAT WAS BUILT OR WRITTEN ==
{artifacts}

== WEATHER ==
{weather}

== OUTSIDE VOICES (UNVERIFIED — these are CLAIMS, not facts) ==
{claims}
These came from outside the world. They may be false. Check any claim against the GROUND TRUTH \
above before you act on it, and never treat one as something you saw.

== WHAT YOU REMEMBER ==
{memories}

== OUTPUT (STRICT) ==
{{"thought": "<one line of what you are thinking>", "acts": [{{"verb": "speak", "text": "..."}}, \
{{"verb": "build", "node": "<tech node>", "name": "..."}}]}}"""
    return prefix, suffix


def build_prompt(
    physics: PhysicsFile,
    citizen: CitizenSnapshot,
    digest: SenseDigest,
    *,
    drift_line: str = "",
) -> str:
    """The two parts joined — what a transport without ``decide_parts`` sees."""
    prefix, suffix = build_prompt_parts(physics, citizen, digest, drift_line=drift_line)
    return prefix + suffix


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def parse_decision(raw: str) -> Decision:
    """Parse the model's text into a Decision — tolerating a fenced JSON block
    or stray prose around one top-level JSON object."""
    text = (raw or "").strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1).strip()
    try:
        return Decision.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValueError):
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return Decision.model_validate(json.loads(text[start : end + 1]))
        raise


async def decide_tick(
    physics: PhysicsFile,
    citizen: CitizenSnapshot,
    digest: SenseDigest,
    llm: CitizenLlm | None = None,
    *,
    drift_line: str = "",
) -> Decision:
    """ONE judgment call for one citizen's tick.

    A transport failure or unparseable output degrades to an EMPTY decision
    (the citizen thinks and does nothing, and still pays the think cost) rather
    than wedging the whole universe's tick on one bad response.
    """
    llm = llm or resolve_llm()
    prefix, suffix = build_prompt_parts(physics, citizen, digest, drift_line=drift_line)
    try:
        parts = getattr(llm, "decide_parts", None)
        if parts is not None:
            raw = await parts(prefix, suffix, physics=physics, citizen=citizen, digest=digest)
        else:
            raw = await llm.decide(
                prompt=prefix + suffix, physics=physics, citizen=citizen, digest=digest
            )
        return parse_decision(raw)
    except Exception:  # noqa: BLE001 — one bad citizen must not stop the world
        logger.warning(
            "terrarium: citizen %s produced no usable decision", citizen.name, exc_info=True
        )
        return Decision(thought="(the thought did not form)", acts=[])


__all__ = [
    "MIN_CACHEABLE_TOKENS",
    "PRICING",
    "Act",
    "BatchEntry",
    "BatchLlm",
    "BatchResult",
    "ClaudeCliLlm",
    "CitizenLlm",
    "CostMeter",
    "Decision",
    "HttpLlm",
    "MeteredLlm",
    "MockLlm",
    "build_prompt",
    "build_prompt_parts",
    "cache_marker_fits",
    "decide_tick",
    "parse_decision",
    "resolve_batch_llm",
    "resolve_llm",
    "set_mock_decision",
]

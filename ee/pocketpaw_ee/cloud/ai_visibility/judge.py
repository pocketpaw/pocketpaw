# AI visibility — judge: is the business named, which sources were used, and how
# does the answer talk about it?
#
# Created 2026-10-03 (feat/ai-visibility-core, AV-3).
#
# Presence is decided by plain string matching, never by a model: independent
# tests found decision models confidently wrong when the name is absent, and
# string matching beats them on presence. ``mentioned`` returns True (exact name,
# alias or the business's domain), False (clearly absent) or None for a fuzzy
# near miss ("Joe Pizza" for "Joe's Pizza"); only a None goes to the injectable
# ``confirm`` hook (``make_confirm`` builds one from a decision model).
#
# Position (recommended | listed | negative) and sentiment (0..4) come from a
# ``DecisionModel``: ``ClefFlash`` (Cloudflare Workers AI REST,
# ``@cf/cloudflare/clef-flash``, Jev-compatible ``{state, questions}`` request,
# $0.09 per 1M input tokens) with ``HaikuFallback`` (Claude Messages API, JSON
# reply) used when Clef's confidence is below ``CONFIDENCE_FLOOR`` or it fails.
# Clef answer shape (spike 2026-10-03): the Cloudflare model page documents the
# request but NOT the answer schema. We parse the Jev shape from a third-party Jev
# reference: noul ``{"noul": 0.96}``, choice ``{"choice", "confidence",
# "probabilities"}``, score ``{"score", "confidence", "probabilities"}``, and
# unwrap the Workers AI ``{"result": ..., "success": ...}`` envelope. TO VERIFY on
# the first live call. ``_to_judgement`` falls back to argmax over probabilities.
#
# Source types come from CONSULTED URLs where the engine exposes them: local AI
# answers lean on the business's own site and its listings (Yelp backs most
# ChatGPT business cards but is rarely visibly cited).

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterable
from difflib import SequenceMatcher
from typing import Any, Protocol
from urllib.parse import unquote_plus, urlsplit

import httpx

from pocketpaw_ee.cloud.ai_visibility.domain import Business, Competitor, MentionJudgement
from pocketpaw_ee.cloud.ai_visibility.engines import post_json, token_cost

#: Fuzzy ratio at or above which a non-exact window is a near miss (None).
NEAR_MISS_RATIO = 0.85
#: Below this decision-model confidence the fallback model judges instead.
CONFIDENCE_FLOOR = 0.7

POSITIONS = ("recommended", "listed", "negative")
SENTIMENT_SCALE = ["Very negative", "Negative", "Neutral", "Positive", "Very positive"]

#: Listing / directory hosts (suffix match). GBP, Yelp, TripAdvisor and Reddit
#: have their own types.
DIRECTORY_DOMAINS = (
    "yellowpages.com",
    "yell.com",
    "bbb.org",
    "foursquare.com",
    "angi.com",
    "homeadvisor.com",
    "thumbtack.com",
    "nextdoor.com",
    "opentable.com",
    "zomato.com",
    "justdial.com",
    "sulekha.com",
    "mapquest.com",
    "manta.com",
    "superpages.com",
    "chamberofcommerce.com",
    "hotfrog.com",
    "cylex.us.com",
    "maps.apple.com",
    "healthgrades.com",
    "zocdoc.com",
    "houzz.com",
    "facebook.com",
    "doordash.com",
    "ubereats.com",
    "grubhub.com",
)
SOURCE_TYPES = ("own_site", "gbp", "yelp", "tripadvisor", "reddit", "directory", "other")

_SUFFIXES = {"llc", "inc", "ltd", "co", "corp", "company", "pllc", "llp"}


def normalize_name(name: str) -> str:
    """Lowercase, accents and apostrophes dropped, ``&`` -> ``and``, punctuation to
    spaces, a leading "the" and trailing company suffixes (LLC, Inc, ...) removed."""
    tokens = _norm_text(name).split()
    if tokens[:1] == ["the"] and len(tokens) > 1:
        tokens = tokens[1:]
    while len(tokens) > 1 and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _norm_text(text: str) -> str:
    text = re.sub(r"['‘’`]", "", text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _names(name: str, aliases: Iterable[str]) -> list[str]:
    return [n for n in dict.fromkeys(normalize_name(x) for x in (name, *aliases)) if n]


def _bare_domain(domain: str | None) -> str:
    if not domain:
        return ""
    host = urlsplit(domain if "//" in domain else f"//{domain}").hostname or ""
    return host.lower().removeprefix("www.")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _host_is(host: str, domain: str) -> bool:
    return bool(domain) and (host == domain or host.endswith("." + domain))


def _has_name(norm_text: str, names: list[str]) -> bool:
    padded = f" {norm_text} "
    return any(f" {n} " in padded for n in names)


def mentioned(text: str, business: Business, cited_urls: Iterable[str] = ()) -> bool | None:
    """True when the answer names the business (name, alias, or its domain in the
    text or a cited URL); False when clearly absent; None for a fuzzy near miss."""
    norm = _norm_text(text)
    names = _names(business.name, business.aliases)
    if _has_name(norm, names):
        return True
    domain = _bare_domain(business.domain)
    if domain and (domain in text.lower() or any(_host_is(_host(u), domain) for u in cited_urls)):
        return True
    words = norm.split()
    for name in names:
        if len(name) < 5:  # too short to fuzz without noise
            continue
        k = len(name.split())
        for size in (k, k + 1):
            for i in range(len(words) - size + 1):
                window = " ".join(words[i : i + size])
                if SequenceMatcher(None, window, name).ratio() >= NEAR_MISS_RATIO:
                    return None
    return False


def competitors_mentioned(text: str, competitors: Iterable[Competitor]) -> list[str]:
    """Names of the competitors the answer names (exact name, alias or domain)."""
    norm = _norm_text(text)
    low = text.lower()
    hits = []
    for c in competitors:
        domain = _bare_domain(c.domain)
        if _has_name(norm, _names(c.name, c.aliases)) or (domain and domain in low):
            hits.append(c.name)
    return hits


def source_type(url: str, business_domain: str | None) -> str:
    """Classify a URL: own_site | gbp | yelp | tripadvisor | reddit | directory | other."""
    host = _host(url)
    path = urlsplit(url).path
    labels = host.split(".")
    if _host_is(host, _bare_domain(business_domain)):
        return "own_site"
    if (
        host in {"maps.google.com", "g.page", "maps.app.goo.gl", "business.google.com"}
        or ("google" in labels and path.startswith("/maps"))
        or (host == "goo.gl" and path.startswith("/maps"))
    ):
        return "gbp"
    if "yelp" in labels:
        return "yelp"
    if "tripadvisor" in labels:
        return "tripadvisor"
    if "reddit" in labels or host == "redd.it":
        return "reddit"
    if any(_host_is(host, d) for d in DIRECTORY_DOMAINS):
        return "directory"
    return "other"


def url_names_business(url: str, business: Business) -> bool:
    """Whether a listing URL is the business's own page (its name is in the path,
    e.g. ``yelp.com/biz/joes-pizza-austin``)."""
    path = _norm_text(unquote_plus(urlsplit(url).path))
    return _has_name(path, _names(business.name, business.aliases))


def passage_for(text: str, business: Business, limit: int = 2000) -> str:
    """The sentences that name the business, or the start of the answer."""
    names = _names(business.name, business.aliases)
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    hits = [s for s in sentences if _has_name(_norm_text(s), names)]
    return (" ".join(hits) or text)[:limit]


# --- decision models ---------------------------------------------------------


class DecisionModel(Protocol):
    name: str

    async def decide(self, state: str, questions: dict[str, Any]) -> tuple[dict[str, Any], float]:
        """Jev-shaped answers keyed by question id, and the call's cost in USD."""
        ...


def judge_questions(business: Business) -> dict[str, Any]:
    name = business.name
    return {
        "position": {
            "type": "choice",
            "instructions": f"How does this answer present {name}?",
            "criteria": {
                "recommended": f"{name} is recommended or ranked as a top pick",
                "listed": f"{name} is mentioned or listed without a recommendation",
                "negative": f"{name} is mentioned with a warning or complaint",
            },
        },
        "sentiment": {
            "type": "score",
            "instructions": f"How positive is this answer about {name}?",
            "criteria": SENTIMENT_SCALE,
        },
    }


def _argmax(probs: dict[str, Any]) -> tuple[str | None, float]:
    if not probs:
        return None, 0.0
    key = max(probs, key=lambda k: probs[k] or 0)
    return key, float(probs[key] or 0)


def _to_judgement(answers: dict[str, Any], judged_by: str, cost: float) -> MentionJudgement:
    pos = answers.get("position") or {}
    top, top_p = _argmax(pos.get("probabilities") or {})
    position = pos.get("choice") or top
    if position not in POSITIONS:
        raise ValueError(f"unexpected position {position!r}")
    pos_conf = float(pos.get("confidence", top_p))
    sent = answers.get("sentiment") or {}
    s_top, s_p = _argmax(sent.get("probabilities") or {})
    score = sent.get("score", s_top)
    if score is None:
        raise ValueError("no sentiment score")
    sentiment = min(4, max(0, round(float(score))))
    conf = min(pos_conf, float(sent.get("confidence", s_p if s_top is not None else 1.0)))
    return MentionJudgement(position, sentiment, round(conf, 4), judged_by, cost)


async def judge_mention(
    passage: str,
    business: Business,
    model: DecisionModel,
    fallback: DecisionModel | None = None,
) -> MentionJudgement:
    """Position + sentiment for a passage that names the business. The fallback
    judges when the primary fails or answers below ``CONFIDENCE_FLOOR``."""
    questions = judge_questions(business)
    spent = 0.0
    try:
        answers, spent = await model.decide(passage, questions)
        first = _to_judgement(answers, model.name, spent)
    except Exception:
        if fallback is None:
            raise
        first = None
    if first is not None and (first.confidence >= CONFIDENCE_FLOOR or fallback is None):
        return first
    assert fallback is not None
    answers, cost = await fallback.decide(passage, questions)
    return _to_judgement(answers, fallback.name, spent + cost)


def make_confirm(model: DecisionModel) -> Callable[[str, Business], Awaitable[bool]]:
    """A ``confirm`` hook for near misses: asks the model whether the passage names
    the business allowing for typos (yes at probability >= 0.5)."""

    async def confirm(passage: str, business: Business) -> bool:
        question = {
            "names_business": {
                "type": "noul",
                "instructions": (
                    f"Does this text name the business {business.name!r}, allowing for "
                    "small spelling differences? A different business with a similar "
                    "name is not a match."
                ),
            }
        }
        answers, _ = await model.decide(passage, question)
        return float((answers.get("names_business") or {}).get("noul", 0)) >= 0.5

    return confirm


class ClefFlash:
    """Cloudflare Clef-flash decision model over the Workers AI REST API."""

    name = "clef-flash"
    model_id = "@cf/cloudflare/clef-flash"

    def __init__(
        self,
        account_id: str,
        api_token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{self.model_id}"
        )
        self._token = api_token
        self._transport = transport

    async def decide(self, state: str, questions: dict[str, Any]) -> tuple[dict[str, Any], float]:
        data = await post_json(
            self._url,
            {"Authorization": f"Bearer {self._token}"},
            {"model": "clef-flash", "state": state, "questions": questions},
            secret=self._token,
            transport=self._transport,
            timeout=30.0,
        )
        result = data.get("result", data) if isinstance(data.get("result"), dict) else data
        usage = result.get("usage") or {}
        cost = token_cost(
            self.model_id, usage.get("input_tokens", usage.get("prompt_tokens", 0)), 0
        )
        return result.get("answers") or {}, cost


class HaikuFallback:
    """Claude (Haiku by default) answering the same questions as one JSON object."""

    name = "claude-haiku"
    url = "https://api.anthropic.com/v1/messages"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self.model = model
        self._transport = transport

    async def decide(self, state: str, questions: dict[str, Any]) -> tuple[dict[str, Any], float]:
        prompt = (
            "Answer each question about the TEXT. Reply with one JSON object only, keyed by "
            'question id. For a \'choice\' question give {"choice": <option>, "confidence": '
            "0..1}; for a 'score' question {\"score\": <0-based index into criteria>, "
            '"confidence": 0..1}; for a \'noul\' question {"noul": <probability of yes>}.\n\n'
            f"QUESTIONS: {json.dumps(questions)}\n\nTEXT:\n{state}"
        )
        data = await post_json(
            self.url,
            {"x-api-key": self._key, "anthropic-version": "2023-06-01"},
            {
                "model": self.model,
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
            secret=self._key,
            transport=self._transport,
            timeout=30.0,
        )
        text = "".join(
            b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text"
        )
        match = re.search(r"\{.*\}", text, re.DOTALL)
        answers = json.loads(match.group(0)) if match else {}
        usage = data.get("usage") or {}
        cost = token_cost(self.model, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        return answers, cost


def default_decision_models() -> tuple[DecisionModel | None, DecisionModel | None]:
    """``(ClefFlash, HaikuFallback)`` from Settings; either is None when unset."""
    from pocketpaw.config import get_settings

    settings = get_settings()

    def _get(name: str) -> str:
        return str(getattr(settings, name, None) or "").strip()

    account, token = _get("ai_visibility_cf_account_id"), _get("ai_visibility_cf_api_token")
    key = _get("ai_visibility_anthropic_api_key")
    return (
        ClefFlash(account, token) if account and token else None,
        HaikuFallback(key) if key else None,
    )


__all__ = [
    "CONFIDENCE_FLOOR",
    "DIRECTORY_DOMAINS",
    "SOURCE_TYPES",
    "ClefFlash",
    "DecisionModel",
    "HaikuFallback",
    "competitors_mentioned",
    "default_decision_models",
    "judge_mention",
    "judge_questions",
    "make_confirm",
    "mentioned",
    "normalize_name",
    "passage_for",
    "source_type",
    "url_names_business",
]

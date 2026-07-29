# tests/cloud/growth/test_researcher.py — the production research loop.
# Created 2026-07-29 (feat/growth-research-agent).
#
# These tests are about the PARSE and the TOOL SURFACE, not about the model.
# The agent run itself is the external boundary; what matters here is that
# whatever comes back, nothing unsafe reaches a prospect and nothing raises.
#
# The email assertions are the reason this file exists. ``recordable_emails``
# is the guard, but it can only refuse what the parse hands it — so the parse
# must never launder a guess into an observation.

from __future__ import annotations

import json

import pytest
from pocketpaw_ee.cloud.growth.discovery import ResearchRequest
from pocketpaw_ee.cloud.growth.domain import recordable_emails
from pocketpaw_ee.cloud.growth.researcher import (
    GROWTH_RESEARCHER_AGENT,
    GROWTH_RESEARCHER_TOOLS,
    build_research_prompt,
    parse_research_response,
)


def _response(companies: list[dict], notes: str = "") -> str:
    return json.dumps({"companies": companies, "notes": notes})


class TestTheToolSurfaceStaysShut:
    """The researcher must never hold a write tool.

    A researcher with ``growth_upsert_prospect`` could file a prospect
    directly, which routes around ``recordable_emails`` — the single function
    that decides whether an address is real enough to store. These pin the
    surface so widening it has to break a test first.
    """

    def test_only_read_tools(self) -> None:
        assert GROWTH_RESEARCHER_TOOLS == ("WebSearch", "WebFetch")

    def test_tool_mode_is_exclusive_not_additive(self) -> None:
        # "additive" would UNION these with the universal MCP grant, handing
        # the agent Write, Bash and the growth tools. The whole guarantee
        # rests on this one string.
        assert GROWTH_RESEARCHER_AGENT["config"]["tool_mode"] == "exclusive"

    def test_no_write_tool_anywhere_in_the_definition(self) -> None:
        tools = GROWTH_RESEARCHER_AGENT["config"]["tools"]
        for banned in ("Write", "Edit", "Bash", "growth_upsert_prospect"):
            assert banned not in tools


class TestAGuessNeverBecomesAnAddress:
    def test_observed_with_a_source_is_storable(self) -> None:
        result = parse_research_response(
            _response(
                [
                    {
                        "domain": "northwinddental.com",
                        "emails": [
                            {
                                "address": "hello@northwinddental.com",
                                "confidence": "observed",
                                "source_url": "https://northwinddental.com/contact",
                            }
                        ],
                    }
                ]
            ),
            max_results=10,
        )
        assert recordable_emails(result.companies[0].emails) == ("hello@northwinddental.com",)

    @pytest.mark.parametrize("confidence", ["guessed", "claimed", "inferred", "likely"])
    def test_anything_but_observed_is_dropped(self, confidence: str) -> None:
        """The model's own claim is carried verbatim and never upgraded."""
        result = parse_research_response(
            _response(
                [
                    {
                        "domain": "x.com",
                        "emails": [
                            {
                                "address": "priya@x.com",
                                "confidence": confidence,
                                "source_url": "https://x.com/team",
                            }
                        ],
                    }
                ]
            ),
            max_results=10,
        )
        assert recordable_emails(result.companies[0].emails) == ()

    def test_observed_without_a_source_url_is_refused_outright(self) -> None:
        """ "I saw it" without "here" is not evidence — refused at the parse,
        before ``recordable_emails`` is even consulted."""
        result = parse_research_response(
            _response(
                [
                    {
                        "domain": "x.com",
                        "emails": [{"address": "a@x.com", "confidence": "observed"}],
                    }
                ]
            ),
            max_results=10,
        )
        assert result.companies[0].emails == ()

    def test_a_missing_confidence_field_fails_closed(self) -> None:
        result = parse_research_response(
            _response(
                [
                    {
                        "domain": "x.com",
                        "emails": [{"address": "a@x.com", "source_url": "https://x.com"}],
                    }
                ]
            ),
            max_results=10,
        )
        assert recordable_emails(result.companies[0].emails) == ()


class TestTheParseDegrades:
    """Every malformed shape yields fewer findings — never an exception. One
    workspace's bad response must not end the sweep for the rest."""

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "I couldn't find anything useful, sorry!",
            "{ this is not json",
            '{"companies": "not a list"}',
            "[]",
        ],
    )
    def test_unreadable_responses_return_nothing(self, text: str) -> None:
        result = parse_research_response(text, max_results=5)
        assert result.companies == ()

    def test_a_fenced_block_is_still_read(self) -> None:
        text = 'Here you go:\n```json\n{"companies":[{"domain":"y.com"}]}\n```'
        result = parse_research_response(text, max_results=5)
        assert [c.domain for c in result.companies] == ["y.com"]

    def test_a_company_without_a_domain_is_dropped(self) -> None:
        """The domain is the dedupe identity — a company nobody can look up is
        not a lead."""
        result = parse_research_response(
            _response([{"company": "Nameless Ltd"}, {"domain": "real.com"}]),
            max_results=5,
        )
        assert [c.domain for c in result.companies] == ["real.com"]

    def test_zero_findings_is_a_legitimate_result(self) -> None:
        """Distinct from a failure: the notes survive, so the caller can tell
        'searched and found nobody' from 'could not read the response'."""
        result = parse_research_response(
            _response([], notes="three directories were paywalled"), max_results=5
        )
        assert result.companies == ()
        assert result.notes == "three directories were paywalled"

    def test_the_cap_is_enforced_on_our_side(self) -> None:
        """``max_results`` is a request to the model, not a promise it kept."""
        result = parse_research_response(
            _response([{"domain": f"c{i}.com"} for i in range(20)]), max_results=3
        )
        assert len(result.companies) == 3

    def test_the_domain_is_normalised(self) -> None:
        result = parse_research_response(
            _response([{"domain": "  MixedCase.COM  "}]), max_results=5
        )
        assert result.companies[0].domain == "mixedcase.com"


class TestThePrompt:
    def test_it_carries_the_run_specifics(self) -> None:
        prompt = build_research_prompt(
            ResearchRequest(
                workspace_id="w1",
                icp_id="i1",
                criteria="web design shops in Pune",
                geography="Pune, Maharashtra",
                exclusions="anyone we already emailed",
                max_results=7,
            )
        )
        assert "web design shops in Pune" in prompt
        assert "Pune, Maharashtra" in prompt
        assert "anyone we already emailed" in prompt
        assert "7" in prompt

    def test_optional_fields_are_omitted_when_empty(self) -> None:
        prompt = build_research_prompt(
            ResearchRequest(workspace_id="w1", icp_id="i1", criteria="dentists")
        )
        assert "Where:" not in prompt
        assert "Skip:" not in prompt

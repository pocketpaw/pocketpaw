# tests/cloud/growth/social/test_pulse.py — Growth › Social Pulse plumbing with no network and no
# model: source cleaning, the provider-response mappers, comment ranking, evidence gathering
# through a fake provider, and the themes parser (drops themes with no why or no evidence).

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.growth.social.pulse import (
    Evidence,
    PulseSource,
    clean_source,
    gather_evidence,
    parse_sources,
    parse_themes,
    reddit_evidence,
    top_comments,
    x_evidence,
)


def test_clean_source_validates_subreddits_and_searches():
    sub = clean_source("reddit", "subreddit", "r/smallbusiness")
    assert sub == PulseSource("reddit", "subreddit", "smallbusiness")
    assert clean_source("reddit", "subreddit", "not a sub!") is None
    q = "invoice reminders"
    assert clean_source("x", "search", q) == PulseSource("x", "search", q)
    assert clean_source("x", "subreddit", "foo") is None


def test_mappers_keep_public_text_and_links_only():
    post = {
        "id": "a1",
        "title": "Chasing invoices",
        "selftext": "ugh",
        "subreddit": "freelance",
        "score": 120,
        "permalink": "/r/freelance/comments/a1/x/",
        "author": "someone",
    }
    ev = reddit_evidence(post, "r/freelance")
    assert ev and ev.url == "https://www.reddit.com/r/freelance/comments/a1/x/"
    assert ev.title == "r/freelance: Chasing invoices"
    assert "someone" not in str(ev.as_dict())
    legacy = {
        "full_text": "late payments again this month",
        "favorite_count": 10,
        "retweet_count": 2,
        "reply_count": 1,
    }
    xe = x_evidence({"rest_id": "9", "url": "https://x.com/u/status/9", "legacy": legacy}, "X: q")
    assert xe and xe.score == 15 and xe.platform == "x"
    assert x_evidence({"url": "javascript:1", "legacy": {"full_text": "x"}}, "s") is None
    link_only = {"url": "https://x.com/a/status/3", "legacy": {"full_text": "https://t.co/abc"}}
    assert x_evidence(link_only, "s") is None


def test_top_comments_ranks_top_level_by_score():
    comments = [
        {"depth": 0, "body": "low", "score": 1},
        {"depth": 1, "body": "reply", "score": 99},
        {"depth": 0, "body": "high", "score": 50},
    ]
    assert top_comments(comments) == ["high", "low"]


@pytest.mark.asyncio
async def test_gather_evidence_reads_subreddits_with_comments_and_x():
    class FakeProvider:
        async def subreddit_top(self, sub):
            post = {"id": "1", "title": "T", "score": 5, "permalink": "/r/s/comments/1/t/"}
            return [{**post, "subreddit": sub}]

        async def reddit_search(self, q):
            return []

        async def reddit_comments(self, url):
            return [{"depth": 0, "body": "same here", "score": 3}]

        async def x_search(self, q):
            url = "https://x.com/a/status/2"
            return [
                {"rest_id": "2", "url": url, "legacy": {"full_text": "this is a real tweet text"}}
            ]

    sources = (PulseSource("reddit", "subreddit", "s"), PulseSource("x", "search", "q"))
    found = await gather_evidence(FakeProvider(), sources)
    assert [e.platform for e in found] == ["reddit", "x"]
    assert found[0].comments == ["same here"]


def test_parsers_tolerate_junk_and_drop_unsupported_themes():
    picked = parse_sources('{"subreddits": ["r/ok_sub", "bad sub"], "x_searches": ["q one"]}')
    assert picked == (
        PulseSource("reddit", "subreddit", "ok_sub"),
        PulseSource("x", "search", "q one"),
    )
    ev = [Evidence("r_1", "reddit", "https://www.reddit.com/r/a/1", "t", "x", 5, "r/a")]
    text = (
        '{"themes": [{"title": "Pain", "why": "They ask for it", "sentiment": "angry", '
        '"phrases": ["so slow"], "evidence": ["r_1", "nope"], "suggestions": '
        '[{"platform": "reddit", "hook": "How we fixed it", "subreddit": "r/smallbiz"}]}, '
        '{"title": "No why", "evidence": ["r_1"]}, '
        '{"title": "No evidence", "why": "w", "evidence": []}]}'
    )
    themes = parse_themes(text, ev)
    assert [t["title"] for t in themes] == ["Pain"]
    assert themes[0]["sentiment"] == "mixed"
    assert themes[0]["evidence"][0]["id"] == "r_1"
    assert themes[0]["suggestions"][0]["subreddit"] == "smallbiz"
    assert parse_themes("nothing", ev) == ()

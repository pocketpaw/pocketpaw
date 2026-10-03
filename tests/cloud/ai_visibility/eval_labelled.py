# tests/cloud/ai_visibility/eval_labelled.py — accuracy of ``mentioned`` and
# ``source_type`` on the hand-labelled set in fixtures/labelled/.
#
# Created 2026-10-03 (feat/ai-visibility-core, AV-3). Not collected by pytest (no
# ``test_`` prefix); ``test_eval_labelled.py`` pins a floor. Run it directly:
#
#   uv run --group ee --group dev python tests/cloud/ai_visibility/eval_labelled.py
#
# ``answers.jsonl`` rows: {id, business: {name, aliases?, domain?}, text,
# mentioned: true | false | null (null = a near miss that should go to the LLM
# confirm hook)}. ``sources.jsonl`` rows: {url, business_domain, type}. Add real
# answers from live runs here as they come in; misses are printed by id.
from __future__ import annotations

import json
from pathlib import Path

from pocketpaw_ee.cloud.ai_visibility.domain import Business
from pocketpaw_ee.cloud.ai_visibility.judge import mentioned, source_type

LABELLED = Path(__file__).parent / "fixtures" / "labelled"


def _rows(name: str) -> list[dict]:
    return [json.loads(line) for line in (LABELLED / name).read_text().splitlines() if line]


def evaluate() -> dict:
    misses: list[str] = []
    answers = _rows("answers.jsonl")
    hit = 0
    for row in answers:
        b = row["business"]
        biz = Business(
            name=b["name"],
            business_type="",
            aliases=tuple(b.get("aliases", ())),
            domain=b.get("domain"),
        )
        got = mentioned(row["text"], biz)
        if got == row["mentioned"]:
            hit += 1
        else:
            misses.append(f"{row['id']}: expected {row['mentioned']}, got {got}")
    sources = _rows("sources.jsonl")
    s_hit = 0
    for row in sources:
        got = source_type(row["url"], row["business_domain"])
        if got == row["type"]:
            s_hit += 1
        else:
            misses.append(f"{row['url']}: expected {row['type']}, got {got}")
    return {
        "mentioned_accuracy": hit / len(answers),
        "mentioned_n": len(answers),
        "source_type_accuracy": s_hit / len(sources),
        "source_type_n": len(sources),
        "misses": misses,
    }


if __name__ == "__main__":
    report = evaluate()
    print(
        f"mentioned: {report['mentioned_accuracy']:.0%} of {report['mentioned_n']}  "
        f"source_type: {report['source_type_accuracy']:.0%} of {report['source_type_n']}"
    )
    for miss in report["misses"]:
        print("  miss", miss)

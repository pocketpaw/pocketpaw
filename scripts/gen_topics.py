"""Generate paw-enterprise/src/lib/core/shared/topics.gen.ts from EVENT_REGISTRY.

Run via:  uv run python scripts/gen_topics.py
"""

from pathlib import Path

from pocketpaw_ee.cloud._core.realtime.events import EVENT_REGISTRY

OUT = Path(__file__).resolve().parents[2] / "paw-enterprise/src/lib/core/shared/topics.gen.ts"

HEADER = """// GENERATED -- do not edit. Run `uv run python backend/scripts/gen_topics.py`.
// Mirrors backend EVENT_REGISTRY keys.
"""


def main() -> None:
    topics = sorted(EVENT_REGISTRY.keys())
    lines = [HEADER, "export const TOPICS = ["]
    for t in topics:
        lines.append(f"  {t!r},")
    lines.append("] as const;")
    lines.append("")
    lines.append("export type Topic = (typeof TOPICS)[number];")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(topics)} topics -> {OUT}")


if __name__ == "__main__":
    main()

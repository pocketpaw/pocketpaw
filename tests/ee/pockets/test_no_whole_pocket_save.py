# tests/ee/pockets/test_no_whole_pocket_save.py: no code saves a whole Pocket
# document.
#
# Beanie's ``save()`` re-sends every field the caller read, so a rename or a share
# change saved after an agent edit put the stale ``source`` / ``rippleSpec`` back and
# the edit vanished. Pocket writes go through ``_save_pocket`` (changed fields only,
# content fields compare-and-set) or ``_commit_fields``. This scan fails when a plain
# ``.save()`` / ``.replace()`` appears on a Pocket document again.
from __future__ import annotations

import re
from pathlib import Path

EE = Path(__file__).resolve().parents[3] / "ee" / "pocketpaw_ee"

#: The pockets service is the Pocket write owner: every ``.save()`` there must be
#: on one of these receivers, which hold documents that are NOT a Pocket (builtin
#: widget rows, chat sessions, backend credentials; ``stand_in`` is the test-double
#: branch of ``_save_pocket``).
SERVICE = "cloud/pockets/service.py"
SERVICE_ALLOWED = {"existing", "stand_in"}

_SAVE = re.compile(r"\b(\w+)\.(save|replace)\(\s*\)")
#: Elsewhere, a name bound from a Pocket read (``x = await Pocket.get(...)``,
#: ``doc, err = await _agent_load_doc(...)``, ``for p in <Pocket.find list>``).
_LOADER = r"(?:_?Pocket(?:Doc)?\.(?:get|find_one|find)|_fetch_pocket|_agent_load_doc)\("
_BOUND = re.compile(r"(\w+)(?:\s*,\s*\w+)?\s*=\s*await\s+" + _LOADER)
_LOOP = re.compile(r"for\s+(\w+)\s+in\s+(\w+)")


def _pocket_names(text: str) -> set[str]:
    names = set(_BOUND.findall(text))
    for var, seq in _LOOP.findall(text):
        if seq in names:
            names.add(var)
    return names


def _code_lines(text: str):
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or "``" in stripped:
            continue
        yield lineno, stripped


def test_no_plain_save_on_a_pocket_document() -> None:
    offenders: list[str] = []
    for path in EE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(EE).as_posix()
        if rel == SERVICE:
            allowed = lambda name: name in SERVICE_ALLOWED  # noqa: E731
        elif "Pocket" in text:
            names = _pocket_names(text)
            allowed = lambda name, names=names: name not in names  # noqa: E731
        else:
            continue
        for lineno, line in _code_lines(text):
            for match in _SAVE.finditer(line):
                if not allowed(match.group(1)):
                    offenders.append(f"{rel}:{lineno}: {line}")
    assert not offenders, (
        "Save Pocket documents through pockets_service._save_pocket (or "
        "_commit_fields for a source write), never a whole-document save():\n"
        + "\n".join(offenders)
    )

# CLI skills command - list and search available skills.

from __future__ import annotations

from pocketpaw.cli.utils import BOLD, DIM, GREEN, RESET, output_json, print_header


def run_skills_cmd(search: str | None = None, as_json: bool = False) -> int:
    """List or search available skills. Returns 0."""
    from pocketpaw.skills import get_skill_loader

    loader = get_skill_loader()
    if search:
        skills = loader.search(search)
    else:
        skills_dict = loader.load()
        skills = list(skills_dict.values())

    if as_json:
        output_json(
            [
                {
                    "name": s.name,
                    "description": s.description,
                    "path": str(s.path),
                    "user_invocable": s.user_invocable,
                }
                for s in skills
            ]
        )
        return 0

    print_header("Skills", f"{len(skills)} skill(s) loaded")

    if not skills:
        print(f"  {DIM}No skills found.{RESET}")
        print(f"  {DIM}Create one with the create_skill tool or place a SKILL.md in:{RESET}")
        for p in loader.paths:
            print(f"    {DIM}{p}{RESET}")
        print()
        return 0

    for s in skills:
        invocable = f" {GREEN}(invocable){RESET}" if s.user_invocable else ""
        print(f"  {BOLD}{s.name}{RESET}{invocable}")
        print(f"    {s.description}")
        print(f"    {DIM}{s.path}{RESET}")
        print()

    return 0

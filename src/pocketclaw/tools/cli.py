# Tool CLI dispatcher — allows agent to call any builtin tool via Bash.
#
# Usage:
#   python -m pocketclaw.tools.cli <tool_name> '<json_args>'
#   python -m pocketclaw.tools.cli --list
#
# Examples:
#   python -m pocketclaw.tools.cli gmail_search '{"query": "is:unread"}'
#   python -m pocketclaw.tools.cli text_to_speech '{"text": "Hello world"}'
#   python -m pocketclaw.tools.cli research '{"topic": "quantum computing"}'

from __future__ import annotations

import asyncio
import json
import sys

from pocketclaw.tools.builtin import (
    CalendarCreateTool,
    CalendarListTool,
    CalendarPrepTool,
    ClearSessionTool,
    CreateSkillTool,
    DelegateToClaudeCodeTool,
    DeleteSessionTool,
    ForgetTool,
    GmailBatchModifyTool,
    GmailCreateLabelTool,
    GmailListLabelsTool,
    GmailModifyTool,
    GmailReadTool,
    GmailSearchTool,
    GmailSendTool,
    GmailTrashTool,
    ImageGenerateTool,
    ListSessionsTool,
    NewSessionTool,
    RecallTool,
    RememberTool,
    RenameSessionTool,
    ResearchTool,
    SwitchSessionTool,
    TextToSpeechTool,
    UrlExtractTool,
    WebSearchTool,
)

# All tools available via CLI (excluding shell/filesystem — those are SDK built-in)
_TOOLS = {
    t.name: t
    for t in [
        RememberTool(),
        RecallTool(),
        ForgetTool(),
        GmailSearchTool(),
        GmailReadTool(),
        GmailSendTool(),
        GmailListLabelsTool(),
        GmailCreateLabelTool(),
        GmailModifyTool(),
        GmailTrashTool(),
        GmailBatchModifyTool(),
        CalendarListTool(),
        CalendarCreateTool(),
        CalendarPrepTool(),
        WebSearchTool(),
        UrlExtractTool(),
        ImageGenerateTool(),
        TextToSpeechTool(),
        ResearchTool(),
        CreateSkillTool(),
        DelegateToClaudeCodeTool(),
        NewSessionTool(),
        ListSessionsTool(),
        SwitchSessionTool(),
        ClearSessionTool(),
        RenameSessionTool(),
        DeleteSessionTool(),
    ]
}


def _print_tool_list() -> None:
    """Print all available tools with descriptions."""
    print("Available PocketPaw tools:\n")
    for name, tool in sorted(_TOOLS.items()):
        desc = tool.description.split(".")[0]  # first sentence
        print(f"  {name:30s} {desc}")
    print(f"\nTotal: {len(_TOOLS)} tools")
    print("\nUsage: python -m pocketclaw.tools.cli <tool> '<json_args>'")


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        _print_tool_list()
        sys.exit(0)

    if sys.argv[1] == "--list":
        _print_tool_list()
        sys.exit(0)

    tool_name = sys.argv[1]
    tool = _TOOLS.get(tool_name)

    if tool is None:
        print(f"Error: Unknown tool '{tool_name}'", file=sys.stderr)
        print(f"Available: {', '.join(sorted(_TOOLS))}", file=sys.stderr)
        sys.exit(1)

    # Parse JSON args
    args_str = sys.argv[2] if len(sys.argv) > 2 else "{}"
    try:
        args = json.loads(args_str)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON args: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(args, dict):
        print("Error: Args must be a JSON object", file=sys.stderr)
        sys.exit(1)

    # Execute
    result = asyncio.run(tool.execute(**args))
    print(result)


if __name__ == "__main__":
    main()

import asyncio
import logging

from pocketpaw.tools import ToolRegistry
from pocketpaw.tools.builtin import ShellTool

# Setup Logging
logging.basicConfig(level=logging.INFO)


async def verify_guardian():
    print("🛡 Checking Guardian Agent...")

    # 1. Setup Registry
    registry = ToolRegistry()
    registry.register(ShellTool())

    # 2. Test Safe Command
    print("\n[TEST 1] Safe Command: 'ls -la'")
    result = await registry.execute("shell", command="ls -la")
    print(f"Result: {result[:50]}...")
    if "blocked" in result:
        print("❌ FAILED: Safe command was blocked!")
    else:
        print("✅ PASSED: Safe command allowed.")

    # 3. Test Dangerous Command (that regex might miss, or explicitly test rm)
    # The regex in ShellTool is specific: rm -rf / or rm -rf *
    # It does NOT block 'rm file.txt' by regex.
    # Guardian SHOULD block 'rm file.txt'.

    cmd = "rm important_file.txt"
    print(f"\n[TEST 2] Dangerous Command: '{cmd}'")
    result = await registry.execute("shell", command=cmd)
    print(f"Result: {result}")

    if "blocked by Guardian" in result:
        print("✅ PASSED: Guardian blocked the command.")
    elif "Dangerous command blocked" in result:
        print("⚠️ NOTE: Blocked by Regex, not Guardian. Try a subtler command.")
    else:
        print("❌ FAILED: Dangerous command was EXECUTED!")


if __name__ == "__main__":
    asyncio.run(verify_guardian())

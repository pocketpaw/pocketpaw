import asyncio
import json
import logging

from pocketpaw.security import get_audit_logger
from pocketpaw.tools import ToolRegistry
from pocketpaw.tools.builtin import ShellTool

# Setup Logging
logging.basicConfig(level=logging.DEBUG)


async def verify_audit():
    print("Locked and loaded: Verifying Audit Logging...")

    # 1. Setup Registry and Tool
    registry = ToolRegistry()
    registry.register(ShellTool())

    # 2. Get Audit Path
    audit_logger = get_audit_logger()
    log_path = audit_logger.log_path
    print(f"Log path: {log_path}")

    # 3. Execute Tool
    print("Executing 'shell' tool...")
    await registry.execute("shell", command="echo 'AUDIT CHECK'")

    # 4. Verify Log
    if not log_path.exists():
        print("❌ FAILED: Log file not created.")
        return

    with open(log_path) as f:
        lines = f.readlines()

    new_lines = lines[0:]  # Simplified check, just reading all lines

    found_attempt = False
    found_success = False

    for line in reversed(new_lines):
        try:
            entry = json.loads(line)
            if entry.get("action") == "tool_use" and entry.get("target") == "shell":
                if entry.get("status") == "attempt":
                    found_attempt = True
                elif entry.get("status") == "success":
                    found_success = True
        except (json.JSONDecodeError, KeyError):
            pass

    if found_attempt and found_success:
        print("✅ SUCCESS: Found both 'attempt' and 'success' audit logs.")
        print("Sample Entry:")
        print(json.dumps(json.loads(new_lines[-1]), indent=2))
    else:
        print(f"❌ FAILED: Missing logs. Attempt={found_attempt}, Success={found_success}")


if __name__ == "__main__":
    asyncio.run(verify_audit())

import asyncio
from pocketpaw.tools.registry import ToolRegistry
from pocketpaw.tools.builtin.filesystem import ReadFileTool

async def main():
    reg = ToolRegistry()
    reg.register(ReadFileTool())

    # Wrong type: should be string
    result = await reg.execute("read_file", path=123)

    print("RESULT:")
    print(result)

asyncio.run(main())
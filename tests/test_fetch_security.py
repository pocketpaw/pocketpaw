from pathlib import Path

import pytest

from pocketpaw.tools.fetch import handle_path


@pytest.mark.asyncio
async def test_empty_path_rejection():
    # This confirms the Pydantic shield catches empty strings
    jail = Path.home()
    result = await handle_path("", jail)
    assert result.get("type") == "error"
    assert "Validation Error" in result.get("message")


@pytest.mark.asyncio
async def test_path_traversal_denied():
    # This confirms the jail is respected
    result = await handle_path("../../", "/tmp")
    assert result.get("type") == "error"

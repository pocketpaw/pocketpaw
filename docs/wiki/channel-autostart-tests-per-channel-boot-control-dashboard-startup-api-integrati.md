---
{
  "title": "Channel Autostart Tests: Per-Channel Boot Control, Dashboard Startup, API Integration, and Config Persistence",
  "summary": "This test suite validates PocketPaw's per-channel autostart feature, which lets operators control which channel adapters automatically start when the dashboard boots. Tests cover the `_channel_autostart_enabled()` helper, startup behavior with mixed enabled/disabled channels, the REST API endpoints for reading and saving autostart config, and round-trip persistence to disk.",
  "concepts": [
    "channel_autostart",
    "startup_event",
    "Settings",
    "_channel_autostart_enabled",
    "get_channels_status",
    "save_channel_config",
    "dashboard",
    "config persistence",
    "round-trip",
    "channel adapters"
  ],
  "categories": [
    "channel management",
    "testing",
    "dashboard",
    "configuration",
    "test"
  ],
  "source_docs": [
    "dfe0df1f0def817d"
  ],
  "backlinks": null,
  "word_count": 366,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When PocketPaw's dashboard starts, it can automatically initialize channel adapters — but some operators may want certain channels (e.g., a development Telegram bot) to remain dormant by default. The `channel_autostart` config dictionary in `Settings` controls this: a missing key defaults to `True` (start the channel), while an explicit `False` prevents startup.

## `_channel_autostart_enabled()` — Default Behavior

```python
def test_default_returns_true_for_unconfigured():
    settings = Settings()  # empty channel_autostart
    assert _autostart_enabled("telegram", settings) is True

def test_respects_explicit_false():
    settings = Settings(channel_autostart={"telegram": False})
    assert _autostart_enabled("telegram", settings) is False
```

The default-to-True behavior is deliberate: existing deployments that didn't have `channel_autostart` in their config files should not be disrupted by the feature's introduction. Channels only stay off if explicitly disabled — a safe opt-in design.

## Dashboard Startup Integration

```python
async def test_startup_skips_disabled_channels():
    settings = Settings(channel_autostart={"telegram": False, "discord": True})
    # startup_event should not call telegram's start
    # but should call discord's start
```

These tests mock the channel startup functions and verify that `startup_event` correctly consults the settings before deciding whether to start each channel. The risk being guarded against is that a change to startup logic accidentally ignores the autostart config and starts all channels regardless.

## REST API: Status and Save Endpoints

```python
async def test_api_status_includes_autostart():
    response = await get_channels_status()
    # Each channel entry includes its autostart setting

async def test_api_save_persists_autostart():
    await save_channel_config("telegram", {"autostart": False})
    # settings.channel_autostart["telegram"] is now False
```

The channels status API exposes autostart configuration to the dashboard UI, allowing operators to see and modify channel settings without editing config files directly. The save endpoint allows the dashboard UI to toggle autostart per channel.

## Round-Trip Persistence

```python
def test_round_trip_save_load(tmp_path):
    # Save autostart config, load it back, verify same values
```

This is the end-to-end test: the autostart setting must survive a complete save-to-disk and load-from-disk cycle. A failure here would mean operators lose their autostart configuration after a PocketPaw restart. The test uses `tmp_path` so it doesn't pollute the real config file.

## Known Gaps

No test covers the race condition where a channel is being stopped while `startup_event` is trying to start it. The `test_api_save_persists_autostart` test verifies in-memory persistence but does not explicitly verify that the change is written to the config file on disk.
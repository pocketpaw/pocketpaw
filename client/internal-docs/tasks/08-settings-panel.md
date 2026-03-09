# Task 08: Settings Panel

> Settings UI: AI model, channels, security, about. Minimal tabs, not a sprawling form.

## Goal

Build the settings page with tabbed sections matching the UX spec.

## Depends On

- **Task 03** (Shell Layout): routing (`/settings`), shadcn components
- **Task 02** (Stores): `settingsStore`, `connectionStore`

## Install Additional shadcn Components

```bash
bunx shadcn-svelte@latest add switch
bunx shadcn-svelte@latest add alert
```

## Files to Create / Modify

```
src/routes/settings/
└── +page.svelte                    # Settings page

src/lib/components/settings/
├── SettingsPanel.svelte            # Tab container
├── TabAIModel.svelte               # AI provider + model selection
├── TabChannels.svelte              # Channel connections (Telegram, Discord, etc.)
├── TabSecurity.svelte              # Guardian AI, audit log, permissions
├── TabAbout.svelte                 # Version, update check, links
└── ChannelConfigCard.svelte        # Single channel config card
```

## Layout

```
┌──────────────────────────────────────────────────────────┐
│  ← Back                                                  │
│                                                          │
│  Settings                                                │
│                                                          │
│  [ AI Model ]  [ Channels ]  [ Security ]  [ About ]    │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │  (Active tab content)                              │  │
│  │                                                    │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

## Tab: AI Model

```
┌────────────────────────────────────────────────────┐
│  AI Model                                          │
│                                                    │
│  Backend:  [ Claude Agent SDK ▾ ]                  │
│                                                    │
│  Provider: [ Anthropic ▾ ]                         │
│                                                    │
│  API Key:  [ sk-ant-••••••••••oR3 ]  [Change]     │
│            ✓ Encrypted and stored locally           │
│                                                    │
│  Model:    [ Claude Sonnet 4.5 ▾ ]                 │
│                                                    │
│  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─  │
│                                                    │
│  Or use a free local model:                        │
│  [ Switch to Ollama (free, offline) → ]            │
│                                                    │
│  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─  │
│                                                    │
│  Advanced                                          │
│  Smart Routing: [ ON ]                             │
│  Max Turns:     [ 25 ]                             │
│                                                    │
│               [ Save ]                             │
└────────────────────────────────────────────────────┘
```

Fields:
- **Backend**: dropdown populated from `GET /backends` (show only available ones)
- **Provider**: depends on backend (Anthropic, OpenAI, Ollama, Google, etc.)
- **API Key**: masked input with change button. Save via `settingsStore.saveApiKey()`
- **Model**: depends on provider (show known models as dropdown)
- **Smart Routing**: toggle (routes simple queries to cheaper models)
- **Max Turns**: number input

## Tab: Channels

```
┌────────────────────────────────────────────────────┐
│  Channels                                          │
│                                                    │
│  ┌──────────────────────────────────────────────┐  │
│  │  📱 Telegram           [ Running ✓ ]  [⚙]   │  │
│  │  Bot token configured                        │  │
│  └──────────────────────────────────────────────┘  │
│                                                    │
│  ┌──────────────────────────────────────────────┐  │
│  │  💬 Discord            [ Stopped ]    [⚙]   │  │
│  │  Not configured                              │  │
│  └──────────────────────────────────────────────┘  │
│                                                    │
│  ┌──────────────────────────────────────────────┐  │
│  │  💼 Slack              [ Stopped ]    [⚙]   │  │
│  │  Not configured                              │  │
│  └──────────────────────────────────────────────┘  │
│                                                    │
│  ┌──────────────────────────────────────────────┐  │
│  │  📞 WhatsApp           [ Stopped ]    [⚙]   │  │
│  │  Not configured                              │  │
│  └──────────────────────────────────────────────┘  │
│                                                    │
└────────────────────────────────────────────────────┘
```

Each `ChannelConfigCard`:
- Channel name + icon
- Status badge: Running (green) / Stopped (gray) / Error (red)
- Gear icon opens config dialog with token/ID fields
- Toggle to start/stop the channel
- Autostart checkbox

## Tab: Security

- Guardian AI status: enabled/disabled toggle
- Plan Mode: toggle
- Tool policy summary
- Link to view audit log (opens in a scrollable panel)
- Recent audit entries (last 10)

## Tab: About

- PocketPaw version (from `GET /version`)
- Update available notification
- Links: Documentation, GitHub, Discord
- System info: backend, model, memory backend

## Acceptance Criteria

- [ ] Settings page loads current settings from API
- [ ] AI Model tab: backend/provider/model selection works
- [ ] API key can be changed (masked, encrypted)
- [ ] Channels tab shows status of all channels
- [ ] Channel config dialog saves and toggles channels
- [ ] Security tab shows Guardian AI and audit status
- [ ] About tab shows version info
- [ ] Save button persists changes via `settingsStore.update()`
- [ ] Back button returns to chat
- [ ] Tabs switch content without page reload

## Notes

- Settings should load current values on mount, not show empty fields.
- Dropdowns for backend/provider/model should show only valid combinations.
- Don't expose internal config paths or technical details to non-tech users.
- The API key field should never show the full key — always mask it.

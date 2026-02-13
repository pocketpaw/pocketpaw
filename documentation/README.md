# PocketPaw Documentation

Technical documentation for PocketPaw - your personal AI agent.

## Structure

```
documentation/
├── features/       # Feature documentation
├── architecture/   # System design and patterns (planned)
├── api/           # API references (planned)
└── guides/        # How-to guides (planned)
```

## Features

- [Channel Adapters](features/channels.md) - Discord, Slack, WhatsApp (Personal + Business), Telegram
- [Tool Policy](features/tool-policy.md) - Fine-grained tool access control (profiles, groups, allow/deny)
- [Web Dashboard](features/web-dashboard.md) - Browser-based control panel and channel management
- [Security](features/security.md) - Injection scanner, audit CLI, Guardian AI, audit logging, self-audit daemon
- [Model Router](features/model-router.md) - Smart complexity-based model selection (Haiku/Sonnet/Opus)
- [Plan Mode](features/plan-mode.md) - Approval workflow for tool execution
- [Integrations](features/integrations.md) - OAuth framework, Gmail, Google Calendar
- [Tools](features/tools.md) - Web search, research, image gen, voice/TTS, delegation, skill gen, URL extract
- [Memory](features/memory.md) - Session compaction, USER.md profile, long-term facts
- [Scheduler](features/scheduler.md) - Cron scheduler, recurring reminders, self-audit daemon
- [Mission Control](features/mission-control.md) - Multi-agent orchestration system

## Architecture

- Planned: Agent architecture, Message bus, Memory system

## API Reference

- Planned: REST API, WebSocket API

## Guides

- [Installation](guides/installation.md) - Quick install for macOS, Linux, and Windows
- [Use Cases](guides/use-cases.md) - Real-world examples (automation, research, coding, multi-agent)
- Planned: Configuration, Deployment

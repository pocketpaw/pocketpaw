# Google Workspace CLI

The Google Workspace CLI (`gws`) is active as an MCP server. It gives you
access to the full Google Workspace API surface via shell commands.

## When to Use GWS CLI vs Built-in Tools

**Use built-in OAuth tools** (gmail_search, gmail_send, calendar_list, etc.)
for simple, single-step operations on Gmail, Calendar, Drive, and Docs.
They are faster and require no subprocess.

**Use GWS CLI** for:
- **Services only available via gws:** Sheets, Chat, Admin, Meet, Keep,
  Forms, Slides, Tasks, Classroom, People
- **Advanced operations:** batch modifications, complex queries, schema
  introspection, pagination, output formatting
- **Workflow skills:** multi-service workflows like meeting prep, inbox
  triage, weekly digests

## Command Pattern

```bash
gws <service> <resource> [sub-resource] <method> [flags]
```

### Key Flags

| Flag | Description |
|------|-------------|
| `--params '{"key": "val"}'` | URL/query parameters |
| `--json '{"key": "val"}'` | Request body |
| `--dry-run` | Validate without calling the API |
| `--fields '<mask>'` | Restrict response fields (saves context) |
| `--format json\|table\|yaml\|csv` | Output format |
| `--page-all` | Auto-paginate (NDJSON output) |
| `-o, --output <PATH>` | Save binary responses to file |
| `--upload <PATH>` | Upload file content (multipart) |

## Services & Helper Commands

| Service | Helpers | Description |
|---------|---------|-------------|
| Gmail | `+send`, `+read`, `+reply`, `+reply-all`, `+forward`, `+triage`, `+watch` | Email |
| Drive | `+upload` | Files and folders |
| Calendar | `+insert`, `+agenda` | Events and scheduling |
| Sheets | `+read`, `+append` | Spreadsheet data |
| Docs | `+write` | Document creation |
| Chat | `+send` | Messaging spaces |
| Tasks | -- | Task management |
| Meet | -- | Video meetings |
| Keep | -- | Notes |
| Forms | -- | Form management |
| Slides | -- | Presentations |
| Classroom | -- | Education |
| People | -- | Contacts |
| Admin Reports | -- | Audit logs, usage reports |

Helper commands use the `+` prefix: `gws gmail +send`, `gws sheets +read`, etc.

## Schema Introspection

Before calling an unfamiliar API method, inspect it:

```bash
gws schema <service>.<resource>.<method>
# Example: gws schema sheets.spreadsheets.values.get
```

This returns required params, types, and defaults.

## Safety Rules

1. **Always use `--dry-run` before mutations** (create, update, delete, send).
   Show the user what will happen, then execute on confirmation.
2. **Use `--fields` to limit response size** and avoid flooding the context.
3. **Never output secrets** (API keys, tokens, credentials).
4. **Confirm destructive actions** (delete, trash, batch modify) with the user.

## Shell Tips

- **zsh `!` expansion:** Wrap sheet ranges in double quotes, not single:
  `gws sheets +read --spreadsheet ID --range "Sheet1!A1:D10"`
- **JSON flags:** Use single quotes around `--params` and `--json` values:
  `gws drive files list --params '{"pageSize": 5}'`

## Authentication

If a gws command returns an auth error, tell the user to run:

```bash
gws auth login
```

## GWS Skills

When `gws-*` skills are installed, you can reference them for detailed
service-specific instructions. Use `gws <service> --help` to discover
available resources and methods for any service.

# Integrations: OAuth, Gmail, Calendar

PocketPaw integrates with Google services via OAuth 2.0. Currently supported: Gmail and Google Calendar.

## OAuth Framework

### Setup

1. Create a Google Cloud project at [console.cloud.google.com](https://console.cloud.google.com)
2. Enable the Gmail API and Google Calendar API
3. Create OAuth 2.0 credentials (Desktop app type)
4. Add your client ID and secret to PocketPaw config

```json
{
  "google_oauth_client_id": "your-client-id.apps.googleusercontent.com",
  "google_oauth_client_secret": "your-client-secret"
}
```

### Auth Flow

1. Agent (or user) triggers an action requiring Google access
2. PocketPaw generates an authorization URL
3. User visits the URL, grants permission, receives an auth code
4. Dashboard provides endpoints for this flow:
   - `GET /api/oauth/authorize` — Returns the authorization URL
   - `GET /oauth/callback` — Handles the redirect with auth code
5. Tokens are stored securely at `~/.pocketclaw/oauth/`

### Token Storage

- Tokens saved as JSON files (one per service, e.g., `google_gmail.json`)
- Files are chmod 0600 (owner-only read/write)
- Automatic token refresh when expired (60-second buffer before expiry)
- `TokenStore` methods: `save()`, `load()`, `delete()`, `list_services()`

### Configuration

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `google_oauth_client_id` | string | `null` | Google OAuth 2.0 client ID |
| `google_oauth_client_secret` | string | `null` | Google OAuth 2.0 client secret |

---

## Gmail

Three core tools for email management, plus helper tools for labels and batch operations.

### Tools

| Tool | Description | Arguments |
|------|-------------|-----------|
| `search_gmail` | Search emails by query | `query` (Gmail search syntax), `max_results` |
| `read_email` | Read a specific email | `message_id` |
| `send_email` | Send an email | `to`, `subject`, `body`, `cc`, `bcc` |

### Additional Tools

| Tool | Description |
|------|-------------|
| `gmail_list_labels` | List all Gmail labels |
| `gmail_create_label` | Create a new label (supports nested with `/`) |
| `gmail_modify` | Add/remove labels on a message |
| `gmail_trash` | Move a message to trash |
| `gmail_batch_modify` | Batch add/remove labels on multiple messages |

### Query Syntax

Gmail search supports the standard Gmail query operators:

```
"from:alice@example.com"          # From specific sender
"subject:meeting"                  # Subject contains "meeting"
"is:unread after:2026/01/01"      # Unread since Jan 2026
"has:attachment filename:pdf"      # PDFs with attachments
"label:work -label:archived"       # In "work" label, not archived
```

### Example

```
You: "Search my Gmail for unread emails from my manager"
Me:  *calls search_gmail with "is:unread from:manager@company.com"*
Me:  "Found 3 unread emails. The most recent is about the Q1 planning doc..."
```

### Tool Policy Group

Gmail tools belong to `group:gmail`. To restrict:

```json
{
  "tools_deny": ["group:gmail"]
}
```

---

## Calendar

Three tools for calendar management.

### Tools

| Tool | Description | Arguments |
|------|-------------|-----------|
| `calendar_list` | List upcoming events | `days_ahead` (default: 1), `max_results` (default: 10) |
| `calendar_create` | Create a new event | `summary`, `start`, `end`, `description`, `attendees` |
| `calendar_prep` | Meeting prep briefing | Shows next meeting with attendees and details |

### Example

```
You: "What meetings do I have today?"
Me:  *calls calendar_list with days_ahead=1*
Me:  "You have 3 meetings: standup at 9am, design review at 11am, 1-on-1 at 3pm."

You: "Schedule a 30-min sync with Alice tomorrow at 2pm"
Me:  *calls calendar_create*
Me:  "Created! '30-min sync' on Feb 8 at 2:00 PM with alice@company.com."
```

### Tool Policy Group

Calendar tools belong to `group:calendar`. To restrict:

```json
{
  "tools_deny": ["group:calendar"]
}
```

---

## Implementation

| File | Description |
|------|-------------|
| `integrations/oauth.py` | `OAuthManager` — auth URL generation, code exchange, token refresh |
| `integrations/token_store.py` | `TokenStore` — file-based token persistence (chmod 0600) |
| `integrations/gmail.py` | `GmailClient` — HTTP client for Gmail API |
| `integrations/gcalendar.py` | `CalendarClient` — HTTP client for Google Calendar API |
| `tools/builtin/gmail.py` | Gmail tool wrappers (search, read, send, labels, batch) |
| `tools/builtin/calendar.py` | Calendar tool wrappers (list, create, prep) |

## Tests

- `tests/test_oauth.py` — OAuth framework tests
- `tests/test_gmail.py` — Gmail integration tests
- `tests/test_gcalendar.py` — Calendar integration tests

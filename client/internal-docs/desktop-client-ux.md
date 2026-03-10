# PocketPaw Desktop Client — UX Spec

> Minimal chat client. Like Telegram Desktop, not VS Code.
> The "Canva for AI agents" entry point.

---

## Design Philosophy

1. **Chat is the interface.** Everything happens through conversation. No complex menus, no deeply nested panels. If you can use Telegram, you can use PocketPaw.

2. **Skills are the Canva moment.** Pre-built workflow cards that non-tech users browse and click. No prompt engineering required. "Use This" → simple form → agent handles the rest.

3. **Progressive disclosure.** First-time user sees: chat + a few skill cards. Power features (memory viewer, activity log, advanced settings) reveal themselves as the user grows.

4. **Invisible complexity.** The Python backend, WebSocket protocol, model routing, Guardian AI — the user never sees any of it. They see a chat window that does things.

---

## Onboarding Flow

Three screens to value. This is the make-or-break moment.

### Screen 1: Welcome

```
┌─────────────────────────────────────────┐
│                                         │
│              🐾                          │
│                                         │
│         Welcome to PocketPaw            │
│                                         │
│    Your AI that runs on your machine.   │
│    Private. Secure. Yours.              │
│                                         │
│                                         │
│         [ Get Started → ]               │
│                                         │
└─────────────────────────────────────────┘
```

No signup. No account creation. No email. Just a button.

### Screen 2: Choose Your AI

```
┌─────────────────────────────────────────┐
│                                         │
│    How would you like to power          │
│    your AI?                             │
│                                         │
│  ┌─────────────────┐ ┌───────────────┐  │
│  │                 │ │               │  │
│  │   Free & Local  │ │   Powerful    │  │
│  │                 │ │               │  │
│  │   Uses Ollama   │ │  Uses Claude  │  │
│  │   Runs 100%     │ │  Smarter,     │  │
│  │   on your       │ │  needs an     │  │
│  │   machine.      │ │  API key.     │  │
│  │   No account    │ │  $3-15/mo     │  │
│  │   needed.       │ │  typical.     │  │
│  │                 │ │               │  │
│  │  [ Set up → ]   │ │ [ Set up → ]  │  │
│  └─────────────────┘ └───────────────┘  │
│                                         │
│  More options: OpenAI, Google, Groq...  │
│                                         │
└─────────────────────────────────────────┘
```

Two clear paths. No jargon. "Free & Local" vs "Powerful." The user doesn't need to know what Ollama or Claude is — they pick a lane.

**Ollama path (auto-setup):**
```
┌─────────────────────────────────────────┐
│                                         │
│    Setting up your local AI...          │
│                                         │
│    ████████████████░░░░  78%            │
│                                         │
│    Downloading a small AI model         │
│    (about 4 GB, one-time setup)         │
│                                         │
│    This runs entirely on your machine.  │
│    Nothing leaves your computer.        │
│                                         │
└─────────────────────────────────────────┘
```

PocketPaw detects if Ollama is installed. If yes, pull a default model. If no, offer to install it (or link to ollama.com with clear instructions). The goal: zero-config local AI.

**API key path:**
```
┌─────────────────────────────────────────┐
│                                         │
│    Paste your API key                   │
│                                         │
│    ┌─────────────────────────────────┐  │
│    │ sk-ant-...                      │  │
│    └─────────────────────────────────┘  │
│                                         │
│    [ Where do I get one? → ]            │
│                                         │
│    Your key is encrypted and stored     │
│    locally. We never see it.            │
│                                         │
│              [ Continue → ]             │
│                                         │
└─────────────────────────────────────────┘
```

One field. One link for help. Trust message about encryption. No 12-step configuration wizard.

### Screen 3: You're In

```
┌─────────────────────────────────────────┐
│                                         │
│              🐾                          │
│                                         │
│         You're all set.                 │
│                                         │
│    Try saying:                          │
│    "Summarize this PDF"                 │
│    "What can you help me with?"         │
│    "Set a reminder for tomorrow"        │
│                                         │
│         [ Start Chatting → ]            │
│                                         │
└─────────────────────────────────────────┘
```

Suggests first messages. Drops the user directly into chat. No tutorial slideshow. Learning happens by doing.

---

## Main Interface

### Layout

```
┌──────────────────────────────────────────────────────────────┐
│  🐾 PocketPaw              ○ Connected    ─  □  ✕           │
├──────────────┬───────────────────────────────────────────────┤
│              │                                               │
│  Sessions    │  🐾 PocketPaw                                 │
│              │  Hey! What can I help you with today?         │
│  ● New Chat  │                                               │
│              │  ┌─────────────────────────────────────────┐  │
│  Today       │  │ You                                     │  │
│  ○ Budget    │  │ Can you summarize this PDF for me?      │  │
│    review    │  │ 📎 Q4-report.pdf                        │  │
│  ○ Recipe    │  └─────────────────────────────────────────┘  │
│    finder    │                                               │
│              │  ┌─────────────────────────────────────────┐  │
│  Yesterday   │  │ 🐾 PocketPaw                            │  │
│  ○ Travel    │  │                                         │  │
│    plans     │  │ Here's what I found in your Q4 report:  │  │
│              │  │                                         │  │
│──────────────│  │ Revenue: $2.4M (+12% QoQ)               │  │
│              │  │ Key risks: Supply chain delays in APAC  │  │
│  Explore     │  │ Action items:                           │  │
│              │  │ • Review vendor contracts by Jan 15     │  │
│  ⚡ Quick     │  │ • Schedule APAC team sync               │  │
│    Tasks     │  │                                         │  │
│  📊 Analyze  │  │ Want me to create tasks for these       │  │
│    Data      │  │ action items?                           │  │
│  📝 Write    │  │                                         │  │
│    Content   │  │ [ Yes, create tasks ] [ No thanks ]     │  │
│  🔧 Dev      │  └─────────────────────────────────────────┘  │
│    Tools     │                                               │
│  📁 Files    │                                               │
│              │                                               │
│──────────────│                                               │
│  ⚙ Settings  │  ┌───────────────────────────────────────┐    │
│  📊 Activity │  │ Message...                     📎 ▶ │    │
│              │  └───────────────────────────────────────┘    │
└──────────────┴───────────────────────────────────────────────┘
```

**Left sidebar** (collapsible):
- Sessions list (like Telegram's chat list)
- "Explore" section with skill categories
- Settings + Activity at the bottom

**Main area**:
- Chat messages with rich formatting (markdown, code blocks, tables)
- Inline action buttons on agent responses ("Yes, create tasks" / "No thanks")
- File attachments with previews
- Streaming text (words appear as the agent thinks)

**Message input**:
- Text field with file attach button and send
- Drag-and-drop zone (drop files anywhere in the window)
- Slash commands for power users (`/skills`, `/memory`, `/settings`)

### Chat Features

**Rich messages from the agent:**
- Markdown rendering (headers, lists, tables, code)
- Inline action buttons (approve/reject, choose options)
- File cards (generated files with download/open buttons)
- Progress indicators ("Working on it..." with expanding activity detail)
- Image/chart previews inline

**User input:**
- Text messages (primary)
- File drag-and-drop or attach button
- Voice input (microphone button, uses STT tool)
- Slash commands (power user shortcut to skills)
- Reply-to (click a previous message to reference it, like Telegram)

---

## Skill Cards — The "Canva" Moment

Skills are pre-built workflows that don't require prompting skill. The user browses categories, picks one, fills a simple form, and the agent executes.

### Skill Browser (Explore section)

```
┌──────────────────────────────────────────────────────────┐
│  Explore Skills                              🔍 Search   │
│                                                          │
│  ⚡ Quick Tasks                                          │
│  ┌────────────────┐ ┌────────────────┐ ┌──────────────┐ │
│  │ 📝 Summarize   │ │ 🌐 Translate   │ │ 📧 Draft     │ │
│  │                │ │                │ │   Email      │ │
│  │ Drop a file,   │ │ Any text, any  │ │ Describe     │ │
│  │ get key points.│ │ language pair. │ │ the context. │ │
│  │                │ │                │ │              │ │
│  │ [Use This →]   │ │ [Use This →]   │ │ [Use This →] │ │
│  └────────────────┘ └────────────────┘ └──────────────┘ │
│                                                          │
│  📊 Analyze Data                                         │
│  ┌────────────────┐ ┌────────────────┐ ┌──────────────┐ │
│  │ 🧾 Expense     │ │ 📈 CSV         │ │ 📋 Compare   │ │
│  │    Report      │ │    Analyzer    │ │   Documents  │ │
│  │                │ │                │ │              │ │
│  │ Drop receipts, │ │ Upload a CSV,  │ │ Two files,   │ │
│  │ get a          │ │ ask questions  │ │ side-by-side │ │
│  │ spreadsheet.   │ │ about it.      │ │ analysis.    │ │
│  │                │ │                │ │              │ │
│  │ [Use This →]   │ │ [Use This →]   │ │ [Use This →] │ │
│  └────────────────┘ └────────────────┘ └──────────────┘ │
│                                                          │
│  📝 Write Content                                        │
│  ┌────────────────┐ ┌────────────────┐ ┌──────────────┐ │
│  │ ✍️ Blog Post   │ │ 📱 Social      │ │ 📄 Report    │ │
│  │               │ │    Media      │ │   Builder    │ │
│  │ Topic + tone, │ │ Platform +    │ │ Drop notes,  │ │
│  │ full draft.   │ │ topic, done.  │ │ get polished  │ │
│  │               │ │               │ │ document.    │ │
│  │ [Use This →]  │ │ [Use This →]  │ │ [Use This →] │ │
│  └────────────────┘ └────────────────┘ └──────────────┘ │
│                                                          │
│  🔧 Developer Tools                                     │
│  ┌────────────────┐ ┌────────────────┐ ┌──────────────┐ │
│  │ 🐛 Debug       │ │ 📖 Explain     │ │ 🔍 Code      │ │
│  │    Error       │ │    Code       │ │   Review     │ │
│  │                │ │               │ │              │ │
│  │ Paste error,   │ │ Drop a file,  │ │ PR link or   │ │
│  │ get diagnosis  │ │ get walkthru. │ │ file, get    │ │
│  │ + fix.        │ │               │ │ feedback.    │ │
│  │               │ │               │ │              │ │
│  │ [Use This →]  │ │ [Use This →]  │ │ [Use This →] │ │
│  └────────────────┘ └────────────────┘ └──────────────┘ │
│                                                          │
│  📁 File Management                                     │
│  ┌────────────────┐ ┌────────────────┐                  │
│  │ 🗂️ Organize    │ │ 🔄 Convert     │                  │
│  │    Downloads  │ │    Files      │                  │
│  │               │ │               │                  │
│  │ Sort messy    │ │ PDF to Word,  │                  │
│  │ folders by    │ │ images to     │                  │
│  │ type + date.  │ │ PDF, etc.     │                  │
│  │               │ │               │                  │
│  │ [Use This →]  │ │ [Use This →]  │                  │
│  └────────────────┘ └────────────────┘                  │
└──────────────────────────────────────────────────────────┘
```

### Skill Interaction Flow

When a user clicks "Use This" on a skill card:

```
Step 1: Simple form (skill-specific)
┌──────────────────────────────────────────┐
│  🧾 Expense Report                       │
│                                          │
│  Drop your receipts here                 │
│  ┌──────────────────────────────────┐    │
│  │                                  │    │
│  │     📎 Drop files or click       │    │
│  │        to browse                 │    │
│  │                                  │    │
│  │  receipt1.jpg  receipt2.png  ✕   │    │
│  │  receipt3.pdf                ✕   │    │
│  │                                  │    │
│  └──────────────────────────────────┘    │
│                                          │
│  Currency: [ USD ▾ ]                     │
│                                          │
│  Categories: (optional)                  │
│  [ ] Travel  [ ] Food  [ ] Software     │
│  [ ] Office  [ ] Other                   │
│                                          │
│       [ Cancel ]    [ Go → ]             │
│                                          │
└──────────────────────────────────────────┘

Step 2: Agent works (back in chat view)
┌──────────────────────────────────────────┐
│                                          │
│  🐾 PocketPaw                            │
│  Working on your expense report...       │
│                                          │
│  ▼ Activity                              │
│    ✓ Reading receipt1.jpg (OCR)          │
│    ✓ Reading receipt2.png (OCR)          │
│    ● Reading receipt3.pdf...             │
│    ○ Categorizing expenses               │
│    ○ Creating spreadsheet                │
│                                          │
└──────────────────────────────────────────┘

Step 3: Result delivered (in chat)
┌──────────────────────────────────────────┐
│                                          │
│  🐾 PocketPaw                            │
│  Here's your expense report.             │
│                                          │
│  ┌────────────────────────────────────┐  │
│  │ 📊 expenses-feb-2026.xlsx          │  │
│  │                                    │  │
│  │ 3 receipts, $247.50 total          │  │
│  │ Categories: Travel ($120),         │  │
│  │ Food ($87.50), Software ($40)      │  │
│  │                                    │  │
│  │ [ Open File ]  [ Save to... ]      │  │
│  └────────────────────────────────────┘  │
│                                          │
│  Anything else you'd like me to adjust?  │
│                                          │
└──────────────────────────────────────────┘
```

The skill did the prompting for them. The user never typed a prompt. They clicked a card, dropped files, clicked "Go." That's the Canva bar.

### Skill Card Data Model

Each skill card maps to an existing PocketPaw skill file (markdown in `.pocketpaw/skills/` or built-in), plus UI metadata:

```yaml
# expense-report.skill.yaml
id: expense-report
name: Expense Report
icon: receipt
category: analyze-data
description: Drop receipts, get a spreadsheet.

# Form fields shown to the user
inputs:
  - id: files
    type: file-drop
    label: Drop your receipts here
    accept: [image/*, application/pdf]
    required: true
  - id: currency
    type: select
    label: Currency
    options: [USD, EUR, GBP, INR, JPY]
    default: USD
  - id: categories
    type: multi-select
    label: Categories
    options: [Travel, Food, Software, Office, Other]
    required: false

# What gets sent to the agent (template)
prompt_template: |
  Create an expense report spreadsheet from these receipts.
  Currency: {{currency}}
  {{#if categories}}Categories to use: {{categories}}{{/if}}
  Attached files: {{files}}
  Output as .xlsx with columns: Date, Vendor, Amount, Category.
  Include a totals row and a category breakdown.
```

The skill YAML defines the form UI. The prompt_template turns form inputs into a well-crafted prompt. The user never writes the prompt — the skill card does it for them.

---

## Activity Panel

Expandable panel showing what the agent is doing. Collapsed by default (just a status line), expandable for detail.

```
Collapsed (in chat):
┌─────────────────────────────────────────┐
│  ● Working...  Reading receipt3.pdf     │
└─────────────────────────────────────────┘

Expanded (click to expand):
┌─────────────────────────────────────────┐
│  Activity                          ▲    │
│                                         │
│  10:32:01  Tool: OCR → receipt1.jpg     │
│  10:32:03  Result: "Uber $45.00 Feb 12" │
│  10:32:04  Tool: OCR → receipt2.png     │
│  10:32:06  Result: "AWS $40.00 Feb 14"  │
│  10:32:07  Tool: OCR → receipt3.pdf     │
│  10:32:09  ● Processing...              │
│                                         │
│  Guardian AI: ✓ All actions approved    │
│  Model: claude-sonnet-4-5               │
│  Tokens: 2,847 used                     │
└─────────────────────────────────────────┘
```

Power users care about this. Non-tech users ignore it. Progressive disclosure — it's there when you want it, invisible when you don't.

---

## Settings (Minimal)

Settings are accessed via the sidebar gear icon. Organized in tabs, not a sprawling form.

```
┌──────────────────────────────────────────────────────────┐
│  Settings                                          ✕     │
│                                                          │
│  [ AI Model ]  [ Channels ]  [ Security ]  [ About ]    │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │  AI Model                                          │  │
│  │                                                    │  │
│  │  Provider: [ Anthropic (Claude) ▾ ]                │  │
│  │                                                    │  │
│  │  API Key:  [ sk-ant-••••••••••••oR3 ]  [Change]    │  │
│  │            ✓ Encrypted and stored locally           │  │
│  │                                                    │  │
│  │  Model:    [ Claude Sonnet 4.5 ▾ ]                 │  │
│  │                                                    │  │
│  │  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─  │  │
│  │                                                    │  │
│  │  Or use a free local model:                        │  │
│  │  [ Switch to Ollama (free, offline) → ]            │  │
│  │                                                    │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│               [ Save ]                                   │
└──────────────────────────────────────────────────────────┘
```

Four tabs max. Most users only ever touch "AI Model" during onboarding. Channels tab for connecting Telegram/Discord. Security tab shows Guardian AI status and audit log. About tab for version and updates.

---

## System Tray Behavior

The desktop app lives in the system tray / menu bar when the window is closed.

**Tray icon states:**
- 🟢 Green dot — idle, ready
- 🟠 Orange dot — agent working on something
- 🔴 Red dot — needs attention (error, or agent is asking a question)
- 🔵 Blue dot — new message / result ready

**Right-click menu:**
```
┌─────────────────────────┐
│  Open PocketPaw         │
│  ─────────────────────  │
│  Quick Ask...     ⌘⇧P  │
│  ─────────────────────  │
│  ● Connected (Claude)   │
│  ○ 2 tasks running      │
│  ─────────────────────  │
│  Settings               │
│  Check for Updates      │
│  ─────────────────────  │
│  Quit PocketPaw         │
└─────────────────────────┘
```

**"Quick Ask" (global hotkey Cmd+Shift+P):**

A floating input bar that appears over whatever you're doing. Type a question, hit Enter, PocketPaw handles it in the background. Like Spotlight/Raycast but for your agent.

```
┌──────────────────────────────────────────────────────┐
│  🐾  What can I help with?                      ▶   │
└──────────────────────────────────────────────────────┘
```

Result appears as a notification or opens the main window if complex.

---

## Universal AI Side Panel

The defining feature of PocketPaw Desktop. A floating chat panel that docks to the edge of your screen and works alongside **any application** — Word, Excel, VS Code, Chrome, Figma, anything.

This is what makes PocketPaw an OS layer, not just an app.

### How It Looks

```
┌──────────────────────────────────────┐ ┌────────────────────┐
│                                      │ │ 🐾 PocketPaw       │
│                                      │ │                    │
│   Any application                    │ │ Working on:        │
│   (Word, Excel, VS Code,            │ │ 📄 Proposal.docx   │
│    Chrome, Figma, Terminal...)       │ │                    │
│                                      │ │ ────────────────── │
│                                      │ │                    │
│   The user works in their            │ │ You: "Make the     │
│   preferred app as usual.            │ │ deadline dates      │
│   Nothing changes.                   │ │ two weeks later"   │
│                                      │ │                    │
│                                      │ │ 🐾: Done. Updated  │
│                                      │ │ all 5 phase dates. │
│                                      │ │ Word should prompt │
│                                      │ │ you to reload.     │
│                                      │ │                    │
│                                      │ │ You: "Also add a   │
│                                      │ │ budget section"    │
│                                      │ │                    │
│                                      │ │ 🐾: Added Section  │
│                                      │ │ 7: Budget with a   │
│                                      │ │ placeholder table. │
│                                      │ │                    │
│                                      │ │ ────────────────── │
│                                      │ │                    │
│                                      │ │ [___________] 📎▶ │
└──────────────────────────────────────┘ └────────────────────┘
  User's app (unchanged)                  PocketPaw side panel
                                          (separate Tauri window)
```

### How It Works (Technical)

**1. Window management (Tauri Rust sidecar)**

The side panel is a **second Tauri window** — slim, always-on-top (optional), docked to the right edge of the screen. Tauri v2's multi-window support handles this natively.

```
Tauri app
├── Main window        → Full PocketPaw client (chat, skills, settings)
└── Side panel window  → Slim floating chat, contextually aware
```

The user can:
- Toggle the side panel from the tray menu or hotkey (`Cmd+Shift+P` opens quick ask, `Cmd+Shift+L` toggles side panel)
- Drag it to any screen edge (left, right)
- Resize width
- Pin it always-on-top or let it go behind other windows
- Collapse to a thin strip (just the 🐾 icon) when not chatting

**2. Context detection (knows what file you're working on)**

The Rust sidecar detects the active application and file:

```
macOS:  NSWorkspace.shared.frontmostApplication
        + Accessibility API for window title / open document path

Windows: GetForegroundWindow() + GetWindowText()
         + Shell API for document path

Linux:  xdotool getactivewindow + xprop
        + /proc/{pid}/fd for open file descriptors
```

The side panel header updates automatically:

```
Working with Word → "📄 Proposal.docx"
Switched to VS Code → "📂 ~/projects/myapp (VS Code)"
Switched to Chrome → "🌐 github.com/pocketpaw/pocketpaw"
Switched to Finder → "📁 ~/Downloads"
No detectable file → "🐾 Ready to help"
```

The agent receives this context with every message. When the user says "make the dates two weeks later," the agent already knows which file to edit.

**3. File editing (agent writes, native app reloads)**

The flow:

```
User types "add a budget table" in side panel
        │
        ▼
PocketPaw agent receives:
  context: { file: "~/Documents/Proposal.docx", app: "Microsoft Word" }
  message: "add a budget table"
        │
        ▼
Agent reads Proposal.docx (python-docx)
Agent edits the document (adds Section 7: Budget with table)
Agent writes Proposal.docx back to disk
        │
        ▼
Native app detects file changed:
  Word: "The document has been modified. Reload?" → Yes
  VS Code: auto-reloads (built-in file watcher)
  Excel: prompts to reload
  Most text editors: auto-reload
        │
        ▼
User sees the changes in their native app
Agent confirms in side panel: "Done. Added Section 7: Budget."
```

**File format support via Python libraries:**

| Format | Read/Write Library | Auto-reload behavior |
|---|---|---|
| `.docx` | python-docx | Word prompts reload |
| `.xlsx` | openpyxl | Excel prompts reload |
| `.pptx` | python-pptx | PowerPoint prompts reload |
| `.pdf` | PyPDF2 / reportlab | Preview auto-reloads on macOS |
| `.md` / `.txt` | built-in Python | Most editors auto-reload |
| `.csv` | csv / pandas | Most editors auto-reload |
| `.json` | built-in Python | VS Code auto-reloads |
| `.py` / code | built-in Python | VS Code auto-reloads |
| images | Pillow | Preview auto-reloads |

**4. Non-file contexts (browser, terminal, etc.)**

The side panel isn't limited to files:

```
User is in Chrome on a long article:
  Side panel: "🌐 medium.com/some-long-article"
  User: "summarize this page"
  Agent: uses browser tool to read the URL, summarizes in chat

User is in Terminal with an error:
  Side panel: "Terminal — ~/projects/myapp"
  User: "I just got a segfault, help"
  Agent: asks to see the error, diagnoses, suggests fix

User is in Figma:
  Side panel: "🎨 Figma — Dashboard v2"
  User: "write the CSS for this layout"
  Agent: can't see Figma directly, but user can screenshot
         (Cmd+Shift+4 → paste into side panel)
```

### Side Panel Modes

**Docked mode** (default): Panel is a separate window docked to screen edge. Resizable. Always visible alongside your work.

```
┌─────────────────────────────┬──────────┐
│  Your app takes most        │  Side    │
│  of the screen              │  Panel   │
│                             │  (slim)  │
└─────────────────────────────┴──────────┘
```

**Collapsed mode**: Panel shrinks to a thin vertical strip with the 🐾 icon. Click to expand. Hover to peek.

```
┌──────────────────────────────────────┬──┐
│  Your app has full screen            │🐾│
│                                      │  │
└──────────────────────────────────────┴──┘
```

**Detached mode**: Panel floats freely, can be positioned anywhere, on any monitor.

**Hidden**: Panel fully hidden, accessible only via tray icon or hotkey.

### Why This Beats Everything Else

| Feature | PocketPaw Side Panel | Open Interpreter | Claude Cowork | Windows Copilot |
|---|---|---|---|---|
| Works with any app | Yes (OS-level) | No (own editor only) | No (own sandbox) | Partially (limited) |
| File editing | Agent edits, native app shows | Built-in editor | Built-in sandbox | No file editing |
| Context-aware | Detects active app/file | Only its own docs | Only granted folders | Limited |
| Chat interface | Full chat + skills | Chat sidebar | Chat interface | Chat sidebar |
| Security | Guardian AI reviews all edits | None | VM sandbox | Microsoft's filters |
| Works offline | Yes (Ollama) | Yes (Ollama) | No | No |
| Open source | Yes | Pivoted commercial | No | No |

The key differentiator: **Open Interpreter and Cowork force you into their editor. PocketPaw meets you wherever you already are.**

### Implementation Priority

The side panel is a **Phase 1 (v0.6)** feature for the desktop app. It uses:

- Tauri v2 multi-window API (stable)
- Same Svelte ChatPanel component as the main window
- Tauri Rust plugins for OS-level window/file detection
- PocketPaw's existing file editing tools (python-docx, openpyxl, etc.)
- Same WebSocket connection to the Python backend

No new backend work needed. The side panel is purely a frontend/shell feature that sends messages to the same agent with extra context (active file path + app name).

### Svelte Component Reuse

```
Main Window                          Side Panel
┌──────────────────────────┐         ┌──────────────────┐
│ SessionList.svelte       │         │                  │
│ SkillBrowser.svelte      │         │ ContextBar.svelte│  ← new (shows active file)
│ ChatPanel.svelte    ◄────┼─────────┤ ChatPanel.svelte │  ← same component
│ ActivityFeed.svelte      │         │                  │
│ SettingsPanel.svelte     │         │                  │
└──────────────────────────┘         └──────────────────┘
```

One new component: `ContextBar.svelte` — displays the detected active file/app. Everything else is reused from the main window.

---

## Native Notifications

Desktop notifications with action buttons:

```
┌──────────────────────────────────────────┐
│  🐾 PocketPaw                    just now │
│                                          │
│  Your expense report is ready.           │
│  3 receipts, $247.50 total.              │
│                                          │
│  [ Open File ]        [ View in Chat ]   │
└──────────────────────────────────────────┘
```

```
┌──────────────────────────────────────────┐
│  🐾 PocketPaw                    just now │
│                                          │
│  Guardian AI blocked a command:          │
│  rm -rf /var/log/*                       │
│                                          │
│  [ Review ]           [ Dismiss ]        │
└──────────────────────────────────────────┘
```

Notifications are the connection between "always-on agent" and "user who's doing other things." The agent works in the background; notifications surface what matters.

---

## File Handling

The desktop client handles files better than any chat channel can.

**Drag and drop:**
- Drop files anywhere in the chat window
- Multiple files at once
- Preview thumbnails for images
- File type badges (PDF, XLSX, PNG, etc.)

**Generated files:**
- Appear as cards in chat with preview
- "Open File" launches the system default app (Excel for .xlsx, Preview for .pdf)
- "Save to..." lets the user pick a location
- Files are stored in `~/.pocketpaw/workspace/` by default

**Folder access (later phase):**
- Settings > Folders: grant agent access to specific directories
- Agent can read/write within granted folders
- Visual indicator shows which folders are accessible
- Guardian AI reviews operations outside granted folders

---

## Responsive Behavior

**Window sizes:**
- **Full**: sidebar + chat (default, like Telegram Desktop)
- **Compact**: chat only, sidebar collapses (< 600px width)
- **Mini**: just the input bar (pinned to corner, for quick asks while working)

**Offline mode:**
- If using Ollama: fully functional offline
- If using API: shows "Offline — waiting for connection" with queued messages
- Queued messages send automatically when connection returns

---

## Keyboard Shortcuts (Power Users)

The desktop client respects power users who prefer keyboards.

| Shortcut | Action |
|---|---|
| `Cmd+Shift+P` | Global quick ask (works from any app) |
| `Cmd+N` | New chat session |
| `Cmd+K` | Search sessions |
| `Cmd+,` | Settings |
| `Cmd+E` | Toggle explore/skills panel |
| `Cmd+.` | Toggle activity panel |
| `Cmd+1-9` | Switch to session 1-9 |
| `Enter` | Send message |
| `Shift+Enter` | New line in message |
| `/` | Open slash command palette |
| `Esc` | Close panels / minimize to tray |

---

## Tech Stack

| Layer | Technology | Rationale |
|---|---|---|
| Shell | Tauri v2 | ~5MB binary, Rust backend for OS integration, webview for UI |
| Frontend | Svelte | Shared components with web dashboard, compiled, fast |
| Styling | Tailwind CSS | Same utility classes across desktop + web |
| State | Svelte stores + WebSocket | Real-time sync with PocketPaw core |
| Backend | PocketPaw Python core | Runs as localhost server, desktop connects via WS |
| Tray/Hotkey | Tauri Rust plugins | system-tray, global-shortcut, notification plugins |
| Auto-update | Tauri updater | Built-in update mechanism, checks GitHub releases |
| Bundled Python | PyInstaller or embedded | Non-tech users don't install Python separately |

### Build targets:
- **Windows**: `.exe` installer (NSIS via Tauri)
- **macOS**: `.dmg` (signed + notarized when we get Apple Dev cert)
- **Linux**: `.AppImage` + `.deb`

### Bundled Python runtime:
The installer includes an embedded Python environment with PocketPaw pre-installed. The user never interacts with Python, pip, or virtual environments. The Tauri shell manages the Python process lifecycle (start on app launch, stop on quit, restart on crash).

---

## What This Is NOT

- **Not a browser.** No built-in web browsing UI. The agent can browse (via Playwright tools), but the user sees results in chat, not a browser pane.
- **Not an IDE.** No code editors, no terminals, no file trees. Developer tools are skills, not UI panels.
- **Not a file manager.** No folder tree view. File operations happen through chat commands and skill cards.
- **Not feature-complete on day one.** v1 ships with: chat, 6-8 skill cards, settings, system tray, notifications. Everything else comes later based on what users actually ask for.

---

## Success Metrics

The desktop client succeeds if:

1. **Non-tech user can go from download to first useful result in < 3 minutes**
2. **Skill cards get used more than free-form chat** (proves the "Canva" model works)
3. **Users leave it running** (tray presence, not "open and close")
4. **Retention**: users come back the next day, the next week
5. **Word of mouth**: "You should try PocketPaw" happens because the experience is surprisingly good, not because the feature list is long

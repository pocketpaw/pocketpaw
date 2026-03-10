# Task 05: Chat Interface

> The core of the app. Telegram-like chat with streaming, markdown, and file attachments.

## Goal

Build the main chat panel — message display, input bar, streaming responses, rich formatting.

## Depends On

- **Task 03** (Shell Layout): app shell, main content area
- **Task 02** (Stores): `chatStore`, `connectionStore`, `activityStore`

## Install Additional shadcn Components

```bash
bunx shadcn-svelte@latest add textarea
bunx shadcn-svelte@latest add dropdown-menu
bunx shadcn-svelte@latest add dialog
```

## Additional Dependencies

```bash
bun add marked           # Markdown parsing
bun add dompurify        # Sanitize HTML from markdown
bun add @types/dompurify -D
```

## Files to Create

```
src/lib/components/chat/
├── ChatPanel.svelte            # Container: messages + input
├── MessageList.svelte          # Scrollable message list
├── ChatMessage.svelte          # Single message bubble
├── UserMessage.svelte          # User message variant
├── AssistantMessage.svelte     # Assistant message with markdown
├── StreamingMessage.svelte     # In-progress streaming message
├── ChatInput.svelte            # Message input bar
├── FilePreview.svelte          # Attached file thumbnail/badge
├── ActionButtons.svelte        # Inline action buttons in assistant messages
├── EmptyState.svelte           # Empty chat placeholder
├── CodeBlock.svelte            # Syntax-highlighted code block
└── MarkdownRenderer.svelte     # Markdown → HTML renderer

src/routes/
└── +page.svelte                # Updated to use ChatPanel
```

## Layout

```
┌───────────────────────────────────────────────┐
│                                               │
│  🐾 PocketPaw                                 │
│  Hey! What can I help you with today?         │
│                                               │
│  ┌─────────────────────────────────────────┐  │
│  │ You                                     │  │
│  │ Can you summarize this PDF for me?      │  │
│  │ 📎 Q4-report.pdf                        │  │
│  └─────────────────────────────────────────┘  │
│                                               │
│  ┌─────────────────────────────────────────┐  │
│  │ 🐾 PocketPaw                            │  │
│  │                                         │  │
│  │ Here's what I found in your Q4 report:  │  │
│  │                                         │  │
│  │ **Revenue:** $2.4M (+12% QoQ)           │  │
│  │ **Key risks:** Supply chain delays      │  │
│  │                                         │  │
│  │ [ Yes, create tasks ] [ No thanks ]     │  │
│  └─────────────────────────────────────────┘  │
│                                               │
│  ┌───────────────────────────────────────┐    │
│  │ Message...                     📎 ▶  │    │
│  └───────────────────────────────────────┘    │
│                                               │
└───────────────────────────────────────────────┘
```

## Component Specs

### ChatPanel.svelte

The container that composes everything:
- If `chatStore.isEmpty` → show `EmptyState`
- Otherwise → `MessageList` + `ChatInput`
- Handle scroll-to-bottom on new messages
- Handle file drop zone (entire panel is droppable)

### MessageList.svelte

- Scrollable container using shadcn `ScrollArea`
- Renders `ChatMessage` for each message in `chatStore.messages`
- If `chatStore.isStreaming` → append `StreamingMessage` at the bottom
- Auto-scroll to bottom on new messages (with "scroll to bottom" button if user scrolled up)
- Group consecutive messages from same role

### ChatMessage.svelte

Dispatcher that renders `UserMessage` or `AssistantMessage` based on `message.role`.

### UserMessage.svelte

- Right-aligned or left-aligned with "You" label (match the mockup style)
- Plain text content
- Show attached files as `FilePreview` chips
- Subtle timestamp on hover

### AssistantMessage.svelte

- Left-aligned with 🐾 PocketPaw label
- Content rendered through `MarkdownRenderer`
- Support inline action buttons (extracted from markdown or metadata)
- Copy button on hover (copies raw content)
- File cards for generated files (with "Open File" / "Save to..." buttons)

### StreamingMessage.svelte

- Same layout as `AssistantMessage` but with:
  - Blinking cursor at the end
  - Content from `chatStore.streamingContent`
  - Subtle typing indicator animation
  - "Stop" button to cancel generation

### ChatInput.svelte

- Auto-resizing textarea (grows with content, max ~6 lines)
- `Enter` to send, `Shift+Enter` for newline
- File attach button (📎) — opens file picker
- Send button (▶) — disabled when empty or streaming
- Show attached files as removable chips above the input
- Drag-and-drop file support
- Slash commands: typing `/` shows a command palette (future — just detect `/` for now)
- Disable during streaming (show "Stop generating" button instead of send)

### MarkdownRenderer.svelte

- Parse markdown with `marked`
- Sanitize HTML with `dompurify`
- Custom renderers:
  - **Code blocks**: syntax highlighting with language label + copy button (`CodeBlock.svelte`)
  - **Tables**: styled with Tailwind
  - **Links**: open in system browser (via Tauri `opener`)
  - **Images**: inline with click-to-enlarge
  - **Lists**: proper indentation

### CodeBlock.svelte

- Language label in top-right corner
- Copy button
- Syntax highlighting (use a lightweight highlighter like `highlight.js` or just style with Tailwind)
- Horizontal scroll for long lines

### EmptyState.svelte

Shown when the chat is empty (new session):

```
┌───────────────────────────────────────────────┐
│                                               │
│                    🐾                         │
│                                               │
│          What can I help you with?            │
│                                               │
│    ┌──────────┐ ┌──────────┐ ┌──────────┐    │
│    │ Summarize│ │ Write an │ │ Debug    │    │
│    │ a file   │ │ email    │ │ an error │    │
│    └──────────┘ └──────────┘ └──────────┘    │
│                                               │
│    ┌──────────┐ ┌──────────┐ ┌──────────┐    │
│    │ Analyze  │ │ Translate│ │ Research │    │
│    │ data     │ │ text     │ │ a topic  │    │
│    └──────────┘ └──────────┘ └──────────┘    │
│                                               │
└───────────────────────────────────────────────┘
```

- Suggestion chips are clickable — clicking pre-fills the input
- Centered vertically in the chat area

### FilePreview.svelte

- Small chip/badge showing: icon (by file type) + filename + size
- Remove button (✕) for files being attached (not yet sent)
- Click to open (for received files)

### ActionButtons.svelte

- Horizontal row of buttons at the bottom of an assistant message
- Example: `[ Yes, create tasks ] [ No thanks ]`
- Clicking sends the button text as a user message

## File Drag-and-Drop

- The entire `ChatPanel` is a drop zone
- On drag-over: show a visual overlay ("Drop files here")
- On drop: add files to `ChatInput`'s attachment list
- Support multiple files
- Accept common types: images, PDFs, documents, code files

## Scroll Behavior

- Auto-scroll to bottom on new messages / streaming chunks
- If user scrolls up: stop auto-scrolling, show "↓ New messages" button at bottom
- Clicking the button scrolls to bottom and re-enables auto-scroll
- Preserve scroll position when loading older history

## Acceptance Criteria

- [ ] Messages render with correct alignment (user vs assistant)
- [ ] Markdown renders correctly (headers, bold, lists, tables, code blocks)
- [ ] Code blocks have syntax highlighting and copy button
- [ ] Streaming works: chunks appear character by character with cursor
- [ ] Stop button cancels generation
- [ ] Auto-scroll works with scroll-up detection
- [ ] File attach via button and drag-and-drop
- [ ] Empty state shows suggestion chips
- [ ] Enter sends, Shift+Enter adds newline
- [ ] Links open in system browser
- [ ] Copy button on assistant messages works

## Notes

- Performance matters here. For long conversations (100+ messages), consider virtualizing the message list.
- Markdown rendering should be safe — always sanitize HTML output.
- Don't build a full file manager. File attachments are simple: pick file → attach → send.
- The chat panel component (`ChatPanel.svelte`) will be reused by the Side Panel (Task 11).

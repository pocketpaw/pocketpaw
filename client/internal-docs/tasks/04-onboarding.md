# Task 04: Onboarding Wizard

> Three screens to value. Download → Choose AI → Start chatting.

## Goal

Build the first-run onboarding flow. A 3-step wizard that gets non-technical users from zero to chatting.

## Depends On

- **Task 03** (Shell Layout): routing, shadcn components
- **Task 02** (Stores): `connectionStore`, `settingsStore`

## Install Additional shadcn Components

```bash
bunx shadcn-svelte@latest add card
bunx shadcn-svelte@latest add progress
bunx shadcn-svelte@latest add radio-group
bunx shadcn-svelte@latest add label
```

## Files to Create

```
src/routes/onboarding/
└── +page.svelte                    # Wizard container with step state

src/lib/components/onboarding/
├── OnboardingWizard.svelte         # Step controller
├── StepWelcome.svelte              # Screen 1: Welcome
├── StepChooseAI.svelte             # Screen 2: Choose AI provider
├── StepReady.svelte                # Screen 3: You're all set
├── OllamaSetup.svelte              # Sub-flow: Ollama detection + model pull
└── ApiKeySetup.svelte              # Sub-flow: API key entry
```

## Screen 1: Welcome

Simple, warm, no jargon.

```
┌─────────────────────────────────────────┐
│                                         │
│              🐾                         │
│                                         │
│         Welcome to PocketPaw            │
│                                         │
│    Your AI that runs on your machine.   │
│    Private. Secure. Yours.              │
│                                         │
│         [ Get Started → ]               │
│                                         │
└─────────────────────────────────────────┘
```

- Centered layout, full-page
- PocketPaw logo/icon at top
- One button: "Get Started"
- No sign-up, no email, no account

## Screen 2: Choose Your AI

Two clear paths presented as cards:

**Card A: "Free & Local"**
- Uses Ollama
- Runs 100% on your machine
- No account needed
- Button: "Set up →"
- Clicking opens `OllamaSetup.svelte` sub-flow

**Card B: "Powerful"**
- Uses Claude / OpenAI / Google
- Smarter, needs an API key
- $3-15/mo typical
- Button: "Set up →"
- Clicking opens `ApiKeySetup.svelte` sub-flow

**Footer link**: "More options: OpenAI, Google, Groq..." (expand to show other providers)

### OllamaSetup.svelte

1. Check if Ollama is reachable (GET `http://localhost:11434/api/tags`)
2. If **yes**: show available models, let user pick one (or pull default `llama3.2`)
3. If **no**: show instructions to install Ollama with link, "Check Again" button
4. Progress bar during model download
5. On success: save settings (`agent_backend: "claude_agent_sdk"`, `llm_provider: "ollama"`, `ollama_model: "<chosen>"`)
6. Auto-advance to Screen 3

### ApiKeySetup.svelte

1. Provider selector: Anthropic (default), OpenAI, Google
2. Single text input: "Paste your API key"
3. Link: "Where do I get one? →" (opens provider's API key page)
4. Trust message: "Your key is encrypted and stored locally. We never see it."
5. "Continue →" button validates key format, saves via `settingsStore.saveApiKey()`
6. On success: advance to Screen 3

## Screen 3: You're All Set

```
┌─────────────────────────────────────────┐
│                                         │
│              🐾                         │
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

- Shows suggested first messages (clickable — clicking navigates to chat with that pre-filled)
- "Start Chatting →" navigates to `/` (main chat)
- Marks onboarding as complete (save flag to localStorage or settings)

## State Management

```typescript
// In OnboardingWizard.svelte
let currentStep = $state(1); // 1, 2, or 3
let selectedProvider = $state<"ollama" | "api" | null>(null);

function next() { currentStep++; }
function back() { currentStep--; }
```

## Routing Guard

In `+layout.svelte` or a layout load function:
- Check if onboarding is complete (localStorage flag: `pocketpaw_onboarded`)
- If not: redirect to `/onboarding`
- If yes: allow normal routing
- The `/onboarding` route itself should always be accessible

## Acceptance Criteria

- [ ] 3-step wizard flow works end to end
- [ ] Ollama detection works (check localhost:11434)
- [ ] API key entry validates and saves
- [ ] Onboarding completion flag prevents re-showing
- [ ] "Start Chatting" navigates to main chat
- [ ] Suggested messages on Screen 3 are clickable
- [ ] Back button works on Screen 2 sub-flows
- [ ] Clean, centered, minimal design using shadcn Card components
- [ ] Step indicator (dots or progress) shows current position

## Notes

- This is the first thing non-tech users see. It must be dead simple.
- Don't show technical details (port numbers, config paths, model parameters).
- The Ollama check should be non-blocking — if it fails, just show install instructions.
- API key validation: just check format (starts with `sk-ant-` for Anthropic, `sk-` for OpenAI). Don't make a test API call during onboarding.

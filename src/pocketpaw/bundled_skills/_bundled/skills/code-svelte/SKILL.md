---
name: code-svelte
description: |
  Plain Svelte 5 + Vite + TypeScript projects — components mounted into one
  page, NOT SvelteKit. Invoke when the work is Svelte: "build a Svelte
  component", "why isn't my $state updating", "$derived vs $effect", "pass a
  snippet to a child", "bind a value both ways", "share state between
  components", "this prop isn't reactive", "add a transition". Covers runes
  ($state, $derived, $effect, $props, $bindable), snippets and {@render},
  event props, .svelte.ts state modules, context, attachments, and scoped
  styling. There is NO SvelteKit in this project: no file-based routing, no
  load functions, no form actions, no +page/+layout files, no adapters, no
  $app imports, no server rendering. This is not a dashboard, not a pocket,
  and it produces no ui-spec.
---

# Svelte 5 + Vite + TypeScript

A plain Svelte app: one HTML page, one mount call, components all the way down.
SvelteKit is not installed and none of its conventions exist here.

## What is actually in the project

Scaffolded from `create-vite@8.3.0`, template `template-svelte-ts`:

```
index.html               <div id="app">
src/main.ts              mount(App, { target: document.getElementById('app')! })
src/App.svelte           root component
src/lib/Counter.svelte   the one child component
src/app.css              global styles, imported by main.ts
src/assets/svelte.svg
public/vite.svg          served at /vite.svg, never processed
svelte.config.js         { preprocess: vitePreprocess() }
vite.config.ts           defineConfig({ plugins: [svelte()] })
tsconfig.app.json        extends @tsconfig/svelte, checkJs on
tsconfig.node.json
```

Verified versions: svelte `^5.45.2`, vite `^7.3.1`,
`@sveltejs/vite-plugin-svelte` `^6.2.1`, `svelte-check` `^4.3.4`,
typescript `~5.9.3`.

The app is mounted imperatively:

```ts
import { mount } from 'svelte'
import App from './App.svelte'

const app = mount(App, { target: document.getElementById('app')! })
```

`mount()` — not `new App({ target })`. The class-component constructor is the
Svelte 4 API and throws in Svelte 5 unless legacy compatibility is on.

## Runes

Reactivity is explicit. A plain `let` is a plain variable; declaring it with
`$state` is what makes assignments reactive.

```svelte
<script lang="ts">
  let count = $state(0)
  let user = $state({ name: 'Ada', tags: ['admin'] })

  const doubled = $derived(count * 2)
  const summary = $derived.by(() => {
    const t = user.tags.join(', ')
    return `${user.name} (${t})`
  })
</script>
```

- **`$state`** — deep by default. Objects and arrays are proxied, so
  `user.tags.push('owner')` is reactive. `$state.raw` opts out of that for a
  value you always replace wholesale; `$state.snapshot(x)` produces a plain
  object to hand to code that dislikes proxies (structured clone, JSON, a
  third-party library).
- **`$derived`** — a cached expression. `$derived.by(() => { ... })` when the
  computation needs statements. It re-evaluates lazily and must stay pure.
- **`$effect`** — for synchronizing with things outside Svelte: an observer, a
  timer, a canvas, a subscription. Dependencies are tracked dynamically — it
  depends on exactly what it read. Return a teardown function.
  `$effect.pre` fires before the DOM updates.
- **`$props`** — the component's inputs.
- **`$bindable`** — marks a prop the parent may bind to.
- **`$inspect`** — a development-only logger that re-fires when its argument
  changes deeply.

The rule that prevents most bad Svelte 5: **if you are assigning to state
inside `$effect`, it should almost certainly have been `$derived`.** Effects
that write state create ordering puzzles and re-entrancy loops.

## Props

```svelte
<script lang="ts">
  interface Props {
    label: string
    count?: number
    onselect?: (id: string) => void
    children?: import('svelte').Snippet
  }

  let { label, count = 0, onselect, children, ...rest }: Props = $props()
</script>

<button {...rest} onclick={() => onselect?.(label)}>
  {label} — {count}
  {@render children?.()}
</button>
```

Props are read-only unless declared `$bindable`:

```svelte
<!-- child -->
<script lang="ts">
  let { value = $bindable('') }: { value?: string } = $props()
</script>
<input bind:value />

<!-- parent -->
<TextField bind:value={name} />
```

Prefer callback props over binding. Binding is for genuine two-way form
controls, not as a general upward channel.

## Events are properties

`onclick`, `oninput`, `onsubmit` — plain attributes taking a function. There is
no `on:click` directive in Svelte 5, and `createEventDispatcher` is gone. A
child notifies its parent by calling a function prop.

Modifiers are gone too; write them out:

```svelte
<form onsubmit={(e) => { e.preventDefault(); save() }}>
```

## Snippets replace slots

```svelte
<!-- List.svelte -->
<script lang="ts">
  import type { Snippet } from 'svelte'
  let { items, row }: { items: Item[]; row: Snippet<[Item]> } = $props()
</script>

<ul>
  {#each items as item (item.id)}
    <li>{@render row(item)}</li>
  {/each}
</ul>

<!-- caller -->
<List {items}>
  {#snippet row(item)}
    <strong>{item.name}</strong>
  {/snippet}
</List>
```

Content placed directly between a component's tags arrives as the `children`
snippet. `{@render children?.()}` renders it.

## Shared state lives in `.svelte.ts` modules

Runes work in any file with a `.svelte.ts` (or `.svelte.js`) extension. That is
how state is shared without a store library:

```ts
// src/lib/session.svelte.ts
export const session = $state({ user: null as User | null, loading: false })

export function signOut() {
  session.user = null
}
```

Import `session` anywhere and mutate its properties. Do not export a
destructured primitive — the binding is copied and the reader never updates.
Export the object, or export a getter.

The Svelte 4 store contract still works (`writable`, `$store` auto-subscribe)
and is worth knowing when reading older code, but new code should use runes.

For values scoped to a component subtree, `setContext`/`getContext` from
`svelte`, called during component initialization only.

## Template features worth using

- `{#each items as item (item.id)}` — the keyed form. Unkeyed `each` reuses DOM
  nodes positionally and will smear state across rows after an insert.
- `{#key expr} ... {/key}` — tears down and rebuilds a subtree when `expr`
  changes. This is the clean way to reset a component.
- `{#await promise}` / `:then` / `:catch` — inline async without an effect.
- `class:active={isActive}` and `style:color={c}` directives; `class` also
  accepts an object or array of conditions.
- `{@attach fn}` (Svelte 5.29+) — attaches behaviour to an element, replacing
  `use:action`. The function receives the node, may read reactive state, and may
  return a teardown.
- `transition:`, `in:`, `out:`, `animate:` from `svelte/transition` and
  `svelte/animate`.

## Styling

A `<style>` block in a component is scoped to that component's markup. It does
not reach child components or dynamically inserted HTML; `:global(.selector)`
opts a rule out. Unused selectors are stripped at compile time and reported —
that warning usually means the selector only matches markup a child owns.

Global styles belong in `src/app.css`, imported once from `main.ts`.

## Types and Vite conventions

- Type checking for `.svelte` files comes from `svelte-check`, a devDependency
  in this template; the plain TypeScript compiler does not understand `.svelte`.
- Type props with an `interface Props`; type snippets with `Snippet<[Args]>`;
  type a component value with `Component` from `svelte`.
- `tsconfig.app.json` sets `checkJs: true`, so JavaScript in `.svelte` files is
  type-checked too.
- `import.meta.env.VITE_*` for public configuration — it lands in the bundle, so
  no secrets. `public/` is copied verbatim; imported assets are hashed.
- A path alias needs `resolve.alias` in `vite.config.ts` *and* `paths` in
  `tsconfig.app.json`.

## Routing

There is none. If the app needs multiple views, either branch on a piece of
`$state` or add a small client-side router as a dependency and mount views from
it. (If the project later moves to SvelteKit, routing, data loading, and server
rendering come from the framework instead — none of that applies to this
project as it stands.)

## Common mistakes

- **Writing SvelteKit.** No `+page.svelte`, no `+layout.svelte`, no `load`
  function, no form actions, no `$app/navigation`, `$app/stores`, or
  `$app/environment`, no adapters, no `export const prerender`/`ssr`. Those
  imports do not resolve and those file names mean nothing here.
- **Svelte 4 syntax.** `export let` for props, `$:` reactive statements,
  `on:click`, `createEventDispatcher`, `<slot />`, `new App({ target })`. All
  superseded; several are hard errors under runes.
- **A plain `let` expected to be reactive.** Without `$state` it never triggers
  an update.
- **`$effect` used to derive a value.** Use `$derived`.
- **Destructuring reactive state at module scope.** `const { user } = session`
  copies the value once. Keep the object.
- **Passing `$state` to an external library and hitting proxy complaints.** Send
  `$state.snapshot(value)`.
- **Unkeyed `{#each}` over a list that reorders.** Component state follows the
  index, not the item.
- **A scoped selector targeting a child's markup.** It is stripped as unused;
  use `:global()` or let the child own the style.
- **Effects with no teardown.** Observers, intervals, and listeners leak on
  unmount.
- **Binding as a general upward data path.** Use a callback prop unless it is a
  real two-way control.

## Review checklist

- Every reactive value is declared with `$state`; every derived value with
  `$derived` or `$derived.by`.
- No `$effect` assigns to state that could be derived.
- Every `$effect` that subscribes, observes, or schedules returns a teardown.
- Props come from `$props()` with a typed `Props` interface; only genuine
  two-way controls use `$bindable`.
- Child-to-parent communication is callback props, not dispatched events.
- Composition uses snippets and `{@render}`, not `<slot>`.
- `{#each}` over anything reorderable is keyed on a stable id.
- Shared state lives in a `.svelte.ts` module and is exported as an object or
  getter, never as a destructured primitive.
- No SvelteKit file names, imports, or page options anywhere.
- Nothing secret sits behind a `VITE_` variable.

---
name: code-react
description: |
  React 19 + Vite + TypeScript projects — the client-rendered SPA scaffold,
  not Next.js. Invoke when the work is React inside a Vite project: "build a
  React component", "why is this effect firing twice", "add a form",
  "this state isn't updating", "share state between components", "add a
  route", "typing this prop is fighting me", "fetch data on mount". Covers
  hooks and when not to reach for them, React 19 client APIs (Actions,
  useActionState, useOptimistic, use(), ref as a prop, Context as provider),
  Vite conventions (import.meta.env, assets, aliases), and the strict
  TypeScript settings this template turns on. There is NO server in this
  project: no App Router, no Server Components, no "use server". Those
  belong to the Next.js skill. This is not a dashboard, not a pocket, and
  it produces no ui-spec.
---

# React 19 + Vite + TypeScript

The project is a **client-rendered single-page app**. Everything ships to the
browser; there is no server, no server rendering, and no request lifecycle.
If a piece of advice starts with "on the server", it does not apply here.

## What is actually in the project

Scaffolded from `create-vite@8.3.0`, template `template-react-ts`:

```
index.html            <div id="root"> plus the module script tag
src/main.tsx          createRoot(...).render(<StrictMode><App /></StrictMode>)
src/App.tsx           the one component that exists
src/App.css           component styles
src/index.css         global styles, imported by main.tsx
src/assets/react.svg
public/vite.svg       served at /vite.svg, never processed
vite.config.ts        defineConfig({ plugins: [react()] })
tsconfig.json         a solution file; the real options are in the two below
tsconfig.app.json     src/**, browser libs
tsconfig.node.json    vite.config.ts only
eslint.config.js      flat config
```

Verified versions: react `^19.2.0`, react-dom `^19.2.0`, vite `^7.3.1`,
`@vitejs/plugin-react` `^5.1.1`, typescript `~5.9.3`.

There is **no router, no state library, no data-fetching library, no CSS
framework**. Anything beyond the list above has to be added as a dependency
and wired up by hand. Say so plainly rather than writing imports that will not
resolve.

## Components

Function components only. Three React 19 changes matter day to day:

```tsx
// 1. ref is an ordinary prop — forwardRef is gone
function Field({ label, ref }: { label: string; ref?: React.Ref<HTMLInputElement> }) {
  return <label>{label}<input ref={ref} /></label>
}

// 2. context objects render directly; .Provider is no longer required
<ThemeContext value="dark">{children}</ThemeContext>

// 3. ref callbacks may return a cleanup function
<div ref={(node) => {
  const ro = new ResizeObserver(onResize)
  ro.observe(node)
  return () => ro.disconnect()
}} />
```

## State

Derive, do not synchronize. If a value can be computed from props or other
state, compute it during render:

```tsx
// wrong: a second source of truth that drifts
const [total, setTotal] = useState(0)
useEffect(() => setTotal(items.length), [items])

// right
const total = items.length
```

To reset state when an identity changes, change the `key` instead of writing an
effect:

```tsx
<ProfileForm key={userId} userId={userId} />
```

Reach for `useReducer` when several fields change together under one event.
Reach for context when a value is genuinely ambient (theme, current user);
passing two props down two levels is not a reason to.

## Effects

An effect is for synchronizing with something outside React: a subscription, a
timer, an imperative DOM API, a network request tied to the lifetime of a view.
It is not for transforming data and not for responding to a click — put that in
the event handler.

`<StrictMode>` is on in this template, so in development every effect mounts,
unmounts, and mounts again. An effect that leaks (a subscription with no
teardown, a fetch with no abort) shows up as a double request or a doubled
listener. That is the check working, not a bug to suppress.

```tsx
useEffect(() => {
  const controller = new AbortController()
  fetch(`/api/users/${id}`, { signal: controller.signal })
    .then((r) => r.json())
    .then(setUser)
    .catch((e) => { if (e.name !== 'AbortError') setError(e) })
  return () => controller.abort()
}, [id])
```

## Forms and async work

React 19's Actions work in a client app. `useActionState` owns the pending flag
and the returned error, so no separate `isSubmitting` state is needed:

```tsx
const [error, submit, isPending] = useActionState(
  async (_prev: string | null, formData: FormData) => {
    const res = await saveName(formData.get('name') as string)
    return res.ok ? null : res.message
  },
  null,
)

return (
  <form action={submit}>
    <input name="name" />
    <button disabled={isPending}>Save</button>
    {error && <p role="alert">{error}</p>}
  </form>
)
```

`useFormStatus` reads the enclosing form's pending state from a child component
without prop drilling. `useOptimistic` shows the expected result while the
request is in flight and rolls back on failure.

`use()` unwraps a promise during render, but only if the promise was created
outside render — a promise built inline is a new promise every pass and will
suspend forever:

```tsx
const cache = new Map<string, Promise<User>>()
const loadUser = (id: string) =>
  cache.get(id) ??
  cache.set(id, fetch(`/api/users/${id}`).then(r => r.json())).get(id)!

// wrap the caller in <Suspense>
const user = use(loadUser(id))
```

`use()` also reads context conditionally, which hooks cannot do.

## Document metadata

`<title>`, `<meta>`, and `<link>` rendered anywhere in the tree hoist into
`<head>` in React 19. For a single-page app this covers per-view titles without
a helper library.

## Vite conventions

- Environment values come from `import.meta.env`. Only keys prefixed `VITE_`
  are exposed to client code, and everything exposed is public — no secrets.
  `import.meta.env.DEV` and `.PROD` are booleans.
- Imported assets (`import url from './assets/logo.svg'`) are hashed and
  rewritten. Files in `public/` are copied verbatim and referenced by absolute
  path.
- A path alias needs both halves: `resolve.alias` in `vite.config.ts` (e.g.
  `{ '@': fileURLToPath(new URL('./src', import.meta.url)) }`) *and*
  `"paths": { "@/*": ["src/*"] }` in `tsconfig.app.json`. One without the other
  fails in exactly one place — types or resolution, never both.
- `*.module.css` gives scoped class names with no extra configuration.

## TypeScript settings this template turns on

`tsconfig.app.json` sets `verbatimModuleSyntax`, `erasableSyntaxOnly`,
`noUnusedLocals`, `noUnusedParameters`, and `noUncheckedSideEffectImports`.
Consequences worth knowing before the first type error:

- Type-only imports must say so: `import type { ReactNode } from 'react'`.
  A mixed import that only carries types will fail.
- `erasableSyntaxOnly` bans `enum`, constructor parameter properties, and
  namespaces. Use a union of string literals or `as const` object instead.
- An unused parameter is an error; prefix it with `_` to keep the signature.

## Routing

Not included. If the app needs more than one view, add a router as a real
dependency and mount it in `main.tsx` — do not hand-roll `window.location`
branching and call it routing. Until a router exists, conditional rendering on
a piece of state is the honest option; name it as temporary.

## Common mistakes

- **Writing Next.js.** No `app/` directory, no `"use client"`, no
  `"use server"`, no Server Components, no `next/link` or `next/image`. Every
  component here is a client component and those imports do not resolve.
- **Effects that mirror props into state.** Produces one render of stale data
  and a whole class of "why is it one behind" bugs. Derive instead.
- **Blaming StrictMode for a double request.** The second mount is exposing a
  missing cleanup. Add the abort, keep the check.
- **Missing or lying dependency arrays.** An empty array on an effect that
  closes over `id` captures the first `id` forever.
- **Index as `key` in a reordering list.** State attaches to the wrong row after
  an insert or a sort. Key on a stable id.
- **Mutating state in place.** `items.push(x)` then `setItems(items)` is the
  same reference, so nothing re-renders. Build a new array.
- **A promise created inside render and handed to `use()`.** Suspends forever.
- **Reading a ref during render.** `ref.current` is not reactive and is null on
  the first pass; read it in an effect or a handler.
- **`VITE_`-prefixed secrets.** They are in the bundle. Anything private needs a
  backend this project does not have.

## Review checklist

- Every stateful value has exactly one owner; nothing is derived into state.
- Every effect synchronizes with something external and returns a cleanup when
  it subscribes, observes, times, or fetches.
- Dependency arrays list every reactive value the effect reads.
- Lists key on stable ids, not indices.
- Async UI has a pending path and an error path, not just the happy one.
- Type-only imports use `import type`; no `enum`, no parameter properties.
- No unused locals or parameters left behind.
- No `next/*` import, no `"use client"`, no `"use server"` anywhere.
- New dependencies are actually added to `package.json`, not just imported.
- Nothing secret sits behind a `VITE_` variable.

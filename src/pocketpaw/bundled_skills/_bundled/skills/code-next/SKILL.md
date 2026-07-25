---
name: code-next
description: |
  Next.js 15 App Router projects with TypeScript and Tailwind CSS v4.
  Invoke when the work is Next: "add a page", "why is this a client
  component", "params is a Promise now", "fetch data in a Server
  Component", "write a server action", "add an API route", "my data is
  stale", "set the page title", "protect a route". Covers the Server/Client
  Component boundary, async request APIs, the app/ file conventions, route
  handlers, server actions and revalidation, metadata, next/image and
  next/font, middleware, and Tailwind v4's CSS-first configuration. This is
  the Next 15 line specifically — Cache Components, "use cache", proxy.ts,
  updateTag, and Turbopack-by-default are Next 16 and are NOT available.
  This is not a dashboard, not a pocket, and it produces no ui-spec.
---

# Next.js 15 App Router + TypeScript + Tailwind v4

A full-stack React framework. Most components render on the server; only what
needs interactivity ships to the browser.

## What is actually in the project

Scaffolded from `create-next-app@15.5.20`, template `app-tw/ts`:

```
app/layout.tsx        root layout, Geist + Geist_Mono via next/font/google
app/page.tsx          the / route
app/globals.css       @import "tailwindcss" plus an @theme inline block
postcss.config.mjs    { plugins: ["@tailwindcss/postcss"] }
tsconfig.json         paths: { "@/*": ["./*"] }, moduleResolution "bundler"
next.config.ts · eslint.config.mjs · biome.json · next-env.d.ts · public/*.svg
```

Dependency ranges: `next` ^15.5, `react` / `react-dom` ^19, `tailwindcss` ^4
with `@tailwindcss/postcss` ^4, `typescript` ^5.

Two structural facts that trip people up: there is **no `src/` directory** —
`app/` sits at the root and `@/*` resolves to `./*`, not `./src/*` — and there
is **no `tailwind.config.js`**, because Tailwind v4 is configured in CSS.

## The Server / Client boundary

Every component under `app/` is a Server Component unless the file (or one above
it in the import chain) starts with `"use client"`. Server Components can be
`async` and reach a database directly and send zero JavaScript; they cannot use
hooks, event handlers, or browser APIs. Push the boundary down — a page needing
one interactive control stays a Server Component and imports a client child:

```tsx
// app/products/page.tsx — server. AddToCart carries 'use client' itself.
export default async function Page() {
  const products = await db.product.findMany()
  return products.map((p) => <AddToCart key={p.id} id={p.id} name={p.name} />)
}
```

A Server Component may be passed to a client component as `children` and still
render on the server. Props crossing the boundary must be serializable.

## Request APIs are async in Next 15

The most common source of type errors on this version. `params`,
`searchParams`, `cookies()`, `headers()`, and `draftMode()` return Promises:

```tsx
type Props = {
  params: Promise<{ slug: string }>
  searchParams: Promise<Record<string, string | string[] | undefined>>
}

export default async function Page({ params }: Props) {
  const { slug } = await params
  const session = (await cookies()).get('session')
}
```

Next 15.5 also generates global `PageProps<'/blog/[slug]'>`, `LayoutProps<'/'>`,
and `RouteContext<'/api/items/[id]'>` types — no import needed, covering parallel
slots. In a client component, `use(params)` unwraps the same promise.

## File conventions in `app/`

| File | Role |
|---|---|
| `page.tsx` | makes a segment routable |
| `layout.tsx` | wraps its segment; persists across navigation, never remounts |
| `template.tsx` | like a layout, but remounts on every navigation |
| `loading.tsx` | Suspense fallback for the segment |
| `error.tsx` | error boundary; must be `"use client"` and takes `reset` |
| `not-found.tsx` | rendered by `notFound()` |
| `route.ts` | an HTTP endpoint; cannot coexist with `page.tsx` in one segment |

Folder names drive the URL: `[slug]` dynamic, `[...slug]` catch-all,
`(marketing)` a route group adding no segment, `_lib` a private folder excluded
from routing, `@modal` a parallel route slot.

```ts
// app/api/items/[id]/route.ts
export async function GET(_req: Request, ctx: RouteContext<'/api/items/[id]'>) {
  return Response.json(await getItem((await ctx.params).id))
}
```

## Caching and revalidation

Next 15 flipped the defaults to uncached: `fetch` and GET route handlers are not
cached, and page segments in the client router cache are stale immediately.
Caching is now something you ask for:

```ts
await fetch(url, { cache: 'force-cache' })            // persist it
await fetch(url, { next: { revalidate: 3600 } })      // time-based
await fetch(url, { next: { tags: ['products'] } })    // tag it for later
```

Wrap non-`fetch` work (an ORM call, a filesystem read) in `unstable_cache`.
Per-segment behaviour comes from route segment config — `export const dynamic =
'force-static' | 'force-dynamic'`, `export const revalidate = 60`. Invalidate
from a server action or route handler with `revalidatePath('/blog')` or
`revalidateTag('products')`; here `revalidateTag` takes the tag alone, since the
second `cacheLife` argument is Next 16. React's `cache()` deduplicates a
function within one request, so a layout and a page can both call `getUser()`.

## Server Actions

```tsx
// app/actions.ts
'use server'
export async function createPost(_prev: unknown, formData: FormData) {
  const title = String(formData.get('title') ?? '')
  if (!title) return { error: 'Title is required' }
  await db.post.create({ data: { title } })
  revalidatePath('/posts')
  return { error: null }
}
```

```tsx
'use client' // the caller
const [state, action, pending] = useActionState(createPost, { error: null })
return (
  <form action={action}>
    <input name="title" />
    <button disabled={pending}>Create</button>
    {state.error && <p role="alert">{state.error}</p>}
  </form>
)
```

An action is a public HTTP endpoint the moment it is exported: authorize and
validate inside the action body, because a check in the calling component
protects nothing. `useFormStatus` reads the form's pending state from a child.

## Metadata, images, fonts, links

- `export const metadata: Metadata` for static, `generateMetadata({ params })`
  when it depends on data. `next/image` needs `width` and `height` (or `fill`
  with a positioned parent); remote hosts go in `images.remotePatterns`.
- `next/font/google` self-hosts and exposes CSS variables — already wired for
  Geist and Geist Mono onto `<html>`.
- `next/link` prefetches automatically; `legacyBehavior` with a nested `<a>` is
  deprecated on 15.5. `typedRoutes: true` in `next.config.ts` is stable on 15.5
  and turns a wrong `href` into a compile error.

## Middleware

The file is `middleware.ts` at the project root on this version, exporting a
function named `middleware`. Node.js runtime support went stable in 15.5:

```ts
export const config = { matcher: ['/account/:path*'], runtime: 'nodejs' }

export function middleware(request: NextRequest) {
  return request.cookies.get('session')
    ? NextResponse.next()
    : NextResponse.redirect(new URL('/login', request.url))
}
```

Keep it thin — it sits in front of every matched request, and real
authorization belongs in the data layer too.

## Tailwind v4

Configuration is CSS-first: `app/globals.css` opens with `@import "tailwindcss"`
and declares tokens in an `@theme` block, where each token becomes a utility.
`--color-brand: oklch(0.62 0.19 264)` yields `bg-brand`, `text-brand`,
`border-brand`. There is no config file and no `content` array. `@apply` exists
but is rarely the right answer.

## Not available on Next 15

These belong to Next 16 and will fail here:

- Cache Components and `"use cache"` as stable, plus the `cacheComponents` flag.
  (`experimental.dynamicIO` exists on 15 but is experimental — do not build on it.)
- `proxy.ts` as a replacement for `middleware.ts`.
- `updateTag()` and `refresh()` from `next/cache`, and the two-argument
  `revalidateTag(tag, profile)`.
- `reactCompiler` as a stable top-level config option, and Turbopack as the
  default bundler with top-level `turbopack` config.
- React 19.2-only APIs: `<Activity>`, `useEffectEvent`, View Transitions.

## Common mistakes

- **`"use client"` on a page** because one child needed a hook. It opts the whole
  subtree into the client bundle. Move the boundary down.
- **Reading `params` or `cookies()` synchronously.** They are Promises on 15.
- **An event handler passed from a Server Component to a client one.** Functions
  do not serialize across the boundary.
- **A secret read in a file that ends up client-side.** Anything unprefixed by
  `NEXT_PUBLIC_` is server-only and unreadable from a `"use client"` file.
- **Expecting Next 14 caching.** Stale data usually means a missing
  `revalidatePath`/`revalidateTag`; fresh-when-you-wanted-cached means a missing
  `force-cache`.
- **A server action with no authorization check.** It is a public endpoint.
- **`useState` or `useEffect` in a Server Component.** They do not exist there.
- **`@/src/...` paths or `tailwind.config.js`.** Neither exists here.
- **`route.ts` beside `page.tsx` in one folder**, or `error.tsx` without
  `"use client"`. Both are hard failures.

## Review checklist

- `"use client"` appears only on leaves that need interactivity.
- Every `params`, `searchParams`, `cookies()`, `headers()` access is awaited,
  and props crossing the server/client boundary are serializable.
- Data-fetching calls state their caching intent explicitly, and every mutation
  is followed by `revalidatePath` or `revalidateTag`.
- Every server action validates input and checks authorization itself, and
  secrets are unprefixed and read only in server code.
- Dynamic pages export `metadata` or `generateMetadata`; `next/image` has
  dimensions and remote hosts are in `images.remotePatterns`.
- Segments that can fail have `error.tsx`; slow segments have `loading.tsx`.
- No Next 16 API (`"use cache"`, `proxy.ts`, `updateTag`, two-argument
  `revalidateTag`) appears anywhere.

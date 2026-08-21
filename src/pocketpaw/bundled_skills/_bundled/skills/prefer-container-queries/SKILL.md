---
name: prefer-container-queries
description: "Enforce Tailwind container queries over viewport breakpoints for responsive components. Use when writing or reviewing responsive Tailwind code, when a component needs to adapt to its available space (cards, sidebars, lists, panels, anything reused in different layouts), or when migrating sm:/md:/lg: classes to @container variants."
---

# Prefer Container Queries

> **PocketPaw note.** Vendored from
> [flornkm/skills](https://github.com/flornkm/skills) (MIT). It applies wherever
> Tailwind is actually in the pipeline:
>
> - **Generated Paw Sites** get Tailwind v4 (`tailwindcss` + `@tailwindcss/vite`
>   in the generated `package.json`, `@import 'tailwindcss'` in `app.css`), so
>   container queries are in core with no plugin. Note the site skills otherwise
>   lean on a scoped `<style>` block per section; container queries are worth
>   reaching for on the reusable pieces (cards, feature tiles, testimonial
>   blocks), not as a reason to rewrite a hand-authored section in utilities.
> - **`code-next` projects** ship Tailwind v4 too.
> - **App UI** (paw-enterprise, ripple) is Tailwind v4.
>
> Everything below is unmodified upstream text.


Components should respond to the space they are in, not to the viewport. A card that lives in a sidebar has maybe 300px of width even on a huge screen, and a viewport breakpoint like `md:flex-row` will get that wrong every time. Container queries fix this: the component asks "how wide am I?" instead of "how wide is the window?".

When writing responsive Tailwind, default to container queries. Viewport breakpoints are the exception, not the rule.

## How it works

Mark the parent as a container, then use `@`-prefixed variants on its children:

```html
<div class="@container">
  <div class="flex flex-col @md:flex-row @md:gap-6">
    <img class="w-full @md:w-48" />
    <div class="@md:flex-1">...</div>
  </div>
</div>
```

`@md:` here means "when the container is at least 28rem wide", regardless of viewport size. This card works in a sidebar, a modal, and a full-width grid without changing a single class.

Two rules that trip people up:

1. The `@container` class goes on the parent. The `@md:` variants go on descendants. An element cannot query its own size.
2. Container variants respond to the nearest ancestor with `@container`. If styles are not applying, check which container you are actually querying.

## Variants

Tailwind v4 ships container queries in core. The sizes:

| Variant | Min width |
| --- | --- |
| `@3xs` | 16rem (256px) |
| `@2xs` | 18rem (288px) |
| `@xs` | 20rem (320px) |
| `@sm` | 24rem (384px) |
| `@md` | 28rem (448px) |
| `@lg` | 32rem (512px) |
| `@xl` | 36rem (576px) |
| `@2xl` | 42rem (672px) |
| `@3xl` | 48rem (768px) |
| `@4xl` | 56rem (896px) |
| `@5xl` | 64rem (1024px) |
| `@6xl` | 72rem (1152px) |
| `@7xl` | 80rem (1280px) |

Also available:

- `@max-md:` styles below a container size.
- `@min-[475px]:` arbitrary values when the scale does not fit.
- `@container/sidebar` plus `@md/sidebar:flex-row` to name a container and query it from deeper in the tree, past other containers.

The full docs for this can be found [here](https://tailwindcss.com/docs/responsive-design#container-queries).

On Tailwind v3, the same syntax needs the `@tailwindcss/container-queries` plugin. Check the Tailwind version before writing classes.

## When viewport breakpoints are still right

Do not convert these:

- Page-level layout. The overall grid, whether the sidebar exists at all, header and navigation behavior. These genuinely depend on the viewport.
- Fixed or sticky elements positioned relative to the viewport, like a bottom bar that becomes a side rail.
- Global typography scale tied to screen size.

Everything inside those layout regions, meaning cards, forms, media objects, stat blocks, table-to-list switches, should use container queries.

## Migrating existing code

1. Find the component's responsive classes (`sm:`, `md:`, `lg:`).
2. Add `@container` to the component's root or the wrapper that owns the available space.
3. Replace viewport variants with the container variant that matches the actual width where the layout should change. Do not map `md:` to `@md:` blindly; `md` is 768px of viewport, `@md` is 448px of container. Resize the container, not the window, to find the real breakpoint.
4. Test the component in its narrowest real context (sidebar, drawer, small grid cell), not just at mobile viewport widths.

## Mistakes to catch in review

- `md:flex-row` on a component that is rendered inside a sidebar or modal. It will stay stacked or break depending on the viewport, not its actual space.
- `@md:` variants used with no `@container` ancestor. They silently never apply.
- `@container` and a `@md:` variant on the same element. The element cannot query itself; move the variant to a child or the container class to the parent.
- A component that only looks right at the exact spot it was built for. If moving it to a different column breaks it, it is viewport-coupled.

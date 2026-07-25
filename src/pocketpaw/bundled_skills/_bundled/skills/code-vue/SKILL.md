---
name: code-vue
description: |
  Vue 3 + Vite + TypeScript projects — the flat single-file-component
  scaffold, with no router and no store bundled. Invoke when the work is
  Vue: "build a Vue component", "why isn't this ref updating", "v-model on
  a custom component", "extract a composable", "add a page", "props are
  losing reactivity", "watch is firing too often", "scoped styles aren't
  applying". Covers <script setup>, refs vs reactive, computed and watchers,
  reactive props destructure, defineModel, useTemplateRef, composables,
  slots, provide/inject, scoped styling, and the strict TypeScript settings
  this template turns on. Vue Router and Pinia are NOT installed here; this
  skill frames them as things to add, not things present. This is not a
  dashboard, not a pocket, and it produces no ui-spec.
---

# Vue 3 + Vite + TypeScript

Single-file components compiled by Vite, mounted into one page. There is no
router, no store, no server rendering, and no data layer until one is added.

## What is actually in the project

Scaffolded from `create-vite@8.3.0`, template `template-vue-ts`. This is
deliberately the flat Vite template, not `create-vue` — which is why nothing
optional is preinstalled:

```
index.html               <div id="app">
src/main.ts              createApp(App).mount('#app')
src/App.vue              root component
src/components/HelloWorld.vue
src/style.css            global styles, imported by main.ts
public/vite.svg          served at /vite.svg, never processed
vite.config.ts           defineConfig({ plugins: [vue()] })
tsconfig.app.json        extends @vue/tsconfig/tsconfig.dom.json
src/assets/vue.svg · tsconfig.json · tsconfig.node.json
```

Verified versions: vue `^3.5.25`, vite `^7.3.1`, `@vitejs/plugin-vue` `^6.0.2`,
`vue-tsc` `^3.1.5`, typescript `~5.9.3`, `@vue/tsconfig` `^0.8.1`.

Vue 3.5 matters for specifics below — reactive props destructure, `useId`,
`useTemplateRef`, and `onWatcherCleanup` all landed there and are stable.

## Component shape

`<script setup lang="ts">` for everything. Top-level bindings are exposed to
the template automatically; there is no `return` and no `components:` block —
an imported component is usable by name.

```vue
<script setup lang="ts">
import { ref, computed } from 'vue'
import ItemRow from './ItemRow.vue'

const { items, emptyLabel = 'Nothing yet' } = defineProps<{
  items: Item[]
  emptyLabel?: string
}>()

const query = ref('')
const visible = computed(() => items.filter((i) => i.name.includes(query.value)))
</script>

<template>
  <input v-model="query" />
  <p v-if="!visible.length">{{ emptyLabel }}</p>
  <ItemRow v-for="item in visible" :key="item.id" :item="item" />
</template>
```

Destructuring `defineProps` keeps reactivity as of 3.5 and a default is plain
JavaScript syntax — `withDefaults` is gone. The catch: a destructured prop is
reactive only where the compiler sees it, so handing one to a composable or a
`watch` source needs a getter (`() => items`).

## Reactivity

- `ref()` for everything by default. `.value` in script, unwrapped in template.
- `reactive()` only for an object you never reassign. It cannot hold a
  primitive, and destructuring it severs reactivity permanently.
- `shallowRef()` for large payloads you always replace wholesale — no deep proxy.
- `computed()` for derived values: cached, and must stay pure.

```ts
const state = reactive({ count: 0 })
const { count } = state       // dead number, never updates
const count = toRef(state, 'count')  // live
```

## Watchers

`watch` for a named source, `watchEffect` when the dependencies are obvious
from the body. Both need `{ flush: 'post' }` to observe the updated DOM.
Register teardown with `onWatcherCleanup` so a stale request cannot land after
a newer one:

```ts
watch(id, async (newId) => {
  const controller = new AbortController()
  onWatcherCleanup(() => controller.abort())
  results.value = await load(newId, controller.signal)
})
```

A watcher that only computes a value should have been a `computed`. Watchers
are for side effects.

## Two-way binding

`defineModel()` replaces the `modelValue` prop plus `update:modelValue` emit:

```vue
<script setup lang="ts">
const model = defineModel<string>({ required: true })
const size = defineModel<'sm' | 'lg'>('size', { default: 'sm' })
</script>

<template><input v-model="model" /></template>
```

The parent writes `<TextField v-model="name" v-model:size="size" />`. For
anything else, `defineEmits<{ submit: [value: string] }>()` and call the emitter.

## Template refs

`useTemplateRef('name')` binds by the string in the template, so it works for
dynamic and conditional elements:

```vue
<script setup lang="ts">
const input = useTemplateRef<HTMLInputElement>('field')
onMounted(() => input.value?.focus())
</script>

<template><input ref="field" /></template>
```

## Composables

A composable is a `useX` function that calls reactive APIs and returns refs —
the unit of reuse. Extract one when the same state and its watchers appear twice.

```ts
// src/composables/useOnline.ts
export function useOnline() {
  const online = ref(navigator.onLine)
  const sync = () => (online.value = navigator.onLine)
  onMounted(() => addEventListener('online', sync))
  onUnmounted(() => removeEventListener('online', sync))
  return { online }
}
```

Call composables synchronously at the top of `<script setup>`. Calling one
inside a callback, a conditional, or after an `await` detaches it from the
component instance, and its lifecycle hooks silently never fire.

## Slots, provide/inject, ids

- Slots for layout composition: `<slot name="footer" :item="item" />`, consumed
  as `<template #footer="{ item }">`.
- `provide`/`inject` for ambient values across depth. Type the key with
  `InjectionKey<T>` so the injected value is not `unknown`.
- `useId()` for stable `id`/`aria-describedby` pairs instead of a counter.

## Styling

`<style scoped>` rewrites selectors with a data attribute and does not reach
into a child component — use `:deep(.child-class)` for that, `:global()` to opt
out entirely, `<style module>` for a `$style` object. `v-bind()` inside a style
block wires reactive state to a CSS custom property.

## TypeScript settings this template turns on

`tsconfig.app.json` extends `@vue/tsconfig/tsconfig.dom.json` and adds
`strict`, `noUnusedLocals`, `noUnusedParameters`, `erasableSyntaxOnly`,
`noFallthroughCasesInSwitch`, `noUncheckedSideEffectImports`.

- `erasableSyntaxOnly` bans `enum`, constructor parameter properties, and
  namespaces. Use a string-literal union or an `as const` object.
- Type-only imports need `import type`.
- Types inside `defineProps<T>()` and `defineEmits<T>()` are compiled, so they
  can only reference types resolvable in that file.
- Type checking for `.vue` files comes from `vue-tsc`, a devDependency in this
  template; the plain TypeScript compiler does not understand SFCs.

## Vite conventions

`import.meta.env.VITE_*` for public configuration — everything exposed is in the
bundle, so no secrets. Imported assets get hashed; `public/` is copied verbatim.
A path alias needs both `resolve.alias` in `vite.config.ts` and `paths` in
`tsconfig.app.json`; one without the other fails in exactly one place.

## What you would add

Neither exists in the project. Both must be added as real dependencies and
registered on the app instance before any import of them resolves.

- **Routing** — Vue Router. Until it is added there is no `<RouterView>`,
  `<RouterLink>`, `useRoute`, or `useRouter`; a `v-if` on state is the honest
  stand-in, and should be named as temporary.
- **Shared state** — Pinia. For a small app a module exporting a `reactive()`
  object is often enough and needs nothing added.

## Common mistakes

- **Assuming a router or store is present.** Importing `vue-router` or `pinia`
  in this project fails to resolve. Say what needs adding first.
- **Destructuring `reactive()`.** Yields plain values. Use `toRefs`, or keep the
  object whole.
- **Forgetting `.value` in script, or writing it in a template.** The template
  unwraps refs; the script does not.
- **Mutating a prop.** Props are readonly. Emit, or use `defineModel`.
- **Passing a destructured prop straight to `watch`.** Pass `() => prop`.
- **A `watch` that only derives a value.** That is a `computed`.
- **`v-if` and `v-for` on the same element.** `v-if` wins on precedence and is
  evaluated before the loop variable exists. Wrap in `<template v-for>`.
- **Index as `:key`.** Component state binds to the wrong row after a reorder.
- **Calling a composable after `await`.** It loses the active instance and its
  lifecycle hooks never fire.
- **Reading the DOM inside `watch` without `flush: 'post'`.** The DOM is still
  the previous render.

## Review checklist

- Derived values are `computed`, not watchers writing into refs.
- Every watcher that starts async work cleans it up with `onWatcherCleanup`.
- Props are read-only; two-way binding goes through `defineModel` or an emit.
- Composables are called synchronously at setup top level.
- `v-for` keys on a stable id, never an index.
- No `v-if` sharing an element with `v-for`.
- Injected values are typed with an `InjectionKey`.
- Type-only imports use `import type`; no `enum`, no parameter properties.
- No import of a package that is not in `package.json`.
- Nothing secret sits behind a `VITE_` variable.

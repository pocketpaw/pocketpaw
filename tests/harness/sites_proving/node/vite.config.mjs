// vite.config.mjs — SSR build config for the once-built site renderer.
// Created for SG-1 (sites proving harness). Copied into the gitignored build
// dir by build.mjs, which is where `bunx vite build` actually runs.
//
// WHAT: a plain Vite SSR library build (NOT SvelteKit). It compiles
// src/entry.js -> dist/entry.js with every `.svelte` file generated for the
// server, so `svelte/server`'s render() can run it under bare Node.
//
// WHY no SvelteKit and no Tailwind plugin: SvelteKit is what makes the current
// publish path per-site — it wants a project on disk, a route tree, and an
// adapter. The proving harness needs only the component, so it drops Kit
// entirely. Tailwind utility generation is deliberately OUT of scope for SG-1
// (the harness ships ripple's own theme.css/styles.css as bundle assets); the
// utility pipeline is a later slice.
//
// PAW_SSR_NO_EXTERNAL=all bundles every dependency into one self-contained file
// (no node_modules needed at render time). Anything else falls back to the
// curated list lifted VERBATIM from paw-sites/templates/vite.config.ts.tmpl —
// those seven entries encode SSR fixes that were expensive to find (raw
// `.svelte` source in re-exported libs, the @xyflow/system devWarn split,
// svelte-toolbelt's runes-in-.svelte.js). build.mjs records which shape won.
import { svelte, vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';

const CURATED_NO_EXTERNAL = [
  '@ripple-ui/svelte',
  'bits-ui',
  'layerchart',
  '@lucide/svelte',
  '@xyflow/svelte',
  '@xyflow/system',
  'svelte-toolbelt',
];

const bundleEverything = process.env.PAW_SSR_NO_EXTERNAL === 'all';

export default defineConfig({
  plugins: [svelte({ preprocess: vitePreprocess() })],
  ssr: {
    noExternal: bundleEverything ? true : CURATED_NO_EXTERNAL,
  },
  build: {
    ssr: 'src/entry.js',
    outDir: 'dist',
    emptyOutDir: true,
    minify: false,
    rollupOptions: {
      output: { format: 'esm', entryFileNames: 'entry.js' },
    },
  },
});

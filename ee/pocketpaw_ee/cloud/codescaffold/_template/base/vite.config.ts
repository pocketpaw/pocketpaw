import tailwindcss from '@tailwindcss/vite';
import adapter from '@sveltejs/adapter-cloudflare';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [
		tailwindcss(),
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) => filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},
			// Bindings in `platform.env` (D1 `DB`, secrets, etc.) are emulated locally
			// from wrangler.jsonc during `vite dev`/`preview`. `persist` keeps the
			// local D1 data between restarts under `.wrangler/state`.
			adapter: adapter({ platformProxy: { persist: true } }),
			typescript: {
				config: (config) => {
					config.include.push('../drizzle.config.ts');
				}
			}
		})
	]
});

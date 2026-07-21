import type { Handle } from '@sveltejs/kit';
import { svelteKitHandler } from 'better-auth/svelte-kit';
import { building } from '$app/environment';
import { createAuth } from './index';

/**
 * Auth request handle. Mounts Better Auth's endpoints (under `/api/auth/*`) and
 * loads the current session/user into `event.locals`. Registered in
 * `hooks.server.ts` via `sequence()` at the `@recipe:handlers` anchor.
 */
export const authHandle: Handle = async ({ event, resolve }) => {
	const auth = createAuth(event.platform!.env);
	const session = await auth.api.getSession({ headers: event.request.headers });
	event.locals.session = session?.session ?? null;
	event.locals.user = session?.user ?? null;
	return svelteKitHandler({ event, resolve, auth, building });
};

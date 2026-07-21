import { type Handle } from '@sveltejs/kit';
import { sequence } from '@sveltejs/kit/hooks';
// @recipe:imports

/**
 * Base request handle.
 *
 * The base template does nothing here beyond resolving the request. Feature
 * recipes register their own handles (e.g. auth session loading) by:
 *   1. adding an import at the `@recipe:imports` anchor above, and
 *   2. adding the handle to the `sequence(...)` call at the `@recipe:handlers`
 *      anchor below.
 * Handles run left-to-right.
 */
const base: Handle = async ({ event, resolve }) => {
	return resolve(event);
};

export const handle: Handle = sequence(
	base,
	// @recipe:handlers
);

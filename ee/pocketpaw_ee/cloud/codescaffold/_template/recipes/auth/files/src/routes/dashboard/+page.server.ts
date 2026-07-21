import { redirect } from '@sveltejs/kit';
import type { PageServerLoad } from './$types';

/** Protected route: redirect to sign-in unless a session is present. */
export const load: PageServerLoad = async ({ locals }) => {
	if (!locals.user) redirect(303, '/sign-in');
	return { user: locals.user };
};

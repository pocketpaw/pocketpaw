import { fail, redirect } from '@sveltejs/kit';
import * as v from 'valibot';
import { createAuth } from '$lib/server/auth';
import { signInSchema } from '$lib/validations/auth';
import type { Actions, PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals }) => {
	if (locals.user) redirect(303, '/dashboard');
	return {};
};

export const actions: Actions = {
	default: async (event) => {
		const raw = Object.fromEntries(await event.request.formData());
		const parsed = v.safeParse(signInSchema, raw);
		if (!parsed.success) {
			return fail(400, { error: 'Enter a valid email and password.', email: String(raw.email ?? '') });
		}

		const auth = createAuth(event.platform!.env);
		try {
			await auth.api.signInEmail({ body: parsed.output, headers: event.request.headers });
		} catch {
			return fail(400, { error: 'Invalid email or password.', email: parsed.output.email });
		}

		redirect(303, '/dashboard');
	}
};

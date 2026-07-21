import { fail, redirect } from '@sveltejs/kit';
import * as v from 'valibot';
import { createAuth } from '$lib/server/auth';
import { signUpSchema } from '$lib/validations/auth';
import type { Actions, PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals }) => {
	if (locals.user) redirect(303, '/dashboard');
	return {};
};

export const actions: Actions = {
	default: async (event) => {
		const raw = Object.fromEntries(await event.request.formData());
		const parsed = v.safeParse(signUpSchema, raw);
		if (!parsed.success) {
			const issue = parsed.issues[0]?.message ?? 'Please check your details.';
			return fail(400, { error: issue, name: String(raw.name ?? ''), email: String(raw.email ?? '') });
		}

		const auth = createAuth(event.platform!.env);
		try {
			await auth.api.signUpEmail({ body: parsed.output, headers: event.request.headers });
		} catch {
			return fail(400, {
				error: 'Could not create the account. The email may already be in use.',
				name: parsed.output.name,
				email: parsed.output.email
			});
		}

		redirect(303, '/dashboard');
	}
};

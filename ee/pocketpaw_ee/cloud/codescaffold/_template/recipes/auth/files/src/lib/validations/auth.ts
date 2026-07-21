import * as v from 'valibot';

export const signInSchema = v.object({
	email: v.pipe(v.string(), v.trim(), v.email('Enter a valid email address.')),
	password: v.pipe(v.string(), v.minLength(8, 'Password must be at least 8 characters.'))
});

export const signUpSchema = v.object({
	name: v.pipe(v.string(), v.trim(), v.minLength(1, 'Enter your name.')),
	email: v.pipe(v.string(), v.trim(), v.email('Enter a valid email address.')),
	password: v.pipe(v.string(), v.minLength(8, 'Password must be at least 8 characters.'))
});

export type SignInInput = v.InferOutput<typeof signInSchema>;
export type SignUpInput = v.InferOutput<typeof signUpSchema>;

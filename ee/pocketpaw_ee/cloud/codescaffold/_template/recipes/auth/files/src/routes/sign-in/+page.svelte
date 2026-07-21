<script lang="ts">
	import { enhance } from '$app/forms';
	import * as Card from '$lib/components/ui/card';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import { Button } from '$lib/components/ui/button';
	import type { ActionData } from './$types';

	let { form }: { form: ActionData } = $props();
</script>

<div class="mx-auto max-w-sm">
	<Card.Root>
		<Card.Header>
			<Card.Title>Sign in</Card.Title>
			<Card.Description>Welcome back.</Card.Description>
		</Card.Header>
		<Card.Content>
			<form method="POST" use:enhance class="flex flex-col gap-4">
				{#if form?.error}
					<p class="text-sm text-destructive">{form.error}</p>
				{/if}
				<div class="flex flex-col gap-2">
					<Label for="email">Email</Label>
					<Input id="email" name="email" type="email" value={form?.email ?? ''} required />
				</div>
				<div class="flex flex-col gap-2">
					<Label for="password">Password</Label>
					<Input id="password" name="password" type="password" required />
				</div>
				<Button type="submit">Sign in</Button>
			</form>
		</Card.Content>
		<Card.Footer>
			<p class="text-sm text-muted-foreground">
				No account? <a class="underline" href="/sign-up">Sign up</a>
			</p>
		</Card.Footer>
	</Card.Root>
</div>

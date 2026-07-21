<script lang="ts">
	import { goto, invalidateAll } from '$app/navigation';
	import * as Card from '$lib/components/ui/card';
	import { Button } from '$lib/components/ui/button';
	import { authClient } from '$lib/client/auth';
	import type { PageProps } from './$types';

	let { data }: PageProps = $props();
	let signingOut = $state(false);

	async function signOut() {
		signingOut = true;
		await authClient.signOut();
		await invalidateAll();
		await goto('/sign-in');
	}
</script>

<div class="mx-auto max-w-md">
	<Card.Root>
		<Card.Header>
			<Card.Title>Dashboard</Card.Title>
			<Card.Description>A protected route — you are signed in.</Card.Description>
		</Card.Header>
		<Card.Content class="space-y-1 text-sm">
			<p><span class="text-muted-foreground">Name:</span> {data.user.name}</p>
			<p><span class="text-muted-foreground">Email:</span> {data.user.email}</p>
		</Card.Content>
		<Card.Footer>
			<Button variant="outline" onclick={signOut} disabled={signingOut}>
				{signingOut ? 'Signing out…' : 'Sign out'}
			</Button>
		</Card.Footer>
	</Card.Root>
</div>

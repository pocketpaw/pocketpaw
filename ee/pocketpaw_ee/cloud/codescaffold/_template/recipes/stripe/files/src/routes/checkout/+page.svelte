<script lang="ts">
	import * as Card from '$lib/components/ui/card';
	import { Button } from '$lib/components/ui/button';

	let loading = $state(false);
	let errorMessage = $state<string | null>(null);

	async function buy() {
		loading = true;
		errorMessage = null;
		try {
			const res = await fetch('/api/checkout', { method: 'POST' });
			if (!res.ok) throw new Error('Could not start checkout.');
			const { url } = (await res.json()) as { url: string | null };
			if (url) {
				window.location.href = url;
				return;
			}
			throw new Error('No checkout URL returned.');
		} catch (e) {
			errorMessage = e instanceof Error ? e.message : 'Something went wrong.';
			loading = false;
		}
	}
</script>

<div class="mx-auto max-w-md">
	<Card.Root>
		<Card.Header>
			<Card.Title>Demo product</Card.Title>
			<Card.Description>A one-time $10.00 payment via Stripe Checkout.</Card.Description>
		</Card.Header>
		<Card.Content>
			{#if errorMessage}
				<p class="text-sm text-destructive">{errorMessage}</p>
			{/if}
		</Card.Content>
		<Card.Footer>
			<Button onclick={buy} disabled={loading}>
				{loading ? 'Redirecting…' : 'Buy — $10.00'}
			</Button>
		</Card.Footer>
	</Card.Root>
</div>

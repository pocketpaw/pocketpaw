<script lang="ts">
	import * as Card from '$lib/components/ui/card';
	import { buttonVariants } from '$lib/components/ui/button';
</script>

<section class="flex flex-col gap-8">
	<div class="space-y-3">
		<h1 class="text-3xl font-bold tracking-tight">Composable SvelteKit template</h1>
		<p class="max-w-prose text-muted-foreground">
			A minimal, production-lean SvelteKit base for Cloudflare Workers + D1. Compose features
			(auth, payments, …) by applying one-shot recipes — the output is plain SvelteKit source you own.
		</p>
	</div>

	<Card.Root class="max-w-md">
		<Card.Header>
			<Card.Title>Deployed to the edge</Card.Title>
			<Card.Description>
				Cloudflare Workers via <code>@sveltejs/adapter-cloudflare</code>, with a per-tenant D1
				database bound as <code>platform.env.DB</code>.
			</Card.Description>
		</Card.Header>
		<Card.Footer>
			<a href="https://svelte.dev/docs/kit" class={buttonVariants({ variant: 'outline' })}>
				Read the SvelteKit docs
			</a>
		</Card.Footer>
	</Card.Root>
</section>

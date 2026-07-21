<script lang="ts">
	import * as Card from '$lib/components/ui/card';
	import { buttonVariants } from '$lib/components/ui/button';
</script>

<div class="mx-auto max-w-md">
	<Card.Root>
		<Card.Header>
			<Card.Title>Payment successful</Card.Title>
			<Card.Description>
				Thanks for your purchase. Your order is confirmed once Stripe delivers the
				<code>checkout.session.completed</code> webhook.
			</Card.Description>
		</Card.Header>
		<Card.Footer>
			<a href="/" class={buttonVariants({ variant: 'outline' })}>Back to home</a>
		</Card.Footer>
	</Card.Root>
</div>

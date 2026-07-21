<script lang="ts">
	import './layout.css';
	import favicon from '$lib/assets/favicon.svg';
	import Nav from '$lib/components/nav.svelte';

	let { children } = $props();
</script>

<svelte:head><link rel="icon" href={favicon} /></svelte:head>

<div class="min-h-screen bg-background text-foreground">
	<Nav />
	<main class="mx-auto max-w-5xl px-4 py-10">
		{@render children()}
	</main>
</div>

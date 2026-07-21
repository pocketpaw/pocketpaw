<script lang="ts">
	import { page } from '$app/state';
	import { cn } from '$lib/utils';

	// Primary navigation links. Recipes append their own entries at the
	// `@recipe:nav` anchor below. Keep the trailing comma on the last base entry
	// so appended entries stay syntactically valid.
	const links: { href: string; label: string }[] = [
		{ href: '/', label: 'Home' },
		// @recipe:nav
	];
</script>

<nav class="border-b border-border">
	<div class="mx-auto flex h-14 max-w-5xl items-center gap-6 px-4">
		<a href="/" class="font-semibold tracking-tight">Base</a>
		<ul class="flex items-center gap-4 text-sm">
			{#each links as link (link.href)}
				<li>
					<a
						href={link.href}
						class={cn(
							'text-muted-foreground transition-colors hover:text-foreground',
							page.url.pathname === link.href && 'text-foreground'
						)}
					>
						{link.label}
					</a>
				</li>
			{/each}
		</ul>
	</div>
</nav>

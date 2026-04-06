<script lang="ts">
	import { cn, type WithElementRef } from "$lib/utils.js";
	import type { HTMLAttributes } from "svelte/elements";
	import type { Snippet } from "svelte";

	let {
		ref = $bindable(null),
		class: className,
		children,
		...restProps
	}: WithElementRef<HTMLAttributes<HTMLSpanElement>> & { children?: Snippet } = $props();
</script>

<span
	bind:this={ref}
	data-slot="input-group-text"
	class={cn("text-muted-foreground text-xs", className)}
	{...restProps}
>
	{@render children?.()}
</span>

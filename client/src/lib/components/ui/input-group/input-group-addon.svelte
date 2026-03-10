<script lang="ts">
	import { cn, type WithElementRef } from "$lib/utils.js";
	import type { HTMLAttributes } from "svelte/elements";
	import type { Snippet } from "svelte";

	let {
		ref = $bindable(null),
		class: className,
		align = "block-end",
		children,
		...restProps
	}: WithElementRef<HTMLAttributes<HTMLDivElement>> & {
		align?: "block-start" | "block-end";
		children?: Snippet;
	} = $props();
</script>

<div
	bind:this={ref}
	data-slot="input-group-addon"
	class={cn(
		"flex items-center gap-1 px-2 py-1.5",
		align === "block-start" && "order-first border-b",
		align === "block-end" && "border-t-0",
		className
	)}
	{...restProps}
>
	{@render children?.()}
</div>

<script lang="ts">
	import { cn, type WithElementRef } from "$lib/utils.js";
	import type { HTMLAttributes } from "svelte/elements";
	import type { Snippet } from "svelte";

	let {
		ref = $bindable(null),
		class: className,
		children,
		...restProps
	}: WithElementRef<HTMLAttributes<HTMLDivElement>> & { children?: Snippet } = $props();
</script>

<div
	bind:this={ref}
	data-slot="input-group"
	class={cn(
		"border-input has-[textarea:focus]:border-ring has-[textarea:focus]:ring-ring/50 rounded-xl border bg-transparent shadow-xs transition-[color,box-shadow] has-[textarea:focus]:ring-[3px]",
		className
	)}
	{...restProps}
>
	{@render children?.()}
</div>

<script lang="ts">
	import { cn, type WithElementRef, type WithoutChildren } from "$lib/utils.js";
	import type { HTMLTextareaAttributes } from "svelte/elements";

	let {
		ref = $bindable(null),
		value = $bindable(),
		class: className,
		...restProps
	}: WithoutChildren<WithElementRef<HTMLTextareaAttributes>> = $props();
</script>

<textarea
	bind:this={ref}
	bind:value
	data-slot="input-group-textarea"
	class={cn(
		"placeholder:text-muted-foreground flex field-sizing-content min-h-[80px] w-full resize-none rounded-xl border-0 bg-transparent px-3 py-2.5 text-base outline-none disabled:cursor-not-allowed disabled:opacity-50 md:text-sm",
		className
	)}
	{...restProps}
></textarea>

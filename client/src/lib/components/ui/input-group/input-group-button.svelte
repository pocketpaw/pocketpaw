<script lang="ts">
	import Button, { type ButtonProps } from "$lib/components/ui/button/button.svelte";

	let { ...restProps }: ButtonProps = $props();
</script>

<Button data-slot="input-group-button" {...restProps} />

<script lang="ts">
  import type { IdentityFiles } from "$lib/api";
  import { goto } from "$app/navigation";
  import { connectionStore } from "$lib/stores";
  import {
    ArrowLeft,
    Fingerprint,
    Heart,
    Palette,
    ScrollText,
    User,
    Pencil,
    Save,
    X,
    Loader2,
  } from "@lucide/svelte";
  import { toast } from "svelte-sonner";

  type TabKey = keyof IdentityFiles;

  interface TabDef {
    key: TabKey;
    label: string;
    icon: typeof Fingerprint;
    hint: string;
  }

  const TABS: TabDef[] = [
    {
      key: "identity_file",
      label: "Identity",
      icon: Fingerprint,
      hint: "The primary directive — defines who the agent is and how it behaves.",
    },
    {
      key: "soul_file",
      label: "Soul",
      icon: Heart,
      hint: "Core philosophy and values that guide the agent's decisions.",
    },
    {
      key: "style_file",
      label: "Style",
      icon: Palette,
      hint: "Communication style — tone, formatting, and interaction patterns.",
    },
    {
      key: "instructions_file",
      label: "Instructions",
      icon: ScrollText,
      hint: "Behavioral instructions and tool usage guides.",
    },
    {
      key: "user_file",
      label: "User",
      icon: User,
      hint: "Your profile — injected into every prompt so the agent knows you.",
    },
  ];

  let identityData = $state<IdentityFiles | null>(null);
  let identityDraft = $state<Partial<IdentityFiles>>({});
  let loading = $state(true);
  let editing = $state(false);
  let saving = $state(false);
  let activeTab = $state<TabKey>("identity_file");

  let currentTab = $derived(TABS.find((t) => t.key === activeTab)!);
  let displayContent = $derived.by(() => {
    if (editing) return identityDraft[activeTab] ?? "";
    return identityData?.[activeTab] ?? "";
  });

  $effect(() => {
    loadIdentity();
  });

  async function loadIdentity() {
    loading = true;
    try {
      const client = connectionStore.getClient();
      identityData = await client.getIdentity();
    } catch {
      toast.error("Failed to load identity files");
    } finally {
      loading = false;
    }
  }

  function startEditing() {
    if (!identityData) return;
    identityDraft = { ...identityData };
    editing = true;
  }

  function cancelEditing() {
    editing = false;
    identityDraft = {};
  }

  async function saveIdentity() {
    saving = true;
    try {
      const client = connectionStore.getClient();
      const result = await client.updateIdentity(identityDraft);
      if (result.ok) {
        if (identityData) {
          identityData = { ...identityData, ...identityDraft };
        }
        editing = false;
        identityDraft = {};
        toast.success(`Updated ${result.updated.join(", ")}`);
      }
    } catch {
      toast.error("Failed to save identity");
    } finally {
      saving = false;
    }
  }

  function handleDraftInput(e: Event) {
    const target = e.target as HTMLTextAreaElement;
    identityDraft = { ...identityDraft, [activeTab]: target.value };
  }
</script>

<div class="flex h-full flex-col gap-4 overflow-y-auto px-4 py-4 md:px-6 md:py-6">
  <!-- Header -->
  <div class="flex items-center gap-3">
    <button
      onclick={() => goto("/")}
      class="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
    >
      <ArrowLeft class="h-4 w-4" />
    </button>
    <Fingerprint class="h-4 w-4 text-muted-foreground" strokeWidth={1.75} />
    <h1 class="text-lg font-semibold text-foreground">Identity</h1>
    <div class="ml-auto flex items-center gap-2">
      {#if editing}
        <button
          onclick={cancelEditing}
          disabled={saving}
          class="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          <X class="h-3 w-3" />
          Cancel
        </button>
        <button
          onclick={saveIdentity}
          disabled={saving}
          class="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
        >
          {#if saving}
            <Loader2 class="h-3 w-3 animate-spin" />
          {:else}
            <Save class="h-3 w-3" />
          {/if}
          Save
        </button>
      {:else}
        <button
          onclick={startEditing}
          disabled={loading || !identityData}
          class="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-40"
        >
          <Pencil class="h-3 w-3" />
          Edit
        </button>
      {/if}
    </div>
  </div>

  {#if loading}
    <div class="flex items-center gap-2 py-8 text-sm text-muted-foreground">
      <Loader2 class="h-4 w-4 animate-spin" />
      Loading identity files...
    </div>
  {:else}
    <!-- Sub-tabs -->
    <div class="flex gap-1 overflow-x-auto rounded-lg border border-border/40 bg-muted/20 p-1">
      {#each TABS as tab (tab.key)}
        {@const Icon = tab.icon}
        <button
          onclick={() => (activeTab = tab.key)}
          class={[
            "flex shrink-0 items-center gap-1.5 rounded-md px-3 py-1.5 text-xs transition-colors",
            activeTab === tab.key
              ? "bg-background font-medium text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground",
          ].join(" ")}
        >
          <Icon class="h-3 w-3" />
          {tab.label}
        </button>
      {/each}
    </div>

    <!-- Hint -->
    <p class="text-[10px] text-muted-foreground">{currentTab.hint}</p>

    <!-- Content -->
    {#if editing}
      <textarea
        value={identityDraft[activeTab] ?? ""}
        oninput={handleDraftInput}
        class="min-h-64 flex-1 resize-y rounded-lg border border-border bg-muted/30 p-3 font-mono text-sm leading-relaxed text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
        placeholder="Enter content..."
      ></textarea>
      <p class="text-[10px] text-muted-foreground">
        Changes take effect on the next message sent to the agent.
      </p>
    {:else}
      <div class="flex-1 overflow-y-auto rounded-lg border border-border/40 bg-muted/20 p-3">
        <pre class="whitespace-pre-wrap font-mono text-sm leading-relaxed text-foreground">{displayContent || "No content yet"}</pre>
      </div>
    {/if}
  {/if}
</div>

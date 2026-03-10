<script lang="ts">
  import type { PanelConfig, AgentProfile, AgentStatus, AgentLevel } from "$lib/types/pawkit";
  import Badge from "$lib/components/ui/badge/badge.svelte";
  import * as Dialog from "$lib/components/ui/dialog";
  import * as Select from "$lib/components/ui/select";
  import { connectionStore, mcStore } from "$lib/stores";
  import { Plus, Trash2, Bot } from "@lucide/svelte";

  let {
    config,
    data,
    onDataChanged,
  }: { config: PanelConfig; data: unknown; onDataChanged?: () => void } = $props();

  let agents = $derived<AgentProfile[]>(
    Array.isArray(data) ? data as AgentProfile[] : mcStore.agents,
  );

  const statusColors: Record<string, string> = {
    idle: "bg-muted-foreground/20 text-muted-foreground",
    active: "bg-green-500/20 text-green-600",
    blocked: "bg-orange-500/20 text-orange-600",
    offline: "bg-red-500/20 text-red-600",
  };

  const allLevels: AgentLevel[] = ["intern", "specialist", "lead"];

  function initials(name: string): string {
    return name.split(/\s+/).map((w) => w[0]).join("").toUpperCase().slice(0, 2);
  }

  // -- Create dialog --
  let createOpen = $state(false);
  let createName = $state("");
  let createRole = $state("");
  let createDesc = $state("");
  let createSpecialties = $state("");
  let createLevel = $state<AgentLevel>("specialist");
  let createLoading = $state(false);

  function openCreate() {
    createName = "";
    createRole = "";
    createDesc = "";
    createSpecialties = "";
    createLevel = "specialist";
    createOpen = true;
  }

  async function handleCreate() {
    if (!createName.trim() || !createRole.trim()) return;
    createLoading = true;
    try {
      const client = connectionStore.getClient();
      await client.mcCreateAgent({
        name: createName.trim(),
        role: createRole.trim(),
        description: createDesc.trim() || undefined,
        specialties: createSpecialties.split(",").map((s) => s.trim()).filter(Boolean),
        level: createLevel,
      });
      createOpen = false;
      mcStore.loadAgents();
      onDataChanged?.();
    } catch (e) {
      console.error("[AgentRoster] Failed to create agent:", e);
    } finally {
      createLoading = false;
    }
  }

  // -- Edit dialog --
  let editOpen = $state(false);
  let editAgent = $state<AgentProfile | null>(null);
  let editName = $state("");
  let editRole = $state("");
  let editDesc = $state("");
  let editSpecialties = $state("");
  let editLevel = $state<AgentLevel>("specialist");
  let editLoading = $state(false);
  let confirmDelete = $state(false);

  function openEdit(agent: AgentProfile) {
    editAgent = agent;
    editName = agent.name;
    editRole = agent.role;
    editDesc = agent.description;
    editSpecialties = agent.specialties.join(", ");
    editLevel = agent.level;
    confirmDelete = false;
    editOpen = true;
  }

  async function handleUpdate() {
    if (!editAgent || !editName.trim() || !editRole.trim()) return;
    editLoading = true;
    try {
      const client = connectionStore.getClient();
      await client.mcUpdateAgent(editAgent.id, {
        name: editName.trim(),
        role: editRole.trim(),
        description: editDesc.trim(),
        specialties: editSpecialties.split(",").map((s) => s.trim()).filter(Boolean),
        level: editLevel,
      });
      editOpen = false;
      mcStore.loadAgents();
      onDataChanged?.();
    } catch (e) {
      console.error("[AgentRoster] Failed to update agent:", e);
    } finally {
      editLoading = false;
    }
  }

  async function handleDelete() {
    if (!editAgent) return;
    if (!confirmDelete) { confirmDelete = true; return; }
    editLoading = true;
    try {
      const client = connectionStore.getClient();
      await client.mcDeleteAgent(editAgent.id);
      editOpen = false;
      mcStore.loadAgents();
      onDataChanged?.();
    } catch (e) {
      console.error("[AgentRoster] Failed to delete agent:", e);
    } finally {
      editLoading = false;
    }
  }
</script>

<div class="flex flex-col gap-2">
  <div class="flex items-center justify-between px-1">
    <span class="text-xs font-medium text-muted-foreground">{agents.length} agent{agents.length !== 1 ? "s" : ""}</span>
    {#if onDataChanged}
      <button
        class="flex h-5 w-5 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        title="Add agent"
        onclick={openCreate}
      >
        <Plus class="h-3.5 w-3.5" />
      </button>
    {/if}
  </div>

  <div class="grid gap-2 sm:grid-cols-2">
    {#each agents as agent (agent.id)}
      <button
        class="flex items-start gap-3 rounded-lg border border-border bg-card p-3 text-left transition-colors hover:border-foreground/20"
        onclick={() => openEdit(agent)}
      >
        <div class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-muted text-xs font-medium text-foreground">
          {initials(agent.name)}
        </div>
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-1.5">
            <span class="truncate text-sm font-medium text-foreground">{agent.name}</span>
            <span class={["inline-flex items-center rounded-full px-1.5 py-0.5 text-[9px] font-medium", statusColors[agent.status] ?? statusColors.offline].join(" ")}>
              {#if agent.status === "active"}<span class="mr-0.5 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-green-500"></span>{/if}
              {agent.status}
            </span>
          </div>
          <p class="truncate text-[11px] text-muted-foreground">{agent.role}</p>
          {#if agent.specialties.length > 0}
            <div class="mt-1 flex flex-wrap gap-1">
              {#each agent.specialties.slice(0, 3) as spec}
                <span class="rounded bg-muted px-1 py-0.5 text-[9px] text-muted-foreground">{spec}</span>
              {/each}
            </div>
          {/if}
        </div>
      </button>
    {/each}
  </div>

  {#if agents.length === 0}
    <div class="flex flex-col items-center gap-2 py-6 text-center">
      <Bot class="h-8 w-8 text-muted-foreground/50" strokeWidth={1.5} />
      <p class="text-xs text-muted-foreground">No agents yet</p>
    </div>
  {/if}
</div>

<!-- Create Agent Dialog -->
<Dialog.Root bind:open={createOpen}>
  <Dialog.Content class="sm:max-w-md">
    <Dialog.Header>
      <Dialog.Title>New agent</Dialog.Title>
    </Dialog.Header>
    <form class="flex flex-col gap-3" onsubmit={(e) => { e.preventDefault(); handleCreate(); }}>
      <input type="text" placeholder="Name" bind:value={createName}
        class="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring" />
      <input type="text" placeholder="Role (e.g. Frontend Developer)" bind:value={createRole}
        class="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring" />
      <input type="text" placeholder="Description (optional)" bind:value={createDesc}
        class="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring" />
      <input type="text" placeholder="Specialties (comma-separated)" bind:value={createSpecialties}
        class="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring" />
      <div class="flex items-center gap-2">
        <span class="text-xs text-muted-foreground">Level:</span>
        <Select.Root type="single" value={createLevel} onValueChange={(v) => { if (v) createLevel = v as AgentLevel; }}>
          <Select.Trigger size="sm" class="w-[120px]">{createLevel}</Select.Trigger>
          <Select.Content>
            {#each allLevels as l}<Select.Item value={l} label={l} />{/each}
          </Select.Content>
        </Select.Root>
      </div>
      <Dialog.Footer>
        <button type="button" class="rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground" onclick={() => (createOpen = false)}>Cancel</button>
        <button type="submit" disabled={!createName.trim() || !createRole.trim() || createLoading} class="rounded-md bg-foreground px-3 py-1.5 text-xs font-medium text-background disabled:opacity-50">
          {createLoading ? "Creating..." : "Create"}
        </button>
      </Dialog.Footer>
    </form>
  </Dialog.Content>
</Dialog.Root>

<!-- Edit Agent Dialog -->
<Dialog.Root bind:open={editOpen}>
  <Dialog.Content class="sm:max-w-md">
    {#if editAgent}
      <Dialog.Header>
        <Dialog.Title>Edit agent</Dialog.Title>
        <Dialog.Description>
          <Badge variant="outline" class="text-[10px]">{editAgent.status}</Badge>
          <Badge variant="secondary" class="ml-1 text-[10px]">{editAgent.level}</Badge>
        </Dialog.Description>
      </Dialog.Header>
      <form class="flex flex-col gap-3" onsubmit={(e) => { e.preventDefault(); handleUpdate(); }}>
        <input type="text" placeholder="Name" bind:value={editName}
          class="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring" />
        <input type="text" placeholder="Role" bind:value={editRole}
          class="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring" />
        <input type="text" placeholder="Description" bind:value={editDesc}
          class="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring" />
        <input type="text" placeholder="Specialties (comma-separated)" bind:value={editSpecialties}
          class="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring" />
        <div class="flex items-center gap-2">
          <span class="text-xs text-muted-foreground">Level:</span>
          <Select.Root type="single" value={editLevel} onValueChange={(v) => { if (v) editLevel = v as AgentLevel; }}>
            <Select.Trigger size="sm" class="w-[120px]">{editLevel}</Select.Trigger>
            <Select.Content>
              {#each allLevels as l}<Select.Item value={l} label={l} />{/each}
            </Select.Content>
          </Select.Root>
        </div>
        <Dialog.Footer>
          <button type="button" disabled={editLoading}
            class={["flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs transition-colors", confirmDelete ? "border-red-500 bg-red-500 text-white" : "border-red-300 text-red-500 hover:bg-red-500/10"].join(" ")}
            onclick={handleDelete}>
            <Trash2 class="h-3 w-3" />{confirmDelete ? "Confirm delete" : "Delete"}
          </button>
          <button type="submit" disabled={!editName.trim() || !editRole.trim() || editLoading} class="rounded-md bg-foreground px-3 py-1.5 text-xs font-medium text-background disabled:opacity-50">
            {editLoading ? "Saving..." : "Save"}
          </button>
        </Dialog.Footer>
      </form>
    {/if}
  </Dialog.Content>
</Dialog.Root>

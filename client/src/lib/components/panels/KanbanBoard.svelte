<script lang="ts">
  import type { PanelConfig, TaskStatus, TaskPriority, MCMessage } from "$lib/types/pawkit";
  import Badge from "$lib/components/ui/badge/badge.svelte";
  import * as Dialog from "$lib/components/ui/dialog";
  import * as Select from "$lib/components/ui/select";
  import { connectionStore, mcStore } from "$lib/stores";
  import { Plus, Trash2, Play, Square, Send, MessageSquare } from "@lucide/svelte";

  let {
    config,
    data,
    onDataChanged,
  }: { config: PanelConfig; data: unknown; onDataChanged?: () => void } = $props();

  interface ColumnDef {
    key: string;
    label: string;
    color: string;
  }

  let columns = $derived((config.columns as ColumnDef[]) ?? []);
  let cardFields = $derived((config.card_fields as string[]) ?? ["title"]);

  function getColumnItems(key: string): Record<string, unknown>[] {
    if (!data || typeof data !== "object") return [];
    const group = (data as Record<string, unknown>)[key];
    if (!Array.isArray(group)) return [];
    return group as Record<string, unknown>[];
  }

  const colorMap: Record<string, string> = {
    gray: "bg-muted/50",
    blue: "bg-blue-500/10",
    orange: "bg-orange-500/10",
    green: "bg-green-500/10",
    purple: "bg-purple-500/10",
  };

  const badgeMap: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
    urgent: "destructive",
    high: "destructive",
    medium: "secondary",
    low: "outline",
  };

  const allStatuses: TaskStatus[] = [
    "inbox", "assigned", "in_progress", "review", "done", "blocked", "skipped",
  ];

  const allPriorities: TaskPriority[] = ["low", "medium", "high", "urgent"];

  // -- Create task dialog state --
  let createOpen = $state(false);
  let createColumn = $state("");
  let createTitle = $state("");
  let createPriority = $state<TaskPriority>("medium");
  let createLoading = $state(false);

  function openCreateDialog(columnKey: string) {
    createColumn = columnKey;
    createTitle = "";
    createPriority = "medium";
    createOpen = true;
  }

  async function handleCreateTask() {
    if (!createTitle.trim()) return;
    createLoading = true;
    try {
      const client = connectionStore.getClient();
      await client.mcCreateTask(
        createTitle.trim(),
        createPriority,
        undefined,
        createColumn as TaskStatus,
      );
      createOpen = false;
      onDataChanged?.();
    } catch (e) {
      console.error("[KanbanBoard] Failed to create task:", e);
    } finally {
      createLoading = false;
    }
  }

  // -- Card detail dialog state --
  let detailOpen = $state(false);
  let detailItem = $state<Record<string, unknown> | null>(null);
  let detailLoading = $state(false);
  let confirmDelete = $state(false);

  // -- Agent assignment state --
  let assignLoading = $state(false);

  // -- Run/Stop state --
  let runLoading = $state(false);
  let stopLoading = $state(false);

  // -- Messages/Discussion state --
  let messages = $state<MCMessage[]>([]);
  let messageText = $state("");
  let messagesLoading = $state(false);
  let sendingMessage = $state(false);

  // Derive current assignee IDs from the detail item
  let currentAssigneeIds = $derived<string[]>(
    detailItem && Array.isArray(detailItem.assignee_ids)
      ? (detailItem.assignee_ids as string[])
      : [],
  );

  // Resolve agent names from mcStore.agents
  let assignedAgents = $derived(
    currentAssigneeIds
      .map((id) => mcStore.agents.find((a) => a.id === id))
      .filter((a) => a != null),
  );

  // Agents available for assignment (not already assigned)
  let availableAgents = $derived(
    mcStore.agents.filter((a) => !currentAssigneeIds.includes(a.id)),
  );

  // Whether the task is currently running
  let isTaskRunning = $derived(
    detailItem?.id ? mcStore.runningTaskIds.has(String(detailItem.id)) : false,
  );

  // Whether the task can be run (has assignees, not already done/in_progress)
  let canRunTask = $derived(
    currentAssigneeIds.length > 0 &&
    detailItem?.status !== "in_progress" &&
    detailItem?.status !== "done" &&
    !isTaskRunning,
  );

  async function openDetailDialog(item: Record<string, unknown>) {
    detailItem = item;
    confirmDelete = false;
    messages = [];
    messageText = "";
    detailOpen = true;

    // Fetch messages in background
    if (item.id) {
      messagesLoading = true;
      try {
        const client = connectionStore.getClient();
        const res = await client.mcGetTaskMessages(String(item.id));
        messages = res.messages;
      } catch (e) {
        console.error("[KanbanBoard] Failed to load messages:", e);
      } finally {
        messagesLoading = false;
      }
    }
  }

  async function handleStatusChange(status: string) {
    if (!detailItem?.id) return;
    detailLoading = true;
    try {
      const client = connectionStore.getClient();
      await client.mcUpdateTaskStatus(String(detailItem.id), status as TaskStatus);
      detailOpen = false;
      onDataChanged?.();
    } catch (e) {
      console.error("[KanbanBoard] Failed to update status:", e);
    } finally {
      detailLoading = false;
    }
  }

  async function handleDelete() {
    if (!detailItem?.id) return;
    if (!confirmDelete) {
      confirmDelete = true;
      return;
    }
    detailLoading = true;
    try {
      const client = connectionStore.getClient();
      await client.mcDeleteTask(String(detailItem.id));
      detailOpen = false;
      onDataChanged?.();
    } catch (e) {
      console.error("[KanbanBoard] Failed to delete task:", e);
    } finally {
      detailLoading = false;
    }
  }

  async function handleAssignAgent(agentId: string) {
    if (!detailItem?.id || !agentId) return;
    assignLoading = true;
    try {
      const client = connectionStore.getClient();
      const newIds = [...currentAssigneeIds, agentId];
      await client.mcAssignTask(String(detailItem.id), newIds);
      // Update local state so UI reflects the change
      detailItem = { ...detailItem, assignee_ids: newIds };
      onDataChanged?.();
    } catch (e) {
      console.error("[KanbanBoard] Failed to assign agent:", e);
    } finally {
      assignLoading = false;
    }
  }

  async function handleUnassignAgent(agentId: string) {
    if (!detailItem?.id) return;
    assignLoading = true;
    try {
      const client = connectionStore.getClient();
      const newIds = currentAssigneeIds.filter((id) => id !== agentId);
      await client.mcAssignTask(String(detailItem.id), newIds);
      detailItem = { ...detailItem, assignee_ids: newIds };
      onDataChanged?.();
    } catch (e) {
      console.error("[KanbanBoard] Failed to unassign agent:", e);
    } finally {
      assignLoading = false;
    }
  }

  async function handleRunTask() {
    if (!detailItem?.id || currentAssigneeIds.length === 0) return;
    runLoading = true;
    try {
      const client = connectionStore.getClient();
      await client.mcRunTask(String(detailItem.id), currentAssigneeIds[0]);
      onDataChanged?.();
    } catch (e) {
      console.error("[KanbanBoard] Failed to run task:", e);
    } finally {
      runLoading = false;
    }
  }

  async function handleStopTask() {
    if (!detailItem?.id) return;
    stopLoading = true;
    try {
      const client = connectionStore.getClient();
      await client.mcStopTask(String(detailItem.id));
      onDataChanged?.();
    } catch (e) {
      console.error("[KanbanBoard] Failed to stop task:", e);
    } finally {
      stopLoading = false;
    }
  }

  async function handleSendMessage() {
    if (!detailItem?.id || !messageText.trim()) return;
    sendingMessage = true;
    try {
      const client = connectionStore.getClient();
      await client.mcPostTaskMessage(String(detailItem.id), messageText.trim());
      messageText = "";
      // Re-fetch messages
      const res = await client.mcGetTaskMessages(String(detailItem.id));
      messages = res.messages;
    } catch (e) {
      console.error("[KanbanBoard] Failed to send message:", e);
    } finally {
      sendingMessage = false;
    }
  }

  function formatRelativeTime(dateStr: string): string {
    const now = Date.now();
    const then = new Date(dateStr).getTime();
    const diffMs = now - then;
    const diffSec = Math.floor(diffMs / 1000);
    if (diffSec < 60) return "just now";
    const diffMin = Math.floor(diffSec / 60);
    if (diffMin < 60) return `${diffMin}m ago`;
    const diffHr = Math.floor(diffMin / 60);
    if (diffHr < 24) return `${diffHr}h ago`;
    const diffDay = Math.floor(diffHr / 24);
    return `${diffDay}d ago`;
  }

  function resolveAgentName(agentId: string): string {
    const agent = mcStore.agents.find((a) => a.id === agentId);
    return agent?.name ?? agentId;
  }
</script>

<div class="grid gap-3" style="grid-template-columns: repeat({columns.length}, 1fr);">
  {#each columns as col (col.key)}
    {@const items = getColumnItems(col.key)}
    <div class="flex flex-col gap-2">
      <div class="flex items-center gap-2 px-1">
        <span class="text-xs font-medium text-foreground">{col.label}</span>
        <span class="text-xs text-muted-foreground">{items.length}</span>
        {#if onDataChanged}
          <button
            class="ml-auto flex h-5 w-5 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            title="Add task to {col.label}"
            onclick={() => openCreateDialog(col.key)}
          >
            <Plus class="h-3.5 w-3.5" />
          </button>
        {/if}
      </div>
      <div class="flex flex-col gap-2 rounded-lg {colorMap[col.color] ?? 'bg-muted/50'} p-2 min-h-[120px]">
        {#each items as item (item.id ?? item.title)}
          <button
            class="relative w-full cursor-pointer rounded-lg border border-border bg-card p-3 text-left transition-colors hover:border-foreground/20"
            onclick={() => openDetailDialog(item)}
          >
            {#if item.id && mcStore.runningTaskIds.has(String(item.id))}
              <span
                class="absolute top-2 right-2 h-2 w-2 animate-pulse rounded-full bg-green-500"
                title="Running"
              ></span>
            {/if}
            <p class="text-sm font-medium text-foreground">{item.title ?? "Untitled"}</p>
            {#if cardFields.includes("priority") && item.priority}
              <Badge variant={badgeMap[String(item.priority)] ?? "outline"} class="mt-1 text-[10px]">
                {item.priority}
              </Badge>
            {/if}
            {#if cardFields.includes("task_type") && item.task_type}
              <span class="ml-1 text-[10px] text-muted-foreground">{item.task_type}</span>
            {/if}
          </button>
        {/each}
        {#if items.length === 0}
          <p class="py-4 text-center text-xs text-muted-foreground">No items</p>
        {/if}
      </div>
    </div>
  {/each}
</div>

<!-- Create Task Dialog -->
<Dialog.Root bind:open={createOpen}>
  <Dialog.Content class="sm:max-w-md">
    <Dialog.Header>
      <Dialog.Title>New task</Dialog.Title>
      <Dialog.Description>
        Create a task in the <span class="font-medium">{columns.find((c) => c.key === createColumn)?.label ?? createColumn}</span> column.
      </Dialog.Description>
    </Dialog.Header>
    <form
      class="flex flex-col gap-3"
      onsubmit={(e) => { e.preventDefault(); handleCreateTask(); }}
    >
      <input
        type="text"
        placeholder="Task title"
        bind:value={createTitle}
        class="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
      />
      <div class="flex items-center gap-2">
        <span class="text-xs text-muted-foreground">Priority:</span>
        <Select.Root
          type="single"
          value={createPriority}
          onValueChange={(v) => { if (v) createPriority = v as TaskPriority; }}
        >
          <Select.Trigger size="sm" class="w-[120px]">
            {createPriority}
          </Select.Trigger>
          <Select.Content>
            {#each allPriorities as p}
              <Select.Item value={p} label={p} />
            {/each}
          </Select.Content>
        </Select.Root>
      </div>
      <Dialog.Footer>
        <button
          type="button"
          class="rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground"
          onclick={() => (createOpen = false)}
        >Cancel</button>
        <button
          type="submit"
          disabled={!createTitle.trim() || createLoading}
          class="rounded-md bg-foreground px-3 py-1.5 text-xs font-medium text-background disabled:opacity-50"
        >{createLoading ? "Creating..." : "Create"}</button>
      </Dialog.Footer>
    </form>
  </Dialog.Content>
</Dialog.Root>

<!-- Card Detail Dialog -->
<Dialog.Root bind:open={detailOpen}>
  <Dialog.Content class="sm:max-w-lg max-h-[85vh] overflow-y-auto">
    {#if detailItem}
      <Dialog.Header>
        <Dialog.Title class="flex items-center gap-2">
          {detailItem.title ?? "Untitled"}
          {#if isTaskRunning}
            <span class="inline-flex h-2.5 w-2.5 animate-pulse rounded-full bg-green-500" title="Running"></span>
          {/if}
        </Dialog.Title>
        {#if detailItem.description}
          <Dialog.Description>{detailItem.description}</Dialog.Description>
        {/if}
      </Dialog.Header>

      <div class="flex flex-col gap-4">
        <!-- Status buttons -->
        <div>
          <span class="mb-1.5 block text-xs text-muted-foreground">Status</span>
          <div class="flex flex-wrap gap-1.5">
            {#each allStatuses as s}
              <button
                disabled={detailLoading}
                class={[
                  "rounded-md border px-2.5 py-1 text-xs transition-colors",
                  detailItem.status === s
                    ? "border-foreground bg-foreground text-background"
                    : "border-border text-muted-foreground hover:border-foreground/40 hover:text-foreground",
                ].join(" ")}
                onclick={() => handleStatusChange(s)}
              >{s.replace("_", " ")}</button>
            {/each}
          </div>
        </div>

        <!-- Priority display -->
        {#if detailItem.priority}
          <div>
            <span class="mb-1 block text-xs text-muted-foreground">Priority</span>
            <Badge variant={badgeMap[String(detailItem.priority)] ?? "outline"} class="text-[10px]">
              {detailItem.priority}
            </Badge>
          </div>
        {/if}

        <!-- Assigned Agents -->
        <div>
          <span class="mb-1.5 block text-xs text-muted-foreground">Assigned Agents</span>
          <div class="flex flex-wrap gap-1.5">
            {#if assignedAgents.length === 0}
              <span class="text-xs text-muted-foreground/60">No agents assigned</span>
            {:else}
              {#each assignedAgents as agent (agent.id)}
                <span
                  class="inline-flex items-center gap-1 rounded-full border border-border bg-muted/50 px-2 py-0.5 text-xs text-foreground"
                >
                  {agent.name}
                  <button
                    class="ml-0.5 text-muted-foreground hover:text-foreground"
                    title="Remove {agent.name}"
                    disabled={assignLoading}
                    onclick={() => handleUnassignAgent(agent.id)}
                  >
                    &times;
                  </button>
                </span>
              {/each}
            {/if}
          </div>
          {#if availableAgents.length > 0}
            <div class="mt-2">
              <Select.Root
                type="single"
                onValueChange={(v) => { if (v) handleAssignAgent(v); }}
              >
                <Select.Trigger size="sm" class="w-[180px]" disabled={assignLoading}>
                  {assignLoading ? "Assigning..." : "Assign agent..."}
                </Select.Trigger>
                <Select.Content>
                  {#each availableAgents as agent (agent.id)}
                    <Select.Item value={agent.id} label={agent.name}>
                      <span class="flex items-center gap-2">
                        <span>{agent.name}</span>
                        <span class="text-muted-foreground text-[10px]">{agent.role}</span>
                      </span>
                    </Select.Item>
                  {/each}
                </Select.Content>
              </Select.Root>
            </div>
          {/if}
        </div>

        <!-- Run / Stop buttons -->
        <div class="flex items-center gap-2">
          {#if canRunTask}
            <button
              disabled={runLoading}
              class="flex items-center gap-1.5 rounded-md border border-green-500 bg-green-500/10 px-3 py-1.5 text-xs font-medium text-green-600 transition-colors hover:bg-green-500/20 disabled:opacity-50 dark:text-green-400"
              onclick={handleRunTask}
            >
              <Play class="h-3 w-3" />
              {runLoading ? "Starting..." : "Run"}
            </button>
          {/if}
          {#if isTaskRunning}
            <button
              disabled={stopLoading}
              class="flex items-center gap-1.5 rounded-md border border-red-500 bg-red-500/10 px-3 py-1.5 text-xs font-medium text-red-600 transition-colors hover:bg-red-500/20 disabled:opacity-50 dark:text-red-400"
              onclick={handleStopTask}
            >
              <Square class="h-3 w-3" />
              {stopLoading ? "Stopping..." : "Stop"}
            </button>
          {/if}
        </div>

        <!-- Messages / Discussion -->
        <div>
          <span class="mb-1.5 flex items-center gap-1.5 text-xs text-muted-foreground">
            <MessageSquare class="h-3 w-3" />
            Discussion
            {#if messages.length > 0}
              <span class="text-[10px]">({messages.length})</span>
            {/if}
          </span>

          <div class="flex max-h-[200px] flex-col gap-2 overflow-y-auto rounded-md border border-border bg-muted/30 p-2">
            {#if messagesLoading}
              <p class="py-3 text-center text-xs text-muted-foreground">Loading messages...</p>
            {:else if messages.length === 0}
              <p class="py-3 text-center text-xs text-muted-foreground">No messages yet</p>
            {:else}
              {#each messages as msg (msg.id)}
                <div class="rounded-md bg-background p-2 text-xs">
                  <div class="mb-0.5 flex items-center justify-between gap-2">
                    <span class="font-medium text-foreground">
                      {resolveAgentName(msg.from_agent_id)}
                    </span>
                    <span class="text-[10px] text-muted-foreground">
                      {formatRelativeTime(msg.created_at)}
                    </span>
                  </div>
                  <p class="whitespace-pre-wrap text-foreground/80">{msg.content}</p>
                </div>
              {/each}
            {/if}
          </div>

          <!-- Send message input -->
          <form
            class="mt-2 flex items-center gap-1.5"
            onsubmit={(e) => { e.preventDefault(); handleSendMessage(); }}
          >
            <input
              type="text"
              placeholder="Write a message..."
              bind:value={messageText}
              disabled={sendingMessage}
              class="flex-1 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
            />
            <button
              type="submit"
              disabled={!messageText.trim() || sendingMessage}
              class="flex h-7 w-7 items-center justify-center rounded-md bg-foreground text-background transition-colors hover:bg-foreground/90 disabled:opacity-50"
              title="Send message"
            >
              <Send class="h-3 w-3" />
            </button>
          </form>
        </div>

        <!-- Delete -->
        <Dialog.Footer>
          <button
            disabled={detailLoading}
            class={[
              "flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs transition-colors",
              confirmDelete
                ? "border-red-500 bg-red-500 text-white"
                : "border-red-300 text-red-500 hover:bg-red-500/10",
            ].join(" ")}
            onclick={handleDelete}
          >
            <Trash2 class="h-3 w-3" />
            {confirmDelete ? "Confirm delete" : "Delete"}
          </button>
        </Dialog.Footer>
      </div>
    {/if}
  </Dialog.Content>
</Dialog.Root>

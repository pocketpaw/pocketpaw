<script lang="ts">
  import type { PanelConfig, DocumentType } from "$lib/types/pawkit";
  import * as Dialog from "$lib/components/ui/dialog";
  import * as Select from "$lib/components/ui/select";
  import { connectionStore } from "$lib/stores";
  import { Plus, Trash2, Pencil } from "@lucide/svelte";

  let {
    config,
    data,
    onDataChanged,
  }: { config: PanelConfig; data: unknown; onDataChanged?: () => void } = $props();

  interface ColDef {
    key: string;
    label: string;
  }

  let columns = $derived((config.columns as ColDef[]) ?? []);
  let rows = $derived<Record<string, unknown>[]>(Array.isArray(data) ? data : []);

  let sortKey = $state<string | null>(null);
  let sortAsc = $state(true);

  function sortedRows(): Record<string, unknown>[] {
    if (!sortKey) return rows;
    return [...rows].sort((a, b) => {
      const va = String(a[sortKey!] ?? "");
      const vb = String(b[sortKey!] ?? "");
      return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
    });
  }

  function toggleSort(key: string) {
    if (sortKey === key) {
      sortAsc = !sortAsc;
    } else {
      sortKey = key;
      sortAsc = true;
    }
  }

  function formatCell(value: unknown): string {
    if (value == null) return "\u2014";
    if (typeof value === "string" && value.includes("T")) {
      try {
        const diff = Date.now() - new Date(value).getTime();
        const hours = Math.floor(diff / 3_600_000);
        if (hours < 1) return "just now";
        if (hours < 24) return `${hours} hours ago`;
        return `${Math.floor(hours / 24)} days ago`;
      } catch {
        return String(value);
      }
    }
    return String(value);
  }

  const allDocTypes: DocumentType[] = [
    "draft", "deliverable", "research", "protocol", "template",
  ];

  // -- Create document dialog state --
  let createOpen = $state(false);
  let createTitle = $state("");
  let createType = $state<DocumentType>("draft");
  let createLoading = $state(false);

  async function handleCreateDoc() {
    if (!createTitle.trim()) return;
    createLoading = true;
    try {
      const client = connectionStore.getClient();
      await client.mcCreateDocument(createTitle.trim(), "", createType);
      createOpen = false;
      onDataChanged?.();
    } catch (e) {
      console.error("[DataTable] Failed to create document:", e);
    } finally {
      createLoading = false;
    }
  }

  // -- Delete state --
  let deleteId = $state<string | null>(null);
  let deleteLoading = $state(false);

  async function handleDelete(id: string) {
    if (deleteId !== id) {
      deleteId = id;
      return;
    }
    deleteLoading = true;
    try {
      const client = connectionStore.getClient();
      await client.mcDeleteDocument(id);
      deleteId = null;
      onDataChanged?.();
    } catch (e) {
      console.error("[DataTable] Failed to delete document:", e);
    } finally {
      deleteLoading = false;
    }
  }

  // -- View/Edit document dialog state --
  let viewDocOpen = $state(false);
  let viewDocId = $state("");
  let viewDocTitle = $state("");
  let viewDocContent = $state("");
  let viewDocLoading = $state(false);
  let editMode = $state(false);
  let editContent = $state("");
  let saveLoading = $state(false);

  async function openDocViewer(row: Record<string, unknown>) {
    const id = String(row.id ?? "");
    if (!id) return;
    viewDocId = id;
    viewDocTitle = String(row.title ?? row.name ?? "Document");
    viewDocContent = "";
    editMode = false;
    viewDocLoading = true;
    viewDocOpen = true;
    try {
      const client = connectionStore.getClient();
      const res = await client.mcGetDocument(id);
      viewDocContent = res.document?.content ?? "";
    } catch (e) {
      console.error("[DataTable] Failed to load document:", e);
      viewDocContent = "";
    } finally {
      viewDocLoading = false;
    }
  }

  async function handleSaveDoc() {
    saveLoading = true;
    try {
      const client = connectionStore.getClient();
      await client.mcUpdateDocument(viewDocId, editContent);
      viewDocContent = editContent;
      editMode = false;
      onDataChanged?.();
    } catch (e) {
      console.error("[DataTable] Failed to save document:", e);
    } finally {
      saveLoading = false;
    }
  }
</script>

<div class="overflow-x-auto">
  <table class="w-full text-sm">
    <thead>
      <tr class="border-b border-border">
        {#each columns as col (col.key)}
          <th
            class="cursor-pointer px-3 py-2 text-left text-xs font-medium text-muted-foreground hover:text-foreground"
            onclick={() => toggleSort(col.key)}
          >
            {col.label}
            {#if sortKey === col.key}
              <span class="ml-0.5">{sortAsc ? "\u2191" : "\u2193"}</span>
            {/if}
          </th>
        {/each}
        {#if onDataChanged}
          <th class="w-16 px-3 py-2 text-right">
            <button
              class="inline-flex h-5 w-5 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              title="Add document"
              onclick={() => { createTitle = ""; createType = "draft"; createOpen = true; }}
            >
              <Plus class="h-3.5 w-3.5" />
            </button>
          </th>
        {/if}
      </tr>
    </thead>
    <tbody>
      {#each sortedRows() as row, i (row.id ?? i)}
        <tr
          class="group cursor-pointer border-b border-border/50 last:border-0 hover:bg-muted/30 transition-colors"
          onclick={() => openDocViewer(row)}
        >
          {#each columns as col (col.key)}
            <td class="px-3 py-2 text-xs text-foreground/80">
              {formatCell(row[col.key])}
            </td>
          {/each}
          {#if onDataChanged && row.id}
            <td class="px-3 py-2 text-right">
              <button
                disabled={deleteLoading}
                class={[
                  "inline-flex h-5 w-5 items-center justify-center rounded transition-colors",
                  deleteId === String(row.id)
                    ? "text-red-500 bg-red-500/10"
                    : "text-transparent group-hover:text-muted-foreground hover:!text-red-500",
                ].join(" ")}
                title={deleteId === String(row.id) ? "Click again to confirm" : "Delete"}
                onclick={(e: MouseEvent) => { e.stopPropagation(); handleDelete(String(row.id)); }}
              >
                <Trash2 class="h-3 w-3" />
              </button>
            </td>
          {/if}
        </tr>
      {/each}
      {#if rows.length === 0}
        <tr>
          <td
            colspan={columns.length + (onDataChanged ? 1 : 0)}
            class="py-4 text-center text-xs text-muted-foreground"
          >
            No data
          </td>
        </tr>
      {/if}
    </tbody>
  </table>
</div>

<!-- Create Document Dialog -->
<Dialog.Root bind:open={createOpen}>
  <Dialog.Content class="sm:max-w-md">
    <Dialog.Header>
      <Dialog.Title>New document</Dialog.Title>
    </Dialog.Header>
    <form
      class="flex flex-col gap-3"
      onsubmit={(e) => { e.preventDefault(); handleCreateDoc(); }}
    >
      <input
        type="text"
        placeholder="Document title"
        bind:value={createTitle}
        class="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
      />
      <div class="flex items-center gap-2">
        <span class="text-xs text-muted-foreground">Type:</span>
        <Select.Root
          type="single"
          value={createType}
          onValueChange={(v) => { if (v) createType = v as DocumentType; }}
        >
          <Select.Trigger size="sm" class="w-[140px]">
            {createType}
          </Select.Trigger>
          <Select.Content>
            {#each allDocTypes as t}
              <Select.Item value={t} label={t} />
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

<!-- View/Edit Document Dialog -->
<Dialog.Root bind:open={viewDocOpen}>
  <Dialog.Content class="sm:max-w-2xl max-h-[80vh] overflow-y-auto">
    <Dialog.Header>
      <Dialog.Title>{viewDocTitle}</Dialog.Title>
    </Dialog.Header>

    {#if viewDocLoading}
      <div class="flex items-center justify-center py-8">
        <div class="h-5 w-5 animate-spin rounded-full border-2 border-foreground/20 border-t-foreground"></div>
      </div>
    {:else if editMode}
      <textarea
        bind:value={editContent}
        class="w-full min-h-[300px] rounded-md border border-border bg-background p-3 text-sm text-foreground font-mono focus:outline-none focus:ring-1 focus:ring-ring"
      ></textarea>
    {:else}
      <div class="min-h-[100px] whitespace-pre-wrap text-sm text-foreground/80">
        {viewDocContent || "No content"}
      </div>
    {/if}

    <Dialog.Footer>
      {#if editMode}
        <button
          type="button"
          class="rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground"
          onclick={() => { editMode = false; }}
        >Cancel</button>
        <button
          type="button"
          disabled={saveLoading}
          class="rounded-md bg-foreground px-3 py-1.5 text-xs font-medium text-background disabled:opacity-50"
          onclick={handleSaveDoc}
        >{saveLoading ? "Saving..." : "Save"}</button>
      {:else}
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground"
          onclick={() => { editMode = true; editContent = viewDocContent; }}
        >
          <Pencil class="h-3 w-3" />
          Edit
        </button>
      {/if}
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>

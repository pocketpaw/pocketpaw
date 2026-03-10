<script lang="ts">
  import Search from "@lucide/svelte/icons/search";
  import ArrowUpDown from "@lucide/svelte/icons/arrow-up-down";
  import Code from "@lucide/svelte/icons/code";
  import Table from "@lucide/svelte/icons/table";
  import Copy from "@lucide/svelte/icons/copy";
  import Check from "@lucide/svelte/icons/check";
  import { cn } from "$lib/utils";

  let {
    content = "",
    extension = "csv",
    base64Url = "",
    isBinary = false,
  }: {
    content?: string;
    extension?: string;
    base64Url?: string;
    isBinary?: boolean;
  } = $props();

  let mode = $state<"table" | "source">("table");
  let searchQuery = $state("");
  let sortCol = $state(-1);
  let sortAsc = $state(true);
  let copied = $state(false);

  let sheetNames = $state<string[]>([]);
  let activeSheet = $state(0);
  let binarySheets = $state<string[][][]>([]);
  let binaryLoading = $state(false);
  let binaryError = $state<string | null>(null);

  let delimiter = $derived(extension.toLowerCase() === "tsv" ? "\t" : ",");

  // Load binary xlsx/xls/ods files
  $effect(() => {
    if (!isBinary || !base64Url) return;
    binaryLoading = true;
    binaryError = null;
    sheetNames = [];
    binarySheets = [];
    activeSheet = 0;

    (async () => {
      try {
        const XLSX = await import("xlsx");
        const { base64DataUrlToArrayBuffer } = await import("$lib/filesystem");
        const ab = base64DataUrlToArrayBuffer(base64Url);
        const workbook = XLSX.read(new Uint8Array(ab), { type: "array" });
        sheetNames = workbook.SheetNames;
        binarySheets = workbook.SheetNames.map((name) => {
          const sheet = workbook.Sheets[name];
          return XLSX.utils.sheet_to_json<string[]>(sheet, { header: 1, defval: "" });
        });
        binaryLoading = false;
      } catch (e) {
        binaryError = e instanceof Error ? e.message : String(e);
        binaryLoading = false;
      }
    })();
  });

  // RFC 4180-ish CSV parser handling quoted fields
  function parseCsv(text: string, sep: string): string[][] {
    const rows: string[][] = [];
    let row: string[] = [];
    let field = "";
    let inQuotes = false;
    let i = 0;

    while (i < text.length) {
      const ch = text[i];

      if (inQuotes) {
        if (ch === '"') {
          if (i + 1 < text.length && text[i + 1] === '"') {
            field += '"';
            i += 2;
          } else {
            inQuotes = false;
            i++;
          }
        } else {
          field += ch;
          i++;
        }
      } else {
        if (ch === '"') {
          inQuotes = true;
          i++;
        } else if (ch === sep) {
          row.push(field);
          field = "";
          i++;
        } else if (ch === "\n" || ch === "\r") {
          row.push(field);
          field = "";
          if (ch === "\r" && i + 1 < text.length && text[i + 1] === "\n") i++;
          if (row.some((c) => c !== "")) rows.push(row);
          row = [];
          i++;
        } else {
          field += ch;
          i++;
        }
      }
    }

    // Final field/row
    row.push(field);
    if (row.some((c) => c !== "")) rows.push(row);

    return rows;
  }

  let allRows = $derived.by(() => {
    if (isBinary && binarySheets.length > 0) {
      return (binarySheets[activeSheet] ?? []).map((row) => row.map(String));
    }
    return parseCsv(content, delimiter);
  });
  let headers = $derived(allRows.length > 0 ? allRows[0] : []);
  let dataRows = $derived(allRows.slice(1));
  let colCount = $derived(headers.length);

  // Filtered rows
  let filteredRows = $derived.by(() => {
    if (!searchQuery.trim()) return dataRows;
    const q = searchQuery.toLowerCase();
    return dataRows.filter((row) => row.some((cell) => cell.toLowerCase().includes(q)));
  });

  // Sorted rows
  let sortedRows = $derived.by(() => {
    if (sortCol < 0) return filteredRows;
    const col = sortCol;
    return [...filteredRows].sort((a, b) => {
      const va = a[col] ?? "";
      const vb = b[col] ?? "";
      // Try numeric comparison
      const na = Number(va);
      const nb = Number(vb);
      if (!isNaN(na) && !isNaN(nb)) {
        return sortAsc ? na - nb : nb - na;
      }
      const cmp = va.localeCompare(vb, undefined, { sensitivity: "base" });
      return sortAsc ? cmp : -cmp;
    });
  });

  function toggleSort(col: number) {
    if (sortCol === col) {
      sortAsc = !sortAsc;
    } else {
      sortCol = col;
      sortAsc = true;
    }
  }

  async function copyContent() {
    try {
      await navigator.clipboard.writeText(content);
      copied = true;
      setTimeout(() => { copied = false; }, 2000);
    } catch {
      // noop
    }
  }

  function isNumeric(val: string): boolean {
    return val !== "" && !isNaN(Number(val));
  }
</script>

<div class="flex h-full flex-col">
  <!-- Toolbar -->
  <div class="flex items-center justify-between border-b border-border/50 px-3 py-1.5">
    <div class="flex items-center gap-3">
      <div class="flex rounded-md border border-border/50 p-0.5">
        <button
          type="button"
          class={cn(
            "rounded px-2 py-0.5 text-xs transition-colors",
            mode === "table"
              ? "bg-muted text-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
          onclick={() => (mode = "table")}
        >
          <span class="flex items-center gap-1">
            <Table class="h-3 w-3" />
            Table
          </span>
        </button>
        <button
          type="button"
          class={cn(
            "rounded px-2 py-0.5 text-xs transition-colors",
            mode === "source"
              ? "bg-muted text-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
          onclick={() => (mode = "source")}
        >
          <span class="flex items-center gap-1">
            <Code class="h-3 w-3" />
            Source
          </span>
        </button>
      </div>

      {#if mode === "table"}
        <span class="text-xs text-muted-foreground">
          {filteredRows.length.toLocaleString()} rows &middot; {colCount} columns
        </span>
      {/if}
    </div>

    <div class="flex items-center gap-2">
      {#if mode === "table"}
        <!-- Search -->
        <div class="relative">
          <Search class="absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-muted-foreground" />
          <input
            type="text"
            placeholder="Filter..."
            class="h-6 w-40 rounded-md border border-border/50 bg-muted/30 pl-7 pr-2 text-xs text-foreground placeholder:text-muted-foreground focus:border-primary/50 focus:outline-none"
            bind:value={searchQuery}
          />
        </div>
      {/if}
      <button
        type="button"
        class="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
        onclick={copyContent}
      >
        {#if copied}
          <Check class="h-3 w-3" />
          Copied
        {:else}
          <Copy class="h-3 w-3" />
          Copy
        {/if}
      </button>
    </div>
  </div>

  {#if mode === "source"}
    <div class="flex-1 overflow-hidden">
      {#await import("./CodeEditor.svelte") then mod}
        <mod.default {content} {extension} readonly={true} />
      {/await}
    </div>
  {:else}
    <!-- Sheet tabs for multi-sheet workbooks -->
    {#if isBinary && sheetNames.length > 1}
      <div class="flex gap-0.5 border-b border-border/50 px-3 py-1">
        {#each sheetNames as name, i}
          <button
            type="button"
            class={cn(
              "rounded-t px-3 py-1 text-xs transition-colors",
              activeSheet === i
                ? "bg-muted text-foreground font-medium"
                : "text-muted-foreground hover:text-foreground hover:bg-muted/50",
            )}
            onclick={() => { activeSheet = i; sortCol = -1; searchQuery = ""; }}
          >
            {name}
          </button>
        {/each}
      </div>
    {/if}

    {#if binaryLoading}
      <div class="flex flex-1 items-center justify-center text-sm text-muted-foreground">
        Loading spreadsheet...
      </div>
    {:else if binaryError}
      <div class="flex flex-1 items-center justify-center text-sm text-red-400">
        {binaryError}
      </div>
    {/if}

    <!-- Table view -->
    <div class="flex-1 overflow-auto">
      {#if allRows.length === 0}
        <div class="flex h-full items-center justify-center text-sm text-muted-foreground">
          No data
        </div>
      {:else}
        <table class="spreadsheet-table w-full border-collapse text-sm">
          <thead class="sticky top-0 z-10">
            <tr>
              <!-- Row number header -->
              <th class="row-num-header">#</th>
              {#each headers as header, i}
                <th>
                  <button
                    type="button"
                    class="flex w-full items-center gap-1"
                    onclick={() => toggleSort(i)}
                  >
                    <span class="truncate">{header || `Col ${i + 1}`}</span>
                    {#if sortCol === i}
                      <ArrowUpDown class="h-3 w-3 shrink-0 text-primary" />
                    {/if}
                  </button>
                </th>
              {/each}
            </tr>
          </thead>
          <tbody>
            {#each sortedRows as row, rowIdx}
              <tr>
                <td class="row-num">{rowIdx + 1}</td>
                {#each headers as _, colIdx}
                  {@const cell = row[colIdx] ?? ""}
                  <td class={cn(isNumeric(cell) && "text-right tabular-nums")}>
                    {cell}
                  </td>
                {/each}
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </div>
  {/if}
</div>

<style>
  .spreadsheet-table {
    font-family: "Inter Variable", "Inter", system-ui, sans-serif;
    font-size: 13px;
  }

  .spreadsheet-table :global(thead th) {
    background: color-mix(in oklab, var(--muted) 80%, var(--background));
    border: 1px solid var(--border);
    padding: 6px 10px;
    font-weight: 600;
    font-size: 12px;
    text-align: left;
    white-space: nowrap;
    color: var(--foreground);
    user-select: none;
  }

  .spreadsheet-table :global(tbody td) {
    border: 1px solid color-mix(in oklab, var(--border) 60%, transparent);
    padding: 4px 10px;
    white-space: nowrap;
    max-width: 300px;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .spreadsheet-table :global(tbody tr:nth-child(even) td) {
    background: color-mix(in oklab, var(--foreground) 2%, transparent);
  }

  .spreadsheet-table :global(tbody tr:hover td) {
    background: color-mix(in oklab, var(--primary) 8%, transparent);
  }

  .row-num-header {
    width: 48px;
    min-width: 48px;
    text-align: center;
    color: var(--muted-foreground);
    font-size: 11px;
  }

  .row-num {
    width: 48px;
    min-width: 48px;
    text-align: center;
    color: var(--muted-foreground);
    font-size: 11px;
    background: color-mix(in oklab, var(--muted) 40%, var(--background)) !important;
    font-variant-numeric: tabular-nums;
    user-select: none;
  }
</style>

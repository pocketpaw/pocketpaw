import type { FileEntry, DefaultDirs, FileChangeEvent } from "$lib/filesystem";
import { localFs, joinPath, getFileName, parentDir, invalidateThumbnail, isImageFile } from "$lib/filesystem";
import type { WSOpenPath } from "$lib/api/types";
import { connectionStore } from "./connection.svelte";

export interface Breadcrumb {
  name: string;
  path: string;
}

export interface PinnedFolder {
  path: string;
  name: string;
  source: string;
}

export type FileTypeCategory =
  | "images"
  | "documents"
  | "code"
  | "audio"
  | "video"
  | "archives"
  | "data";

const FILE_TYPE_EXTENSIONS: Record<FileTypeCategory, Set<string>> = {
  images: new Set([
    "png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico", "tiff", "tif", "avif",
  ]),
  documents: new Set([
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "txt", "rtf", "epub",
  ]),
  code: new Set([
    "ts", "tsx", "js", "jsx", "py", "rs", "go", "java", "c", "cpp", "h", "hpp", "cs", "rb",
    "php", "swift", "kt", "scala", "vue", "svelte", "html", "css", "scss", "less", "json",
    "yaml", "yml", "toml", "xml", "md", "sh", "bash", "zsh", "ps1", "bat", "sql",
  ]),
  audio: new Set(["mp3", "wav", "flac", "ogg", "aac", "m4a", "wma", "opus"]),
  video: new Set(["mp4", "mkv", "avi", "mov", "webm", "wmv", "flv", "m4v"]),
  archives: new Set(["zip", "tar", "gz", "bz2", "xz", "7z", "rar", "zst"]),
  data: new Set(["csv", "tsv", "parquet", "sqlite", "db", "jsonl", "ndjson"]),
};

export interface ExplorerTab {
  id: string;
  path: string;
  source: "local" | "remote" | "cloud";
  files: FileEntry[];
  searchQuery: string;
  isRecursiveSearch: boolean;
  recursiveResults: FileEntry[];
  recursiveSearchLoading: boolean;
  typeFilters: Set<FileTypeCategory>;
  selectedFiles: Set<string>;
  openFile: FileEntry | null;
  history: string[];
  historyIndex: number;
  focusedIndex: number;
  error: string | null;
  isLoading: boolean;
}

function createTab(path = "", source: "local" | "remote" | "cloud" = "local"): ExplorerTab {
  return {
    id: crypto.randomUUID(),
    path,
    source,
    files: [],
    searchQuery: "",
    isRecursiveSearch: false,
    recursiveResults: [],
    recursiveSearchLoading: false,
    typeFilters: new Set(),
    selectedFiles: new Set(),
    openFile: null,
    history: path ? [path] : [],
    historyIndex: path ? 0 : -1,
    focusedIndex: -1,
    error: null,
    isLoading: false,
  };
}

const PINNED_KEY = "pocketpaw_pinned_folders";
const VIEW_KEY = "pocketpaw_explorer_view";
const TABS_KEY = "pocketpaw_explorer_tabs";

class ExplorerStore {
  // ─── Tab State ─────────────────────────────────────────────
  tabs = $state<ExplorerTab[]>([]);
  activeTabId = $state("");

  // ─── Shared State (not per-tab) ─────────────────────────────
  viewMode = $state<"icon" | "grid" | "list" | "column" | "gallery">("icon");
  sortBy = $state<"name" | "modified" | "size" | "type">("name");
  sortAsc = $state(true);
  chatSidebarOpen = $state(true);
  defaultDirs = $state<DefaultDirs | null>(null);
  pinnedFolders = $state<PinnedFolder[]>([]);
  renamingFile = $state<string | null>(null);
  clipboardFiles = $state<Set<string>>(new Set());
  clipboardMode = $state<"copy" | "cut" | null>(null);
  openFileChangedOnDisk = $state(0);
  webPreviewUrl = $state<string | null>(null);

  private unwatchFn: (() => void) | null = null;
  private debounceTimer: ReturnType<typeof setTimeout> | null = null;
  private recursiveSearchTimer: ReturnType<typeof setTimeout> | null = null;
  private unsubWs: (() => void)[] = [];

  // ─── Active Tab (derived) ──────────────────────────────────
  activeTab = $derived(this.tabs.find((t) => t.id === this.activeTabId) ?? this.tabs[0]);

  // ─── Backward-Compatible Derived Proxies ───────────────────
  // All existing components (FileGrid, FileList, etc.) read these unchanged.
  currentPath = $derived(this.activeTab?.path ?? "");
  currentSource = $derived(this.activeTab?.source ?? "local");
  files = $derived(this.activeTab?.files ?? []);
  selectedFiles = $derived(this.activeTab?.selectedFiles ?? new Set<string>());
  searchQuery = $derived(this.activeTab?.searchQuery ?? "");
  openFile = $derived(this.activeTab?.openFile ?? null);
  history = $derived(this.activeTab?.history ?? []);
  historyIndex = $derived(this.activeTab?.historyIndex ?? -1);
  focusedIndex = $derived(this.activeTab?.focusedIndex ?? -1);
  isLoading = $derived(this.activeTab?.isLoading ?? false);
  error = $derived(this.activeTab?.error ?? null);
  isRecursiveSearch = $derived(this.activeTab?.isRecursiveSearch ?? false);
  recursiveResults = $derived(this.activeTab?.recursiveResults ?? []);
  recursiveSearchLoading = $derived(this.activeTab?.recursiveSearchLoading ?? false);
  typeFilters = $derived(this.activeTab?.typeFilters ?? new Set<FileTypeCategory>());

  // ─── Derived Computations ──────────────────────────────────
  filteredFiles = $derived.by(() => {
    const tab = this.activeTab;
    if (!tab) return [];

    // Start with base file list or recursive results
    let result: FileEntry[] =
      tab.isRecursiveSearch && tab.recursiveResults.length > 0
        ? tab.recursiveResults
        : tab.files;

    // Text filter (local filter always applies)
    const q = tab.searchQuery.trim().toLowerCase();
    if (q && !tab.isRecursiveSearch) {
      result = result.filter((f) => f.name.toLowerCase().includes(q));
    }

    // Type filter chips
    if (tab.typeFilters.size > 0) {
      result = result.filter((f) => {
        if (f.isDir) return true; // Always show directories
        const ext = f.extension.toLowerCase();
        for (const category of tab.typeFilters) {
          if (FILE_TYPE_EXTENSIONS[category].has(ext)) return true;
        }
        return false;
      });
    }

    return result;
  });

  sortedFiles = $derived.by(() => {
    const sorted = [...this.filteredFiles].sort((a, b) => {
      // Directories always first
      if (a.isDir !== b.isDir) return a.isDir ? -1 : 1;

      let cmp = 0;
      switch (this.sortBy) {
        case "name":
          cmp = a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
          break;
        case "modified":
          cmp = a.modified - b.modified;
          break;
        case "size":
          cmp = a.size - b.size;
          break;
        case "type":
          cmp = a.extension.localeCompare(b.extension);
          break;
      }
      return this.sortAsc ? cmp : -cmp;
    });
    return sorted;
  });

  isHome = $derived(this.currentPath === "");
  isDetailView = $derived(this.openFile !== null);
  canGoBack = $derived(this.historyIndex > 0);
  canGoForward = $derived(this.historyIndex < this.history.length - 1);

  breadcrumbs = $derived.by((): Breadcrumb[] => {
    if (!this.currentPath) return [];
    const normalized = this.currentPath.replace(/\\/g, "/");
    const parts = normalized.split("/").filter(Boolean);
    const crumbs: Breadcrumb[] = [];

    let accumulated = "";
    for (let i = 0; i < parts.length; i++) {
      if (i === 0 && parts[0].endsWith(":")) {
        accumulated = parts[0] + "/";
      } else {
        accumulated += (accumulated.endsWith("/") ? "" : "/") + parts[i];
      }
      crumbs.push({ name: parts[i], path: accumulated });
    }
    return crumbs;
  });

  // ─── Status Bar Derived ────────────────────────────────────
  totalItemCount = $derived(this.filteredFiles.length);
  selectionCount = $derived(this.selectedFiles.size);
  totalSize = $derived(this.filteredFiles.reduce((sum, f) => sum + (f.isDir ? 0 : f.size), 0));
  selectedSize = $derived.by(() => {
    if (this.selectionCount === 0) return 0;
    const tab = this.activeTab;
    if (!tab) return 0;
    let sum = 0;
    for (const path of tab.selectedFiles) {
      const f = tab.files.find((file) => file.path === path);
      if (f && !f.isDir) sum += f.size;
    }
    return sum;
  });

  // ─── Private Helpers ───────────────────────────────────────
  private updateActiveTab(updater: (tab: ExplorerTab) => void): void {
    const tab = this.tabs.find((t) => t.id === this.activeTabId);
    if (tab) updater(tab);
  }

  // ─── Tab Management ────────────────────────────────────────
  createNewTab(path = "", source: "local" | "remote" | "cloud" = "local"): string {
    const tab = createTab(path, source);
    this.tabs = [...this.tabs, tab];
    this.activeTabId = tab.id;
    if (path) {
      this.loadTabDirectory(tab.id, path);
    }
    return tab.id;
  }

  closeTab(tabId: string): void {
    if (this.tabs.length <= 1) return; // Don't close last tab
    const idx = this.tabs.findIndex((t) => t.id === tabId);
    if (idx === -1) return;

    this.tabs = this.tabs.filter((t) => t.id !== tabId);

    if (this.activeTabId === tabId) {
      // Activate the tab to the left, or the first tab
      const newIdx = Math.min(idx, this.tabs.length - 1);
      this.activeTabId = this.tabs[newIdx].id;
      this.onTabActivated();
    }
  }

  activateTab(tabId: string): void {
    if (this.activeTabId === tabId) return;
    this.activeTabId = tabId;
    this.onTabActivated();
  }

  reorderTabs(fromIndex: number, toIndex: number): void {
    const newTabs = [...this.tabs];
    const [moved] = newTabs.splice(fromIndex, 1);
    newTabs.splice(toIndex, 0, moved);
    this.tabs = newTabs;
  }

  duplicateTab(tabId: string): void {
    const src = this.tabs.find((t) => t.id === tabId);
    if (!src) return;
    const dup = createTab(src.path, src.source);
    dup.files = [...src.files];
    dup.history = [...src.history];
    dup.historyIndex = src.historyIndex;
    this.tabs = [...this.tabs, dup];
    this.activeTabId = dup.id;
    if (dup.path) this.startWatching(dup.path);
  }

  closeOtherTabs(tabId: string): void {
    this.tabs = this.tabs.filter((t) => t.id === tabId);
    this.activeTabId = tabId;
    this.onTabActivated();
  }

  closeTabsToRight(tabId: string): void {
    const idx = this.tabs.findIndex((t) => t.id === tabId);
    if (idx === -1) return;
    this.tabs = this.tabs.slice(0, idx + 1);
    if (!this.tabs.find((t) => t.id === this.activeTabId)) {
      this.activeTabId = tabId;
      this.onTabActivated();
    }
  }

  nextTab(): void {
    const idx = this.tabs.findIndex((t) => t.id === this.activeTabId);
    const next = (idx + 1) % this.tabs.length;
    this.activateTab(this.tabs[next].id);
  }

  prevTab(): void {
    const idx = this.tabs.findIndex((t) => t.id === this.activeTabId);
    const prev = (idx - 1 + this.tabs.length) % this.tabs.length;
    this.activateTab(this.tabs[prev].id);
  }

  private async onTabActivated(): Promise<void> {
    const tab = this.activeTab;
    if (!tab) return;
    // Restart watcher for the active tab's directory
    this.stopWatching();
    if (tab.path) {
      // Auto-refresh when switching tabs
      try {
        tab.isLoading = true;
        tab.files = await localFs.readDir(tab.path);
        tab.isLoading = false;
      } catch {
        tab.isLoading = false;
      }
      await this.startWatching(tab.path);
    }
  }

  private async loadTabDirectory(tabId: string, path: string): Promise<void> {
    const tab = this.tabs.find((t) => t.id === tabId);
    if (!tab) return;
    tab.isLoading = true;
    tab.error = null;
    try {
      tab.files = await localFs.readDir(path);
      if (tabId === this.activeTabId) {
        await this.startWatching(path);
      }
    } catch (e) {
      tab.error = e instanceof Error ? e.message : String(e);
    } finally {
      tab.isLoading = false;
    }
  }

  // ─── Recursive Search ──────────────────────────────────────
  setRecursiveSearch(enabled: boolean): void {
    this.updateActiveTab((tab) => {
      tab.isRecursiveSearch = enabled;
      tab.recursiveResults = [];
      tab.recursiveSearchLoading = false;
      if (enabled && tab.searchQuery.trim()) {
        this.performRecursiveSearch();
      }
    });
  }

  toggleTypeFilter(category: FileTypeCategory): void {
    this.updateActiveTab((tab) => {
      const next = new Set(tab.typeFilters);
      if (next.has(category)) {
        next.delete(category);
      } else {
        next.add(category);
      }
      tab.typeFilters = next;
    });
  }

  clearTypeFilters(): void {
    this.updateActiveTab((tab) => {
      tab.typeFilters = new Set();
    });
  }

  performRecursiveSearch(): void {
    const tab = this.activeTab;
    if (!tab || !tab.path || !tab.searchQuery.trim()) return;

    if (this.recursiveSearchTimer) {
      clearTimeout(this.recursiveSearchTimer);
    }

    this.recursiveSearchTimer = setTimeout(async () => {
      this.recursiveSearchTimer = null;
      const currentTab = this.activeTab;
      if (!currentTab || currentTab.id !== tab.id) return;

      currentTab.recursiveSearchLoading = true;
      try {
        const result = await localFs.searchRecursive(
          currentTab.path,
          currentTab.searchQuery.trim(),
        );
        // Only update if still the same tab and query
        if (this.activeTabId === currentTab.id) {
          currentTab.recursiveResults = result.entries;
          currentTab.recursiveSearchLoading = false;
        }
      } catch (e) {
        console.error("Recursive search failed:", e);
        if (this.activeTabId === currentTab.id) {
          currentTab.recursiveSearchLoading = false;
        }
      }
    }, 300);
  }

  // ─── Initialize ────────────────────────────────────────────
  async initialize(): Promise<void> {
    // Load persisted view mode
    const savedView = localStorage.getItem(VIEW_KEY);
    if (savedView) {
      this.viewMode = savedView as typeof this.viewMode;
    }

    // Load pinned folders
    try {
      const raw = localStorage.getItem(PINNED_KEY);
      if (raw) {
        this.pinnedFolders = JSON.parse(raw);
      }
    } catch {
      // Ignore parse errors
    }

    // Load default dirs
    try {
      this.defaultDirs = await localFs.getDefaultDirs();
    } catch {
      // Not in Tauri or error — leave null
    }

    // Create initial tab
    if (this.tabs.length === 0) {
      const tab = createTab();
      this.tabs = [tab];
      this.activeTabId = tab.id;
    }
  }

  // ─── Navigation ────────────────────────────────────────────
  async navigateTo(path: string, source?: "local" | "remote" | "cloud"): Promise<void> {
    this.updateActiveTab((tab) => {
      tab.isLoading = true;
      tab.error = null;
      tab.openFile = null;
      tab.selectedFiles = new Set();
      tab.searchQuery = "";
      tab.focusedIndex = -1;
      tab.isRecursiveSearch = false;
      tab.recursiveResults = [];
      tab.typeFilters = new Set();
    });

    try {
      const s = source ?? this.currentSource;
      const entries = await localFs.readDir(path);
      this.updateActiveTab((tab) => {
        tab.files = entries;
        tab.path = path;
        tab.source = s;

        // Push to history
        if (tab.historyIndex < tab.history.length - 1) {
          tab.history = tab.history.slice(0, tab.historyIndex + 1);
        }
        tab.history = [...tab.history, path];
        tab.historyIndex = tab.history.length - 1;
        tab.isLoading = false;
      });

      // Start watching
      await this.startWatching(path);
    } catch (e) {
      this.updateActiveTab((tab) => {
        tab.error = e instanceof Error ? e.message : String(e);
        tab.isLoading = false;
      });
    }
  }

  async refresh(): Promise<void> {
    const tab = this.activeTab;
    if (!tab || !tab.path) return;
    tab.isLoading = true;
    tab.error = null;
    try {
      tab.files = await localFs.readDir(tab.path);
    } catch (e) {
      tab.error = e instanceof Error ? e.message : String(e);
    } finally {
      tab.isLoading = false;
    }
  }

  goBack(): void {
    if (!this.canGoBack) return;
    this.updateActiveTab((tab) => {
      tab.historyIndex--;
      const path = tab.history[tab.historyIndex];
      this.loadWithoutHistory(path);
    });
  }

  goForward(): void {
    if (!this.canGoForward) return;
    this.updateActiveTab((tab) => {
      tab.historyIndex++;
      const path = tab.history[tab.historyIndex];
      this.loadWithoutHistory(path);
    });
  }

  goHome(): void {
    this.updateActiveTab((tab) => {
      tab.path = "";
      tab.files = [];
      tab.openFile = null;
      tab.selectedFiles = new Set();
      tab.searchQuery = "";
      tab.focusedIndex = -1;
      tab.error = null;
      tab.isRecursiveSearch = false;
      tab.recursiveResults = [];
      tab.typeFilters = new Set();
    });
    this.stopWatching();
  }

  openFileDetail(file: FileEntry): void {
    this.updateActiveTab((tab) => {
      tab.openFile = file;
    });
  }

  closeDetail(): void {
    this.updateActiveTab((tab) => {
      tab.openFile = null;
    });
  }

  selectFile(path: string, multi = false): void {
    this.updateActiveTab((tab) => {
      if (multi) {
        const next = new Set(tab.selectedFiles);
        if (next.has(path)) {
          next.delete(path);
        } else {
          next.add(path);
        }
        tab.selectedFiles = next;
      } else {
        tab.selectedFiles = new Set([path]);
      }
    });
  }

  selectAll(): void {
    this.updateActiveTab((tab) => {
      tab.selectedFiles = new Set(tab.files.map((f) => f.path));
    });
  }

  clearSelection(): void {
    this.updateActiveTab((tab) => {
      tab.selectedFiles = new Set();
    });
  }

  copyToClipboard(): void {
    if (this.selectedFiles.size === 0) return;
    this.clipboardFiles = new Set(this.selectedFiles);
    this.clipboardMode = "copy";
  }

  cutToClipboard(): void {
    if (this.selectedFiles.size === 0) return;
    this.clipboardFiles = new Set(this.selectedFiles);
    this.clipboardMode = "cut";
  }

  async paste(): Promise<void> {
    if (this.clipboardFiles.size === 0 || !this.clipboardMode || !this.currentPath) return;
    try {
      for (const srcPath of this.clipboardFiles) {
        const name = getFileName(srcPath);
        const destPath = joinPath(this.currentPath, name);
        const stat = await localFs.stat(srcPath);
        if (this.clipboardMode === "copy") {
          if (stat.isDir) {
            await localFs.copyDir(srcPath, destPath);
          } else {
            await localFs.copyFile(srcPath, destPath);
          }
        } else {
          await localFs.moveFile(srcPath, destPath);
        }
      }
      if (this.clipboardMode === "cut") {
        this.clipboardFiles = new Set();
        this.clipboardMode = null;
      }
      await this.refresh();
    } catch (e) {
      console.error("Paste failed:", e);
    }
  }

  moveFocus(delta: number): void {
    const files = this.sortedFiles;
    if (files.length === 0) return;
    this.updateActiveTab((tab) => {
      let next = tab.focusedIndex + delta;
      if (next < 0) next = 0;
      if (next >= files.length) next = files.length - 1;
      tab.focusedIndex = next;
      tab.selectedFiles = new Set([files[next].path]);
    });
  }

  setViewMode(mode: typeof this.viewMode): void {
    this.viewMode = mode;
    localStorage.setItem(VIEW_KEY, mode);
  }

  setSortBy(field: "name" | "modified" | "size" | "type"): void {
    if (this.sortBy === field) {
      this.sortAsc = !this.sortAsc;
    } else {
      this.sortBy = field;
      this.sortAsc = true;
    }
  }

  openWebPreview(url: string): void {
    this.webPreviewUrl = url;
  }

  closeWebPreview(): void {
    this.webPreviewUrl = null;
  }

  toggleChatSidebar(): void {
    this.chatSidebarOpen = !this.chatSidebarOpen;
  }

  setSearchQuery(query: string): void {
    this.updateActiveTab((tab) => {
      tab.searchQuery = query;
      if (tab.isRecursiveSearch && query.trim()) {
        this.performRecursiveSearch();
      }
      if (!query.trim()) {
        tab.recursiveResults = [];
      }
    });
  }

  startRename(filePath: string): void {
    this.renamingFile = filePath;
  }

  cancelRename(): void {
    this.renamingFile = null;
  }

  async commitRename(oldPath: string, newName: string): Promise<void> {
    if (!newName.trim() || newName.includes("/") || newName.includes("\\")) {
      this.renamingFile = null;
      return;
    }
    try {
      const { parentDir, joinPath } = await import("$lib/filesystem");
      const dir = parentDir(oldPath);
      const newPath = joinPath(dir, newName);
      await localFs.rename(oldPath, newPath);
      this.renamingFile = null;
      await this.refresh();
    } catch (e) {
      console.error("Rename failed:", e);
      this.renamingFile = null;
    }
  }

  async createFolder(name: string): Promise<void> {
    if (!this.currentPath || !name.trim()) return;
    try {
      const { joinPath } = await import("$lib/filesystem");
      const newPath = joinPath(this.currentPath, name);
      await localFs.createDir(newPath);
      await this.refresh();
    } catch (e) {
      console.error("Create folder failed:", e);
    }
  }

  async createFile(name: string): Promise<void> {
    if (!this.currentPath || !name.trim()) return;
    try {
      const { joinPath } = await import("$lib/filesystem");
      const newPath = joinPath(this.currentPath, name);
      await localFs.writeFile(newPath, "");
      await this.refresh();
    } catch (e) {
      console.error("Create file failed:", e);
    }
  }

  pinFolder(path: string, name: string, source: string): void {
    if (this.pinnedFolders.some((p) => p.path === path)) return;
    this.pinnedFolders = [...this.pinnedFolders, { path, name, source }];
    localStorage.setItem(PINNED_KEY, JSON.stringify(this.pinnedFolders));
  }

  unpinFolder(path: string): void {
    this.pinnedFolders = this.pinnedFolders.filter((p) => p.path !== path);
    localStorage.setItem(PINNED_KEY, JSON.stringify(this.pinnedFolders));
  }

  // ─── Open Path (backend → client signal) ───────────────────
  handleOpenPath(path: string, action: "navigate" | "view"): void {
    if (action === "navigate") {
      this.navigateTo(path);
    } else if (action === "view") {
      // Navigate to parent dir, then open the file in the detail viewer
      this.openFileByPath(path);
    }
  }

  /** Open a file by absolute path in the explorer's detail viewer. */
  async openFileByPath(absolutePath: string): Promise<void> {
    const parent = parentDir(absolutePath);
    if (!parent) return;

    await this.navigateTo(parent);

    // Find the file in the loaded directory listing
    const name = absolutePath.replace(/\\/g, "/").split("/").pop();
    const file = this.files.find(
      (f) => f.path === absolutePath || f.name === name,
    );
    if (file) {
      this.openFileDetail(file);
    } else {
      // File might be hidden (dotfile) or not in the first 50 entries.
      // Create a synthetic entry so the viewer can still open it.
      const ext = (name?.split(".").pop() || "").toLowerCase();
      let size = 0;
      let modified = Date.now();
      try {
        const stat = await localFs.stat(absolutePath);
        size = stat.size;
        modified = stat.modified;
      } catch {
        // stat unavailable
      }
      this.openFileDetail({
        name: name || absolutePath,
        path: absolutePath,
        isDir: false,
        size,
        modified,
        extension: ext,
        source: "local",
      });
    }
  }

  bindEvents(): void {
    this.unbindEvents();
    try {
      const ws = connectionStore.getWebSocket();
      this.unsubWs.push(
        ws.on("open_path" as any, (event: any) => {
          const e = event as WSOpenPath;
          this.handleOpenPath(e.path, e.action);
        }),
      );
    } catch {
      // WS not available yet
    }
  }

  unbindEvents(): void {
    for (const unsub of this.unsubWs) unsub();
    this.unsubWs = [];
  }

  // ─── Private ───────────────────────────────────────────────
  private async loadWithoutHistory(path: string): Promise<void> {
    this.updateActiveTab((tab) => {
      tab.isLoading = true;
      tab.error = null;
      tab.openFile = null;
      tab.selectedFiles = new Set();
      tab.searchQuery = "";
      tab.focusedIndex = -1;
      tab.isRecursiveSearch = false;
      tab.recursiveResults = [];
      tab.typeFilters = new Set();
    });
    try {
      const entries = await localFs.readDir(path);
      this.updateActiveTab((tab) => {
        tab.files = entries;
        tab.path = path;
        tab.isLoading = false;
      });
      await this.startWatching(path);
    } catch (e) {
      this.updateActiveTab((tab) => {
        tab.error = e instanceof Error ? e.message : String(e);
        tab.isLoading = false;
      });
    }
  }

  private debouncedRefresh(): void {
    if (this.debounceTimer) clearTimeout(this.debounceTimer);
    this.debounceTimer = setTimeout(() => {
      this.debounceTimer = null;
      this.refresh();
    }, 400);
  }

  private async startWatching(path: string): Promise<void> {
    this.stopWatching();
    try {
      this.unwatchFn = await localFs.watch(path, (event: FileChangeEvent) => {
        const eventPath = event.path.replace(/\\/g, "/");

        // On image modify, invalidate its thumbnail
        if (event.kind === "modify" && !event.is_dir) {
          const ext = eventPath.split(".").pop()?.toLowerCase() ?? "";
          if (isImageFile(ext)) {
            invalidateThumbnail(event.path);
          }
        }

        // If the open file was modified, signal the viewer
        const tab = this.activeTab;
        if (event.kind === "modify" && tab?.openFile) {
          const openPath = tab.openFile.path.replace(/\\/g, "/");
          if (eventPath === openPath) {
            this.openFileChangedOnDisk++;
          }
        }

        // Always debounce-refresh the directory listing
        this.debouncedRefresh();
      });
    } catch {
      // Watching not available
    }
  }

  private stopWatching(): void {
    if (this.debounceTimer) {
      clearTimeout(this.debounceTimer);
      this.debounceTimer = null;
    }
    if (this.unwatchFn) {
      this.unwatchFn();
      this.unwatchFn = null;
    }
  }
}

export const explorerStore = new ExplorerStore();

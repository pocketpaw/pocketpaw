import type { InstalledKit, KitCatalogEntry } from "$lib/types/pawkit";
import { connectionStore } from "./connection.svelte";

class KitStore {
  kits = $state<InstalledKit[]>([]);
  activeKitId = $state<string | null>(null);
  activeKit = $derived(this.kits.find((k) => k.id === this.activeKitId) ?? null);
  kitData = $state<Record<string, unknown>>({});
  isLoading = $state(false);

  // Catalog (Kit Store)
  catalog = $state<KitCatalogEntry[]>([]);
  isCatalogLoading = $state(false);

  private pollInterval: ReturnType<typeof setInterval> | null = null;

  async load(): Promise<void> {
    this.isLoading = true;
    try {
      const client = connectionStore.getClient();
      this.kits = await client.listKits();
      const active = this.kits.find((k) => k.active);
      this.activeKitId = active?.id ?? null;
    } catch (err) {
      console.error("[KitStore] Failed to load kits:", err);
    } finally {
      this.isLoading = false;
    }
  }

  async activate(kitId: string): Promise<void> {
    try {
      const client = connectionStore.getClient();
      await client.activateKit(kitId);
      this.activeKitId = kitId;
      // Update local state
      this.kits = this.kits.map((k) => ({ ...k, active: k.id === kitId }));
      await this.loadData(kitId);
    } catch (err) {
      console.error("[KitStore] Failed to activate kit:", err);
    }
  }

  async loadData(kitId: string): Promise<void> {
    try {
      const client = connectionStore.getClient();
      this.kitData = await client.getKitData(kitId);
    } catch (err) {
      console.error("[KitStore] Failed to load kit data:", err);
    }
  }

  async install(yaml: string): Promise<void> {
    try {
      const client = connectionStore.getClient();
      await client.installKit(yaml);
      await this.load();
    } catch (err) {
      console.error("[KitStore] Failed to install kit:", err);
      throw err;
    }
  }

  async remove(kitId: string): Promise<void> {
    try {
      const client = connectionStore.getClient();
      await client.removeKit(kitId);
      this.kits = this.kits.filter((k) => k.id !== kitId);
      if (this.activeKitId === kitId) {
        this.activeKitId = null;
        this.kitData = {};
      }
    } catch (err) {
      console.error("[KitStore] Failed to remove kit:", err);
      throw err;
    }
  }

  async loadCatalog(): Promise<void> {
    this.isCatalogLoading = true;
    try {
      const client = connectionStore.getClient();
      this.catalog = await client.listKitCatalog();
    } catch (err) {
      console.error("[KitStore] Failed to load catalog:", err);
    } finally {
      this.isCatalogLoading = false;
    }
  }

  async installFromCatalog(kitId: string): Promise<void> {
    try {
      const client = connectionStore.getClient();
      const result = await client.installCatalogKit(kitId);

      // Refresh both kits list and catalog
      await this.load();
      await this.loadCatalog();

      // Auto-activate the newly installed kit
      if (result.activated) {
        this.activeKitId = result.id;
        await this.loadData(result.id);
      }
    } catch (err) {
      console.error("[KitStore] Failed to install catalog kit:", err);
      throw err;
    }
  }

  startPolling(kitId: string): void {
    this.stopPolling();
    this.pollInterval = setInterval(() => {
      this.loadData(kitId);
    }, 30_000);
  }

  stopPolling(): void {
    if (this.pollInterval) {
      clearInterval(this.pollInterval);
      this.pollInterval = null;
    }
  }
}

export const kitStore = new KitStore();

import type { Skill } from "$lib/api";
import { connectionStore } from "./connection.svelte";

class SkillStore {
  skills = $state<Skill[]>([]);
  isLoading = $state(false);
  searchResults = $state<Skill[]>([]);
  searchQuery = $state("");

  async load(): Promise<void> {
    this.isLoading = true;
    try {
      const client = connectionStore.getClient();
      this.skills = await client.listSkills();
    } catch (err) {
      console.error("[SkillStore] Failed to load skills:", err);
    } finally {
      this.isLoading = false;
    }
  }

  async search(query: string): Promise<void> {
    this.searchQuery = query;
    if (!query.trim()) {
      this.searchResults = [];
      return;
    }
    try {
      const client = connectionStore.getClient();
      this.searchResults = await client.searchSkills(query);
    } catch (err) {
      console.error("[SkillStore] Failed to search skills:", err);
      this.searchResults = [];
    }
  }

  async install(identifier: string): Promise<void> {
    try {
      const client = connectionStore.getClient();
      await client.installSkill(identifier);
      // Reload the skills list after installation
      await this.load();
    } catch (err) {
      console.error("[SkillStore] Failed to install skill:", err);
      throw err;
    }
  }

  async remove(name: string): Promise<void> {
    try {
      const client = connectionStore.getClient();
      await client.removeSkill(name);
      this.skills = this.skills.filter((s) => s.name !== name);
    } catch (err) {
      console.error("[SkillStore] Failed to remove skill:", err);
      throw err;
    }
  }

  clearSearch(): void {
    this.searchQuery = "";
    this.searchResults = [];
  }
}

export const skillStore = new SkillStore();

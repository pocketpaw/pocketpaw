import type { Settings } from "$lib/api";
import { connectionStore } from "./connection.svelte";

class SettingsStore {
  settings = $state<Settings | null>(null);
  isLoading = $state(false);

  agentBackend = $derived(this.settings?.agent_backend ?? "claude_agent_sdk");

  model = $derived.by(() => {
    const s = this.settings;
    if (!s) return "";
    switch (s.agent_backend) {
      case "claude_agent_sdk": return s.claude_sdk_model ?? s.anthropic_model ?? "";
      case "openai_agents": return s.openai_agents_model ?? s.openai_model ?? "";
      case "google_adk": return s.google_adk_model ?? s.gemini_model ?? "";
      case "codex_cli": return s.codex_cli_model ?? "";
      case "copilot_sdk": return s.copilot_sdk_model ?? "";
      case "opencode": return s.opencode_model ?? "";
      default: return "";
    }
  });

  async load(): Promise<void> {
    this.isLoading = true;
    try {
      const client = connectionStore.getClient();
      this.settings = await client.getSettings();
    } catch (err) {
      console.error("[SettingsStore] Failed to load settings:", err);
    } finally {
      this.isLoading = false;
    }
  }

  async update(patch: Partial<Settings>): Promise<void> {
    try {
      const client = connectionStore.getClient();
      await client.updateSettings(patch);
      // Merge into local state
      if (this.settings) {
        this.settings = { ...this.settings, ...patch };
      }
    } catch (err) {
      console.error("[SettingsStore] Failed to update settings:", err);
      throw err;
    }
  }

  async saveApiKey(provider: string, key: string): Promise<void> {
    // Map provider display names to settings field names
    const providerFieldMap: Record<string, string> = {
      anthropic: "anthropic_api_key",
      openai: "openai_api_key",
      google: "google_api_key",
      gemini: "google_api_key",
      ollama: "ollama_host",
    };
    const fieldName = providerFieldMap[provider.toLowerCase()] ?? `${provider.toLowerCase()}_api_key`;
    try {
      await this.update({ [fieldName]: key } as Partial<Settings>);
      await this.load();
    } catch (err) {
      console.error("[SettingsStore] Failed to save API key:", err);
      throw err;
    }
  }
}

export const settingsStore = new SettingsStore();

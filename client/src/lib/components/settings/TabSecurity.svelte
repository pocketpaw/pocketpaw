<script lang="ts">
  import { settingsStore, connectionStore } from "$lib/stores";
  import { Switch } from "$lib/components/ui/switch";
  import { Shield, ScrollText, Loader2 } from "@lucide/svelte";
  import { toast } from "svelte-sonner";

  let auditEntries = $state<unknown[]>([]);
  let auditLoading = $state(false);
  let showAuditLog = $state(false);

  let guardianEnabled = $derived(settingsStore.settings?.injection_scan_enabled ?? false);
  let planMode = $derived(settingsStore.settings?.plan_mode ?? false);
  let selfAudit = $derived(settingsStore.settings?.self_audit_enabled ?? false);
  let toolProfile = $derived(settingsStore.settings?.tool_profile ?? "standard");

  async function toggleGuardian(checked: boolean) {
    try {
      await settingsStore.update({ injection_scan_enabled: checked });
      toast.success(`Guardian AI ${checked ? "enabled" : "disabled"}`);
    } catch {
      toast.error("Failed to update Guardian AI");
    }
  }

  async function togglePlanMode(checked: boolean) {
    try {
      await settingsStore.update({ plan_mode: checked });
      toast.success(`Plan Mode ${checked ? "enabled" : "disabled"}`);
    } catch {
      toast.error("Failed to update Plan Mode");
    }
  }

  async function toggleSelfAudit(checked: boolean) {
    try {
      await settingsStore.update({ self_audit_enabled: checked });
      toast.success(`Self Audit ${checked ? "enabled" : "disabled"}`);
    } catch {
      toast.error("Failed to update Self Audit");
    }
  }

  async function loadAuditLog() {
    auditLoading = true;
    showAuditLog = true;
    try {
      const client = connectionStore.getClient();
      auditEntries = await client.getAuditLog(10);
    } catch {
      auditEntries = [];
    } finally {
      auditLoading = false;
    }
  }
</script>

<div class="flex flex-col gap-6">
  <div class="flex items-center gap-2">
    <Shield class="h-4 w-4 text-muted-foreground" strokeWidth={1.75} />
    <h3 class="text-sm font-semibold text-foreground">Security</h3>
  </div>

  <!-- Guardian AI -->
  <div class="flex items-center justify-between">
    <div class="flex flex-col">
      <span class="text-sm text-foreground">Guardian AI</span>
      <span class="text-[10px] text-muted-foreground">
        Scans inputs for prompt injection attacks
      </span>
    </div>
    <Switch checked={guardianEnabled} onCheckedChange={toggleGuardian} />
  </div>

  <!-- Plan Mode -->
  <div class="flex items-center justify-between">
    <div class="flex flex-col">
      <span class="text-sm text-foreground">Plan Mode</span>
      <span class="text-[10px] text-muted-foreground">
        Agent proposes a plan before executing tools
      </span>
    </div>
    <Switch checked={planMode} onCheckedChange={togglePlanMode} />
  </div>

  <!-- Self Audit -->
  <div class="flex items-center justify-between">
    <div class="flex flex-col">
      <span class="text-sm text-foreground">Self Audit</span>
      <span class="text-[10px] text-muted-foreground">
        Periodically reviews its own behavior for anomalies
      </span>
    </div>
    <Switch checked={selfAudit} onCheckedChange={toggleSelfAudit} />
  </div>

  <!-- Tool Profile -->
  <div class="border-t border-border/50 pt-4">
    <div class="flex flex-col gap-1">
      <span class="text-xs font-medium text-muted-foreground">Tool Policy</span>
      <span class="text-sm text-foreground capitalize">{toolProfile}</span>
      <span class="text-[10px] text-muted-foreground">
        Controls which tools the agent can access
      </span>
    </div>
  </div>

  <!-- Audit Log -->
  <div class="border-t border-border/50 pt-4">
    <div class="flex items-center justify-between">
      <div class="flex items-center gap-2">
        <ScrollText class="h-3.5 w-3.5 text-muted-foreground" strokeWidth={1.75} />
        <span class="text-sm text-foreground">Audit Log</span>
      </div>
      <button
        onclick={loadAuditLog}
        class="text-xs text-primary transition-opacity hover:opacity-80"
      >
        {showAuditLog ? "Refresh" : "View"}
      </button>
    </div>

    {#if showAuditLog}
      <div class="mt-3 max-h-60 overflow-y-auto rounded-lg border border-border/50 bg-muted/30">
        {#if auditLoading}
          <div class="flex items-center justify-center py-4">
            <Loader2 class="h-4 w-4 animate-spin text-muted-foreground" />
          </div>
        {:else if auditEntries.length === 0}
          <p class="px-3 py-4 text-center text-xs text-muted-foreground">
            No audit entries yet
          </p>
        {:else}
          {#each auditEntries as entry, i (i)}
            <div class="border-b border-border/30 px-3 py-2 text-xs text-muted-foreground last:border-b-0">
              <pre class="whitespace-pre-wrap break-all font-mono text-[10px]">{JSON.stringify(entry, null, 2)}</pre>
            </div>
          {/each}
        {/if}
      </div>
    {/if}
  </div>
</div>

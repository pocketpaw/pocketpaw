<script lang="ts">
  import type { ChannelInfo } from "$lib/api";
  import { connectionStore } from "$lib/stores";
  import { cn } from "$lib/utils";
  import { Switch } from "$lib/components/ui/switch";
  import * as Select from "$lib/components/ui/select";
  import {
    Loader2,
    Info,
    QrCode,
    CheckCircle2,
    Download,
    Pencil,
    Plus,
    CircleDot,
    Send,
    Hash,
    Phone,
    Shield,
    AtSign,
    Building2,
    MessageCircle,
    FlaskConical,
    AlertTriangle,
  } from "@lucide/svelte";
  import * as Dialog from "$lib/components/ui/dialog";
  import { toast } from "svelte-sonner";
  import QRCode from "qrcode";
  import { CHANNEL_SCHEMA, CHANNEL_COLORS } from "./channel-schema";

  let {
    channel,
    info,
    onRefresh,
  }: {
    channel: string;
    info: ChannelInfo;
    onRefresh: () => void;
  } = $props();

  let configOpen = $state(false);
  let toggling = $state(false);
  let savingConfig = $state(false);
  let formValues = $state<Record<string, string>>({});
  let formAutostart = $state(false);
  let secretSaved = $state<Record<string, boolean>>({});
  let prefilling = $state(false);
  let testing = $state(false);

  // WhatsApp QR pairing state
  let pairingActive = $state(false);
  let qrSvg = $state<string | null>(null);
  let qrConnected = $state(false);
  let qrPollingId = $state<ReturnType<typeof setInterval> | null>(null);
  let installingDep = $state(false);
  let pairingStatus = $state("");

  // WhatsApp close guard
  let showCloseGuard = $state(false);
  let pendingCloseAction = $state<(() => void) | null>(null);

  // Missing dependency prompt state
  let missingDep = $state<{ package: string; pip_spec: string } | null>(null);

  // Inline error from last toggle failure
  let toggleError = $state<string | null>(null);

  let schema = $derived(CHANNEL_SCHEMA[channel]);
  let label = $derived(schema?.label ?? channel);
  let description = $derived(schema?.description ?? "");
  let colorKey = $derived(schema?.color ?? "sky");
  let colors = $derived(CHANNEL_COLORS[colorKey] ?? CHANNEL_COLORS.sky);

  // Icon component lookup
  const ICON_MAP: Record<string, typeof Send> = {
    Send,
    Hash,
    Phone,
    Shield,
    AtSign,
    Building2,
    MessageCircle,
  };
  let ChannelIcon = $derived(ICON_MAP[schema?.icon ?? ""] ?? Hash);

  let visibleFields = $derived.by(() => {
    if (!schema) return [];
    return schema.fields.filter((f) => {
      if (!f.showWhen) return true;
      return formValues[f.showWhen.field] === f.showWhen.value;
    });
  });

  let isWhatsAppPersonal = $derived(
    channel === "whatsapp" && formValues["mode"] === "personal",
  );

  let canSave = $derived.by(() => {
    if (isWhatsAppPersonal) return true;
    return visibleFields
      .filter((f) => f.required)
      .every((f) => {
        const val = (formValues[f.key] ?? "").trim();
        // If the field has a saved secret and the user hasn't typed anything, that's fine
        if (!val && secretSaved[f.key]) return true;
        return val !== "";
      });
  });

  function initForm() {
    const vals: Record<string, string> = {};
    if (schema) {
      for (const field of schema.fields) {
        if (field.type === "select" && field.options?.length) {
          vals[field.key] = field.options[0].value;
        } else {
          vals[field.key] = "";
        }
      }
    }
    formValues = vals;
    formAutostart = info.autostart;
    secretSaved = {};
    pairingActive = false;
    qrSvg = null;
    qrConnected = false;
    showCloseGuard = false;
    pendingCloseAction = null;
    stopQrPolling();
  }

  async function openDialog() {
    initForm();
    configOpen = true;

    // Attempt config prefill for configured channels
    if (info.configured) {
      prefilling = true;
      try {
        const client = connectionStore.getClient();
        const config = await client.getChannelConfig(channel);
        if (config) {
          // Merge returned values into form
          for (const [key, val] of Object.entries(config.values)) {
            if (key in formValues) {
              formValues[key] = val;
            }
          }
          // Track which secrets are saved on backend
          secretSaved = { ...config.has_secret };
        }
      } catch {
        // Silently no-op if endpoint missing
      } finally {
        prefilling = false;
      }
    }
  }

  function handleDialogClose(open: boolean) {
    if (!open && pairingActive && !qrConnected) {
      // WhatsApp pairing active — show close guard
      showCloseGuard = true;
      pendingCloseAction = () => {
        showCloseGuard = false;
        configOpen = false;
        stopQrPolling();
      };
      return;
    }
    configOpen = open;
    if (!open) stopQrPolling();
  }

  async function handleToggle(checked: boolean) {
    toggling = true;
    toggleError = null;
    try {
      const client = connectionStore.getClient();
      const res = await client.toggleChannel(channel, checked ? "start" : "stop");
      if (res?.missing_dep) {
        missingDep = { package: String(res.package ?? ""), pip_spec: String(res.pip_spec ?? "") };
        configOpen = true;
        return;
      } else if (res?.error) {
        toggleError = String(res.error);
        toast.error(String(res.error));
      }
      onRefresh();
    } catch {
      toggleError = `Failed to ${checked ? "start" : "stop"} ${label}`;
      toast.error(toggleError);
    } finally {
      toggling = false;
    }
  }

  async function handleAutostartToggle(checked: boolean) {
    try {
      const client = connectionStore.getClient();
      await client.saveChannel(channel, {}, checked);
      formAutostart = checked;
      onRefresh();
    } catch {
      toast.error("Failed to update autostart");
    }
  }

  async function handleInstallDep() {
    installingDep = true;
    try {
      const client = connectionStore.getClient();

      const preCheck = await client.checkExtra(channel);
      if (preCheck.installed) {
        missingDep = null;
      } else {
        const installRes = await client.installExtra(channel);
        if (installRes?.error) {
          toast.error(String(installRes.error));
          return;
        }

        const postCheck = await client.checkExtra(channel);
        if (!postCheck.installed) {
          toast.error("Install completed but dependency is still not available. Try restarting the backend.");
          return;
        }

        toast.success(`${missingDep?.package ?? label} installed`);
        missingDep = null;
      }

      const res = await client.toggleChannel(channel, "start");
      if (res?.missing_dep) {
        if (res.error) {
          toast.error(`Failed to start ${label}: ${res.error}`);
        } else {
          missingDep = { package: String(res.package ?? ""), pip_spec: String(res.pip_spec ?? "") };
        }
        return;
      }
      if (res?.error) {
        toast.error(String(res.error));
        return;
      }
      onRefresh();

      if (isWhatsAppPersonal) {
        await startPairing();
      }
    } catch {
      toast.error("Failed to install dependency");
    } finally {
      installingDep = false;
    }
  }

  function normalizeListValue(val: string): string {
    return val
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean)
      .join(", ");
  }

  async function handleSaveConfig() {
    savingConfig = true;
    try {
      const config: Record<string, string> = {};
      for (const field of visibleFields) {
        const raw = formValues[field.key] ?? "";
        if (!raw.trim()) continue;
        config[field.key] =
          field.type === "list" ? normalizeListValue(raw) : raw.trim();
      }
      if (schema) {
        for (const field of schema.fields) {
          if (field.type === "select" && formValues[field.key]) {
            config[field.key] = formValues[field.key];
          }
        }
      }
      const client = connectionStore.getClient();
      await client.saveChannel(channel, config, formAutostart);
      onRefresh();
      toast.success(`${label} configured`);

      if (isWhatsAppPersonal) {
        await startPairing();
      } else {
        configOpen = false;
      }
    } catch {
      toast.error(`Failed to save ${label} config`);
    } finally {
      savingConfig = false;
    }
  }

  async function handleTestChannel() {
    testing = true;
    try {
      const client = connectionStore.getClient();
      const result = await client.testChannel(channel);
      if (result.ok) {
        toast.success(`${label} connection test passed`);
      } else {
        toast.error(result.error ?? "Connection test failed");
      }
    } catch {
      toast.error("Connection test failed");
    } finally {
      testing = false;
    }
  }

  // ── WhatsApp QR Pairing ──────────────────────────────────────────

  async function startPairing() {
    pairingActive = true;
    qrConnected = false;
    qrSvg = null;
    installingDep = false;
    pairingStatus = "";

    try {
      const client = connectionStore.getClient();

      const check = await client.checkExtra(channel);
      if (!check.installed) {
        missingDep = {
          package: check.package ?? "neonize",
          pip_spec: check.pip_spec ?? "pocketpaw[whatsapp-personal]",
        };
        pairingActive = false;
        return;
      }

      if (!info.running) {
        const res = await client.toggleChannel("whatsapp", "start");

        if (res?.missing_dep) {
          if (res.error) {
            toast.error(`Failed to start ${label}: ${res.error}`);
          } else {
            missingDep = { package: String(res.package ?? ""), pip_spec: String(res.pip_spec ?? "") };
          }
          pairingActive = false;
          return;
        } else if (res?.error) {
          toast.error(String(res.error));
          pairingActive = false;
          return;
        }

        onRefresh();
      }
    } catch {
      toast.error("Failed to start WhatsApp channel");
      pairingActive = false;
      return;
    }

    pairingStatus = "";
    await pollQr();
    qrPollingId = setInterval(pollQr, 2000);
  }

  async function pollQr() {
    try {
      const client = connectionStore.getClient();
      const data = await client.getWhatsAppQR();

      if (data.connected) {
        qrConnected = true;
        qrSvg = null;
        stopQrPolling();
        onRefresh();
        toast.success("WhatsApp paired successfully!");
        return;
      }

      if (data.qr) {
        qrSvg = await QRCode.toString(data.qr, {
          type: "svg",
          width: 256,
          margin: 2,
          color: { dark: "#000000", light: "#ffffff" },
        });
      }
    } catch (e) {
      console.warn("[WhatsApp QR] poll error:", e);
    }
  }

  function stopQrPolling() {
    if (qrPollingId) {
      clearInterval(qrPollingId);
      qrPollingId = null;
    }
  }
</script>

<!-- Card Layout -->
<div class="group flex flex-col gap-3 rounded-lg border border-border/60 bg-card/10 p-4 transition-colors hover:bg-card/50">
  <!-- Header: Icon + Name + Badges -->
  <div class="flex items-center gap-2.5">
    <div class={cn("flex h-8 w-8 shrink-0 items-center justify-center rounded-lg", colors.bg)}>
      <ChannelIcon class={cn("h-4 w-4", colors.text)} strokeWidth={1.75} />
    </div>
    <div class="flex min-w-0 flex-1 flex-col">
      <div class="flex items-center gap-1.5">
        <span class="text-sm font-medium text-foreground">{label}</span>
        {#if info.mode}
          <span class="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
            {info.mode}
          </span>
        {/if}
      </div>
      <p class="truncate text-[11px] text-muted-foreground">{description}</p>
    </div>
  </div>

  <!-- Status row -->
  <div class="flex items-center gap-2">
    {#if toggling}
      <CircleDot class="h-3 w-3 animate-pulse-dot text-paw-warning" strokeWidth={2} />
      <span class="text-[10px] text-paw-warning">
        {info.running ? "Stopping..." : "Starting..."}
      </span>
    {:else if info.running}
      <CircleDot class="h-3 w-3 text-paw-success" strokeWidth={2} />
      <span class="text-[10px] text-paw-success">Running</span>
    {:else if info.configured}
      <CircleDot class="h-3 w-3 text-muted-foreground/60" strokeWidth={2} />
      <span class="text-[10px] text-muted-foreground">Stopped</span>
    {:else}
      <CircleDot class="h-3 w-3 text-muted-foreground/40" strokeWidth={2} />
      <span class="text-[10px] text-muted-foreground/60">Not configured</span>
    {/if}

    {#if toggleError || info.error}
      <span class="truncate text-[10px] text-paw-error" title={toggleError ?? info.error}>
        {toggleError ?? info.error}
      </span>
    {/if}
  </div>

  <!-- Actions bar -->
  <div class="flex items-center justify-between border-t border-border/40 pt-2.5">
    <div class="flex items-center gap-2">
      {#if info.configured}
        <div class="flex items-center gap-1.5">
          <span class="text-[10px] text-muted-foreground">Auto</span>
          <Switch
            checked={info.autostart}
            onCheckedChange={handleAutostartToggle}
            class="scale-75"
          />
        </div>
      {/if}
    </div>
    <div class="flex items-center gap-2">
      {#if info.configured}
        <Switch checked={info.running} onCheckedChange={handleToggle} disabled={toggling} />
      {/if}
      <button
        onclick={openDialog}
        class="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        title={info.configured ? `Edit ${label}` : `Configure ${label}`}
      >
        {#if info.configured}
          <Pencil class="h-3.5 w-3.5" strokeWidth={1.75} />
        {:else}
          <Plus class="h-3.5 w-3.5" strokeWidth={1.75} />
        {/if}
      </button>
    </div>
  </div>
</div>

<!-- Config Dialog -->
<Dialog.Root open={configOpen} onOpenChange={handleDialogClose}>
  <Dialog.Content class="max-w-md">
    <Dialog.Header>
      <Dialog.Title class="flex items-center gap-2 text-base">
        <div class={cn("flex h-6 w-6 items-center justify-center rounded-md", colors.bg)}>
          <ChannelIcon class={cn("h-3.5 w-3.5", colors.text)} strokeWidth={1.75} />
        </div>
        {info.configured ? "Edit" : "Configure"} {label}
      </Dialog.Title>
      <Dialog.Description class="text-xs text-muted-foreground">
        {#if pairingActive && !qrConnected}
          Scan the QR code with WhatsApp on your phone.
        {:else if qrConnected}
          Device paired successfully.
        {:else}
          {description}
        {/if}
      </Dialog.Description>
    </Dialog.Header>

    {#if showCloseGuard}
      <!-- WhatsApp close guard -->
      <div class="flex flex-col gap-3 rounded-lg border border-paw-warning/40 bg-paw-warning/5 p-4">
        <div class="flex items-start gap-2">
          <AlertTriangle class="mt-0.5 h-4 w-4 shrink-0 text-paw-warning" />
          <div class="flex flex-col gap-1">
            <p class="text-xs font-medium text-foreground">Pairing in progress</p>
            <p class="text-[11px] text-muted-foreground">
              Closing now will cancel the QR code pairing. Are you sure?
            </p>
          </div>
        </div>
        <div class="flex items-center gap-2">
          <button
            onclick={() => { if (pendingCloseAction) pendingCloseAction(); }}
            class="rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            Close anyway
          </button>
          <button
            onclick={() => { showCloseGuard = false; }}
            class="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-opacity hover:opacity-90"
          >
            Stay
          </button>
        </div>
      </div>
    {:else if pairingActive}
      <!-- QR Pairing View -->
      <div class="flex flex-col items-center gap-4 py-6">
        {#if qrConnected}
          <div class="flex flex-col items-center gap-3">
            <div class="flex h-16 w-16 items-center justify-center rounded-full bg-paw-success/10">
              <CheckCircle2 class="h-8 w-8 text-paw-success" />
            </div>
            <p class="text-sm font-medium text-foreground">Connected!</p>
            <p class="text-xs text-muted-foreground">Your WhatsApp is now linked.</p>
          </div>
        {:else if qrSvg}
          <div class="h-56 w-56 rounded-xl border border-border bg-white p-3 [&>svg]:h-full [&>svg]:w-full">
            {@html qrSvg}
          </div>
          <div class="flex flex-col gap-1.5 text-center">
            <p class="text-xs font-medium text-foreground">Scan with WhatsApp</p>
            <ol class="flex flex-col gap-0.5 text-[11px] text-muted-foreground">
              <li>1. Open WhatsApp on your phone</li>
              <li>2. Go to Settings &rarr; Linked Devices</li>
              <li>3. Tap "Link a Device"</li>
              <li>4. Point your camera at this QR code</li>
            </ol>
          </div>
        {:else}
          <Loader2 class="h-8 w-8 animate-spin text-muted-foreground" />
          <p class="text-xs text-muted-foreground">
            {pairingStatus || "Waiting for QR code..."}
          </p>
        {/if}
      </div>

      <Dialog.Footer>
        {#if qrConnected}
          <button
            onclick={() => (configOpen = false)}
            class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90"
          >
            Done
          </button>
        {:else}
          <button
            onclick={() => { stopQrPolling(); pairingActive = false; }}
            class="rounded-lg border border-border px-4 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            Back
          </button>
        {/if}
      </Dialog.Footer>
    {:else}
      <!-- Config Form View -->
      <div class="flex max-h-[60vh] flex-col gap-4 overflow-y-auto py-4">
        {#if missingDep}
          <div class="flex flex-col gap-3 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3">
            <div class="flex items-start gap-2">
              <Download class="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
              <div class="flex flex-col gap-1">
                <p class="text-xs font-medium text-foreground">
                  Missing dependency
                </p>
                <p class="text-[11px] text-muted-foreground">
                  {label} requires <code class="rounded bg-muted px-1 py-0.5 text-[10px]">{missingDep.package}</code> to run. Install it now?
                </p>
              </div>
            </div>
            <div class="flex items-center gap-2">
              <button
                onclick={handleInstallDep}
                disabled={installingDep}
                class="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
              >
                {#if installingDep}
                  <span class="flex items-center gap-1.5">
                    <Loader2 class="h-3 w-3 animate-spin" />
                    Installing...
                  </span>
                {:else}
                  Install
                {/if}
              </button>
              <button
                onclick={() => (missingDep = null)}
                disabled={installingDep}
                class="rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
              >
                Dismiss
              </button>
            </div>
          </div>
        {/if}

        {#if prefilling}
          <div class="flex items-center gap-2 py-2 text-xs text-muted-foreground">
            <Loader2 class="h-3 w-3 animate-spin" />
            Loading saved config...
          </div>
        {/if}

        {#if isWhatsAppPersonal}
          <div class="flex items-start gap-2 rounded-lg border border-border/60 bg-muted/40 p-3">
            <Info class="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
            <p class="text-xs text-muted-foreground">
              No tokens needed for personal mode. Save to start pairing via QR code.
            </p>
          </div>
        {/if}

        {#each visibleFields as field (field.key)}
          <div class="flex flex-col gap-1.5">
            <label for={`field-${field.key}`} class="text-xs font-medium text-muted-foreground">
              {field.label}{#if field.required}<span class="text-destructive"> *</span>{/if}
            </label>

            {#if field.type === "select" && field.options}
              <Select.Root type="single" value={formValues[field.key] ?? ""} onValueChange={(v) => { formValues[field.key] = v ?? ""; }}>
                <Select.Trigger class="w-full">
                  {field.options.find((o) => o.value === formValues[field.key])?.label ?? "Select..."}
                </Select.Trigger>
                <Select.Content>
                  {#each field.options as opt (opt.value)}
                    <Select.Item value={opt.value} label={opt.label} />
                  {/each}
                </Select.Content>
              </Select.Root>
            {:else if field.type === "textarea"}
              <textarea
                id={`field-${field.key}`}
                value={formValues[field.key] ?? ""}
                oninput={(e) => { formValues[field.key] = e.currentTarget.value; }}
                placeholder={secretSaved[field.key] ? "(saved)" : (field.placeholder ?? "")}
                rows={4}
                class="w-full resize-y rounded-lg border border-border bg-muted/50 px-3 py-2 font-mono text-xs text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
              ></textarea>
            {:else}
              <input
                id={`field-${field.key}`}
                value={formValues[field.key] ?? ""}
                oninput={(e) => { formValues[field.key] = e.currentTarget.value; }}
                type={field.type === "password" ? "password" : "text"}
                placeholder={secretSaved[field.key] ? "(saved)" : (field.placeholder ?? "")}
                class="h-9 w-full rounded-lg border border-border bg-muted/50 px-3 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
              />
            {/if}

            {#if field.helpText}
              <p class="text-[10px] text-muted-foreground">{field.helpText}</p>
            {/if}
          </div>
        {/each}

        <!-- Autostart toggle in dialog -->
        <div class="flex items-center justify-between rounded-lg border border-border/60 bg-muted/30 px-3 py-2.5">
          <div class="flex flex-col">
            <span class="text-xs font-medium text-foreground">Start automatically</span>
            <span class="text-[10px] text-muted-foreground">Launch this channel when the backend starts</span>
          </div>
          <Switch
            checked={formAutostart}
            onCheckedChange={(v) => { formAutostart = v; }}
          />
        </div>
      </div>

      <Dialog.Footer class="flex items-center gap-2">
        {#if info.configured}
          <button
            onclick={handleTestChannel}
            disabled={testing}
            class="mr-auto flex items-center gap-1.5 rounded-lg border border-border px-3 py-2 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-40"
          >
            {#if testing}
              <Loader2 class="h-3 w-3 animate-spin" />
            {:else}
              <FlaskConical class="h-3 w-3" strokeWidth={1.75} />
            {/if}
            Test
          </button>
        {/if}
        <button
          onclick={() => (configOpen = false)}
          class="rounded-lg border border-border px-4 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          Cancel
        </button>
        <button
          onclick={handleSaveConfig}
          disabled={!canSave || savingConfig}
          class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
        >
          {#if savingConfig}
            Saving...
          {:else if isWhatsAppPersonal}
            <span class="flex items-center gap-1.5">
              <QrCode class="h-3.5 w-3.5" />
              Save & Pair
            </span>
          {:else}
            Save
          {/if}
        </button>
      </Dialog.Footer>
    {/if}
  </Dialog.Content>
</Dialog.Root>

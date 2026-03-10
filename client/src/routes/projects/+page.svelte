<script lang="ts">
  import { projectStore } from "$lib/stores";
  import type { ProjectStatus, MCProject, MCProjectProgress } from "$lib/types/pawkit";
  import Badge from "$lib/components/ui/badge/badge.svelte";
  import {
    Plus,
    ArrowLeft,
    Check,
    Loader2,
    Sparkles,
    SkipForward,
    RotateCcw,
    Play,
    Pause,
    XCircle,
    CheckCircle2,
    Circle,
  } from "@lucide/svelte";

  // --- View state ---
  type View = "list" | "create" | "detail";
  let view = $state<View>("list");
  let activeProjectId = $state<string | null>(null);

  // --- List filtering ---
  type FilterTab = "all" | "active" | "completed";
  let filterTab = $state<FilterTab>("all");

  let filteredProjects = $derived(() => {
    const all = projectStore.projects;
    if (filterTab === "active") {
      return all.filter(
        (p) =>
          p.status === "planning" ||
          p.status === "awaiting_approval" ||
          p.status === "approved" ||
          p.status === "executing" ||
          p.status === "paused" ||
          p.status === "draft",
      );
    }
    if (filterTab === "completed") {
      return all.filter(
        (p) => p.status === "completed" || p.status === "failed" || p.status === "cancelled",
      );
    }
    return all;
  });

  // --- Create state ---
  let createStep = $state(1);
  let goalText = $state("");
  let analysisResult = $state<Record<string, unknown> | null>(null);
  let isAnalyzing = $state(false);
  let isStarting = $state(false);

  // --- Detail state ---
  let detailProject = $state<MCProject | null>(null);
  let detailTasks = $state<Record<string, unknown>[]>([]);
  let detailProgress = $state<MCProjectProgress | null>(null);
  let isLoadingDetail = $state(false);

  // --- Status badge colors ---
  const statusColors: Record<string, string> = {
    draft: "bg-gray-500/10 text-gray-500",
    planning: "bg-blue-500/10 text-blue-500",
    awaiting_approval: "bg-yellow-500/10 text-yellow-500",
    approved: "bg-green-500/10 text-green-500",
    executing: "bg-blue-500/10 text-blue-500",
    paused: "bg-orange-500/10 text-orange-500",
    completed: "bg-green-500/10 text-green-500",
    failed: "bg-red-500/10 text-red-500",
    cancelled: "bg-gray-500/10 text-gray-500",
  };

  // --- Planning phases ---
  const planPhases = [
    { key: "goal_analysis", label: "Goal Analysis" },
    { key: "research", label: "Research" },
    { key: "prd", label: "PRD Generation" },
    { key: "tasks", label: "Task Breakdown" },
    { key: "team", label: "Team Assembly" },
  ];

  // --- Relative time helper ---
  function relativeTime(dateStr: string): string {
    const now = Date.now();
    const then = new Date(dateStr).getTime();
    const diffMs = now - then;
    const diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 1) return "just now";
    if (diffMin < 60) return `${diffMin}m ago`;
    const diffHr = Math.floor(diffMin / 60);
    if (diffHr < 24) return `${diffHr}h ago`;
    const diffDay = Math.floor(diffHr / 24);
    if (diffDay < 30) return `${diffDay}d ago`;
    return new Date(dateStr).toLocaleDateString();
  }

  // --- Format status label ---
  function formatStatus(status: string): string {
    return status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }

  // --- Load projects on mount ---
  $effect(() => {
    projectStore.loadProjects();
  });

  // --- Create flow: analyze goal ---
  async function handleAnalyze() {
    if (!goalText.trim()) return;
    isAnalyzing = true;
    analysisResult = null;
    try {
      const result = await projectStore.parseGoal(goalText);
      analysisResult = result;
    } finally {
      isAnalyzing = false;
    }
  }

  // --- Create flow: start project ---
  async function handleStartPlanning() {
    isStarting = true;
    createStep = 2;
    try {
      const projectId = await projectStore.startProject(
        goalText,
        analysisResult?.description as string | undefined,
      );
      if (projectId) {
        activeProjectId = projectId;
      }
    } finally {
      isStarting = false;
    }
  }

  // --- Navigate to detail ---
  async function openDetail(projectId: string) {
    activeProjectId = projectId;
    view = "detail";
    await loadDetail(projectId);
  }

  async function loadDetail(projectId: string) {
    isLoadingDetail = true;
    try {
      const result = await projectStore.getProjectDetail(projectId);
      if (result) {
        detailProject = result.project;
        detailTasks = result.tasks;
        detailProgress = result.progress;
      }
    } finally {
      isLoadingDetail = false;
    }
  }

  // --- Lifecycle actions ---
  async function handleApprove() {
    if (!activeProjectId) return;
    await projectStore.approveProject(activeProjectId);
    await loadDetail(activeProjectId);
  }

  async function handlePause() {
    if (!activeProjectId) return;
    await projectStore.pauseProject(activeProjectId);
    await loadDetail(activeProjectId);
  }

  async function handleResume() {
    if (!activeProjectId) return;
    await projectStore.resumeProject(activeProjectId);
    await loadDetail(activeProjectId);
  }

  async function handleCancel() {
    if (!activeProjectId) return;
    await projectStore.cancelProject(activeProjectId);
    await loadDetail(activeProjectId);
  }

  async function handleSkipTask(taskId: string) {
    if (!activeProjectId) return;
    await projectStore.skipTask(activeProjectId, taskId);
    await loadDetail(activeProjectId);
  }

  async function handleRetryTask(taskId: string) {
    if (!activeProjectId) return;
    await projectStore.retryTask(activeProjectId, taskId);
    await loadDetail(activeProjectId);
  }

  // --- Reset create flow ---
  function resetCreate() {
    createStep = 1;
    goalText = "";
    analysisResult = null;
    isAnalyzing = false;
    isStarting = false;
  }

  function goToList() {
    view = "list";
    resetCreate();
    detailProject = null;
    detailTasks = [];
    detailProgress = null;
    activeProjectId = null;
  }

  // --- Phase index helper ---
  function phaseIndex(phase: string | null): number {
    if (!phase) return -1;
    return planPhases.findIndex((p) => p.key === phase);
  }
</script>

<div class="flex h-full flex-col overflow-auto">
  <!-- ======================== LIST VIEW ======================== -->
  {#if view === "list"}
    <!-- Header -->
    <div class="flex items-center justify-between border-b border-border/40 px-4 py-2">
      <div class="flex gap-1 rounded-lg border border-border/40 bg-muted/20 p-0.5">
        {#each [["all", "All"], ["active", "Active"], ["completed", "Completed"]] as [key, label]}
          <button
            onclick={() => (filterTab = key as FilterTab)}
            class={[
              "rounded-md px-3 py-1.5 text-xs transition-colors",
              filterTab === key
                ? "bg-background font-medium text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            ].join(" ")}
          >
            {label}
          </button>
        {/each}
      </div>

      <button
        onclick={() => {
          resetCreate();
          view = "create";
        }}
        class="flex items-center gap-1.5 rounded-lg bg-foreground px-3 py-1.5 text-xs font-medium text-background transition-opacity hover:opacity-90"
      >
        <Plus class="h-3.5 w-3.5" />
        New Project
      </button>
    </div>

    <!-- Content -->
    {#if projectStore.isLoading}
      <div class="flex flex-1 items-center justify-center">
        <div
          class="h-6 w-6 animate-spin rounded-full border-2 border-foreground/20 border-t-foreground"
        ></div>
      </div>
    {:else if filteredProjects().length === 0}
      <div class="flex h-full flex-col items-center justify-center gap-4 p-8 text-center">
        <div class="rounded-2xl border border-border/50 bg-muted/30 p-4">
          <Sparkles class="h-10 w-10 text-muted-foreground" strokeWidth={1.5} />
        </div>
        <div>
          <h2 class="text-lg font-semibold text-foreground">No projects yet</h2>
          <p class="mt-1 max-w-sm text-sm text-muted-foreground">
            Create your first project by describing a goal in natural language.
          </p>
        </div>
        <button
          onclick={() => {
            resetCreate();
            view = "create";
          }}
          class="rounded-lg bg-foreground px-5 py-2.5 text-sm font-medium text-background transition-opacity hover:opacity-90"
        >
          Create Project
        </button>
      </div>
    {:else}
      <div class="grid grid-cols-1 gap-3 p-4 sm:grid-cols-2 lg:grid-cols-3">
        {#each filteredProjects() as project (project.id)}
          <button
            onclick={() => openDetail(project.id)}
            class="flex flex-col gap-2 rounded-lg border border-border/50 bg-muted/10 p-4 text-left transition-colors hover:border-border hover:bg-muted/20"
          >
            <div class="flex items-start justify-between gap-2">
              <h3 class="text-sm font-medium text-foreground line-clamp-2">{project.title}</h3>
              <span
                class={[
                  "shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium",
                  statusColors[project.status] ?? "bg-gray-500/10 text-gray-500",
                ].join(" ")}
              >
                {formatStatus(project.status)}
              </span>
            </div>
            {#if project.description}
              <p class="text-xs text-muted-foreground line-clamp-2">{project.description}</p>
            {/if}
            <div class="mt-auto flex items-center justify-between pt-1">
              <span class="text-[10px] text-muted-foreground">
                {relativeTime(project.created_at)}
              </span>
              {#if project.task_ids && project.task_ids.length > 0}
                <span class="text-[10px] text-muted-foreground">
                  {project.task_ids.length} task{project.task_ids.length !== 1 ? "s" : ""}
                </span>
              {/if}
            </div>
          </button>
        {/each}
      </div>
    {/if}

  <!-- ======================== CREATE VIEW ======================== -->
  {:else if view === "create"}
    <!-- Header -->
    <div class="flex items-center gap-2 border-b border-border/40 px-4 py-2">
      <button
        onclick={goToList}
        class="rounded-md p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      >
        <ArrowLeft class="h-4 w-4" />
      </button>
      <h2 class="text-sm font-medium text-foreground">New Project</h2>
      <div class="ml-auto flex items-center gap-1 text-xs text-muted-foreground">
        <span class={createStep >= 1 ? "text-foreground" : ""}>Goal</span>
        <span>-</span>
        <span class={createStep >= 2 ? "text-foreground" : ""}>Planning</span>
      </div>
    </div>

    <div class="mx-auto w-full max-w-2xl p-6">
      <!-- Step 1: Goal Input -->
      {#if createStep === 1}
        <div class="flex flex-col gap-4">
          <div>
            <h3 class="text-base font-semibold text-foreground">Describe your goal</h3>
            <p class="mt-1 text-sm text-muted-foreground">
              What would you like to accomplish? Describe it in natural language and the AI will
              analyze it into a structured project plan.
            </p>
          </div>

          <textarea
            bind:value={goalText}
            placeholder="e.g., Build a REST API for a todo app with user auth, CRUD operations, and deploy to AWS..."
            rows={5}
            class="w-full resize-none rounded-lg border border-border/50 bg-muted/10 px-3 py-2.5 text-sm text-foreground placeholder:text-muted-foreground focus:border-foreground/30 focus:outline-none"
          ></textarea>

          <button
            onclick={handleAnalyze}
            disabled={!goalText.trim() || isAnalyzing}
            class="flex items-center justify-center gap-2 self-start rounded-lg bg-foreground px-4 py-2 text-sm font-medium text-background transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            {#if isAnalyzing}
              <Loader2 class="h-3.5 w-3.5 animate-spin" />
              Analyzing...
            {:else}
              <Sparkles class="h-3.5 w-3.5" />
              Analyze
            {/if}
          </button>

          <!-- Analysis Result -->
          {#if analysisResult}
            <div class="rounded-lg border border-border/50 bg-muted/10 p-4">
              <h4 class="text-sm font-medium text-foreground">Analysis</h4>
              <div class="mt-3 grid grid-cols-2 gap-3">
                {#if analysisResult.domain}
                  <div>
                    <span class="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                      Domain
                    </span>
                    <p class="mt-0.5 text-sm text-foreground">{analysisResult.domain}</p>
                  </div>
                {/if}
                {#if analysisResult.complexity}
                  <div>
                    <span class="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                      Complexity
                    </span>
                    <div class="mt-0.5">
                      <Badge variant="secondary" class="text-[10px]">
                        {analysisResult.complexity}
                      </Badge>
                    </div>
                  </div>
                {/if}
                {#if analysisResult.ai_capabilities}
                  <div>
                    <span class="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                      AI Capabilities
                    </span>
                    <p class="mt-0.5 text-sm text-foreground">
                      {Array.isArray(analysisResult.ai_capabilities) ? (analysisResult.ai_capabilities as string[]).join(", ") : analysisResult.ai_capabilities}
                    </p>
                  </div>
                {/if}
                {#if analysisResult.human_requirements}
                  <div>
                    <span class="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                      Human Requirements
                    </span>
                    <p class="mt-0.5 text-sm text-foreground">
                      {Array.isArray(analysisResult.human_requirements) ? (analysisResult.human_requirements as string[]).join(", ") : analysisResult.human_requirements}
                    </p>
                  </div>
                {/if}
              </div>

              {#if analysisResult.clarifications_needed && Array.isArray(analysisResult.clarifications_needed) && (analysisResult.clarifications_needed as unknown[]).length > 0}
                <div class="mt-3">
                  <span class="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                    Clarification Questions
                  </span>
                  <ul class="mt-1 space-y-1">
                    {#each analysisResult.clarifications_needed as q}
                      <li class="text-sm text-muted-foreground">- {q}</li>
                    {/each}
                  </ul>
                </div>
              {/if}

              <button
                onclick={handleStartPlanning}
                disabled={isStarting}
                class="mt-4 flex items-center gap-2 rounded-lg bg-foreground px-4 py-2 text-sm font-medium text-background transition-opacity hover:opacity-90 disabled:opacity-50"
              >
                {#if isStarting}
                  <Loader2 class="h-3.5 w-3.5 animate-spin" />
                  Starting...
                {:else}
                  <Play class="h-3.5 w-3.5" />
                  Start Planning
                {/if}
              </button>
            </div>
          {/if}
        </div>

      <!-- Step 2: Planning Progress -->
      {:else if createStep === 2}
        <div class="flex flex-col gap-4">
          <div>
            <h3 class="text-base font-semibold text-foreground">Planning in progress</h3>
            <p class="mt-1 text-sm text-muted-foreground">
              The AI is breaking down your goal into a structured project plan.
            </p>
          </div>

          <!-- Phase indicators -->
          <div class="flex flex-col gap-2">
            {#each planPhases as phase, i}
              {@const currentIdx = phaseIndex(projectStore.planningPhase)}
              {@const isComplete = currentIdx > i}
              {@const isCurrent = currentIdx === i}
              <div
                class={[
                  "flex items-center gap-3 rounded-lg border px-4 py-3 transition-colors",
                  isCurrent
                    ? "border-foreground/20 bg-foreground/5"
                    : isComplete
                      ? "border-border/30 bg-muted/10"
                      : "border-border/20 bg-transparent",
                ].join(" ")}
              >
                {#if isComplete}
                  <CheckCircle2 class="h-4 w-4 shrink-0 text-green-500" />
                {:else if isCurrent}
                  <Loader2 class="h-4 w-4 shrink-0 animate-spin text-foreground" />
                {:else}
                  <Circle class="h-4 w-4 shrink-0 text-muted-foreground/40" />
                {/if}
                <span
                  class={[
                    "text-sm",
                    isCurrent
                      ? "font-medium text-foreground"
                      : isComplete
                        ? "text-muted-foreground"
                        : "text-muted-foreground/60",
                  ].join(" ")}
                >
                  {phase.label}
                </span>
              </div>
            {/each}
          </div>

          <!-- Current message -->
          {#if projectStore.planningMessage}
            <div class="rounded-lg border border-border/30 bg-muted/10 px-4 py-3">
              <p class="text-xs text-muted-foreground">{projectStore.planningMessage}</p>
            </div>
          {/if}

          <!-- Error -->
          {#if projectStore.planningError}
            <div class="rounded-lg border border-red-500/30 bg-red-500/5 px-4 py-3">
              <p class="text-xs text-red-500">{projectStore.planningError}</p>
            </div>
          {/if}

          <!-- Done -->
          {#if projectStore.planningDone}
            <button
              onclick={() => {
                if (activeProjectId) {
                  view = "detail";
                  loadDetail(activeProjectId);
                } else if (projectStore.planningProjectId) {
                  activeProjectId = projectStore.planningProjectId;
                  view = "detail";
                  loadDetail(projectStore.planningProjectId);
                }
              }}
              class="flex items-center gap-2 self-start rounded-lg bg-foreground px-4 py-2 text-sm font-medium text-background transition-opacity hover:opacity-90"
            >
              <Check class="h-3.5 w-3.5" />
              View Project
            </button>
          {/if}
        </div>
      {/if}
    </div>

  <!-- ======================== DETAIL VIEW ======================== -->
  {:else if view === "detail"}
    {#if isLoadingDetail}
      <div class="flex flex-1 items-center justify-center">
        <div
          class="h-6 w-6 animate-spin rounded-full border-2 border-foreground/20 border-t-foreground"
        ></div>
      </div>
    {:else if detailProject}
      <!-- Header -->
      <div
        class="flex flex-wrap items-center gap-3 border-b border-border/40 px-4 py-2"
      >
        <button
          onclick={goToList}
          class="rounded-md p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          <ArrowLeft class="h-4 w-4" />
        </button>

        <h2 class="text-sm font-medium text-foreground">{detailProject.title}</h2>

        <span
          class={[
            "rounded-full px-2 py-0.5 text-[10px] font-medium",
            statusColors[detailProject.status] ?? "bg-gray-500/10 text-gray-500",
          ].join(" ")}
        >
          {formatStatus(detailProject.status)}
        </span>

        <!-- Lifecycle buttons -->
        <div class="ml-auto flex items-center gap-1.5">
          {#if detailProject.status === "awaiting_approval"}
            <button
              onclick={handleApprove}
              class="flex items-center gap-1 rounded-md bg-green-600 px-2.5 py-1 text-[11px] font-medium text-white transition-opacity hover:opacity-90"
            >
              <Check class="h-3 w-3" />
              Approve
            </button>
            <button
              onclick={handleCancel}
              class="flex items-center gap-1 rounded-md bg-red-600 px-2.5 py-1 text-[11px] font-medium text-white transition-opacity hover:opacity-90"
            >
              <XCircle class="h-3 w-3" />
              Cancel
            </button>
          {:else if detailProject.status === "executing"}
            <button
              onclick={handlePause}
              class="flex items-center gap-1 rounded-md bg-yellow-600 px-2.5 py-1 text-[11px] font-medium text-white transition-opacity hover:opacity-90"
            >
              <Pause class="h-3 w-3" />
              Pause
            </button>
            <button
              onclick={handleCancel}
              class="flex items-center gap-1 rounded-md bg-red-600 px-2.5 py-1 text-[11px] font-medium text-white transition-opacity hover:opacity-90"
            >
              <XCircle class="h-3 w-3" />
              Cancel
            </button>
          {:else if detailProject.status === "paused"}
            <button
              onclick={handleResume}
              class="flex items-center gap-1 rounded-md bg-green-600 px-2.5 py-1 text-[11px] font-medium text-white transition-opacity hover:opacity-90"
            >
              <Play class="h-3 w-3" />
              Resume
            </button>
            <button
              onclick={handleCancel}
              class="flex items-center gap-1 rounded-md bg-red-600 px-2.5 py-1 text-[11px] font-medium text-white transition-opacity hover:opacity-90"
            >
              <XCircle class="h-3 w-3" />
              Cancel
            </button>
          {:else if detailProject.status === "completed" || detailProject.status === "failed" || detailProject.status === "cancelled"}
            <span class="text-[11px] text-muted-foreground">
              {formatStatus(detailProject.status)}
            </span>
          {/if}
        </div>
      </div>

      <div class="flex flex-col gap-4 overflow-auto p-4">
        <!-- Progress bar -->
        {#if detailProgress}
          <div class="flex flex-col gap-1.5">
            <div class="flex items-center justify-between text-xs text-muted-foreground">
              <span>{detailProgress.completed}/{detailProgress.total} tasks complete</span>
              <span>{detailProgress.percent}%</span>
            </div>
            <div class="flex h-2 w-full overflow-hidden rounded-full bg-muted/30">
              {#if detailProgress.total > 0}
                {#if detailProgress.completed > 0}
                  <div
                    class="bg-green-500 transition-all"
                    style="width: {(detailProgress.completed / detailProgress.total) * 100}%"
                  ></div>
                {/if}
                {#if detailProgress.in_progress > 0}
                  <div
                    class="bg-blue-500 transition-all"
                    style="width: {(detailProgress.in_progress / detailProgress.total) * 100}%"
                  ></div>
                {/if}
                {#if detailProgress.blocked > 0}
                  <div
                    class="bg-orange-500 transition-all"
                    style="width: {(detailProgress.blocked / detailProgress.total) * 100}%"
                  ></div>
                {/if}
                {@const pending =
                  detailProgress.total -
                  detailProgress.completed -
                  detailProgress.in_progress -
                  detailProgress.blocked -
                  (detailProgress.skipped ?? 0)}
                {#if pending > 0}
                  <div
                    class="bg-gray-400/30 transition-all"
                    style="width: {(pending / detailProgress.total) * 100}%"
                  ></div>
                {/if}
              {/if}
            </div>
            <div class="flex gap-3 text-[10px] text-muted-foreground">
              <span class="flex items-center gap-1">
                <span class="inline-block h-2 w-2 rounded-full bg-green-500"></span> Completed
              </span>
              <span class="flex items-center gap-1">
                <span class="inline-block h-2 w-2 rounded-full bg-blue-500"></span> In Progress
              </span>
              <span class="flex items-center gap-1">
                <span class="inline-block h-2 w-2 rounded-full bg-orange-500"></span> Blocked
              </span>
              <span class="flex items-center gap-1">
                <span class="inline-block h-2 w-2 rounded-full bg-gray-400/30"></span> Pending
              </span>
            </div>
          </div>
        {/if}

        <!-- Task table -->
        {#if detailTasks.length > 0}
          <div class="overflow-hidden rounded-lg border border-border/40">
            <table class="w-full text-sm">
              <thead>
                <tr class="border-b border-border/30 bg-muted/20">
                  <th class="px-3 py-2 text-left text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                    Title
                  </th>
                  <th class="px-3 py-2 text-left text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                    Type
                  </th>
                  <th class="px-3 py-2 text-left text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                    Priority
                  </th>
                  <th class="px-3 py-2 text-left text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                    Status
                  </th>
                  <th class="px-3 py-2 text-right text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody>
                {#each detailTasks as task (task.id)}
                  {@const taskStatus = (task.status as string) ?? ""}
                  {@const taskType = (task.task_type as string) ?? "agent"}
                  {@const taskPriority = (task.priority as string) ?? "medium"}
                  <tr class="border-b border-border/20 last:border-0">
                    <td class="px-3 py-2 text-foreground">
                      {task.title ?? "Untitled"}
                    </td>
                    <td class="px-3 py-2">
                      <Badge variant="outline" class="text-[10px]">
                        {taskType}
                      </Badge>
                    </td>
                    <td class="px-3 py-2">
                      <span
                        class={[
                          "rounded px-1.5 py-0.5 text-[10px] font-medium",
                          taskPriority === "urgent"
                            ? "bg-red-500/10 text-red-500"
                            : taskPriority === "high"
                              ? "bg-orange-500/10 text-orange-500"
                              : taskPriority === "medium"
                                ? "bg-blue-500/10 text-blue-500"
                                : "bg-gray-500/10 text-gray-500",
                        ].join(" ")}
                      >
                        {taskPriority}
                      </span>
                    </td>
                    <td class="px-3 py-2">
                      <span
                        class={[
                          "rounded-full px-2 py-0.5 text-[10px] font-medium",
                          statusColors[taskStatus] ?? "bg-gray-500/10 text-gray-500",
                        ].join(" ")}
                      >
                        {formatStatus(taskStatus)}
                      </span>
                    </td>
                    <td class="px-3 py-2 text-right">
                      <div class="flex items-center justify-end gap-1">
                        {#if taskStatus === "blocked" || taskStatus === "inbox" || taskStatus === "assigned"}
                          <button
                            onclick={() => handleSkipTask(task.id as string)}
                            class="rounded p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                            title="Skip task"
                          >
                            <SkipForward class="h-3 w-3" />
                          </button>
                        {/if}
                        {#if taskStatus === "blocked" || (task.error_message as string)}
                          <button
                            onclick={() => handleRetryTask(task.id as string)}
                            class="rounded p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                            title="Retry task"
                          >
                            <RotateCcw class="h-3 w-3" />
                          </button>
                        {/if}
                      </div>
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {:else}
          <div class="py-8 text-center text-sm text-muted-foreground">No tasks in this project.</div>
        {/if}
      </div>
    {:else}
      <div class="flex h-full flex-col items-center justify-center gap-2 p-8 text-center">
        <p class="text-sm text-muted-foreground">Project not found or failed to load.</p>
        <button
          onclick={goToList}
          class="text-sm text-foreground underline underline-offset-2"
        >
          Back to projects
        </button>
      </div>
    {/if}
  {/if}
</div>

import type { ChatMessage, FileContext, MediaAttachment } from "$lib/api";
import { friendlyErrorMessage } from "$lib/api/client";
import { toast } from "svelte-sonner";
import { connectionStore } from "./connection.svelte";
import { sessionStore } from "./sessions.svelte";
import { explorerStore } from "./explorer.svelte";
import { activityStore } from "./activity.svelte";

function humanizeToolName(tool: string): string {
  const lower = tool.toLowerCase();
  if (lower.includes("bash") || lower.includes("command") || lower.includes("shell"))
    return "Running command...";
  if (lower.includes("read") || lower.includes("cat")) return "Reading file...";
  if (lower.includes("write") || lower.includes("edit")) return "Editing file...";
  if (lower.includes("grep") || lower.includes("search") || lower.includes("glob"))
    return "Searching code...";
  if (lower.includes("browser") || lower.includes("navigate")) return "Browsing web...";
  if (lower.includes("list") || lower.includes("ls")) return "Listing files...";
  return `Running ${tool}...`;
}

class ChatStore {
  messages = $state<ChatMessage[]>([]);
  isStreaming = $state(false);
  streamingContent = $state("");
  streamingStatus = $state<string | null>(null);
  error = $state<string | null>(null);

  /** Pending AskUserQuestion state */
  pendingAskQuestion = $state<string | null>(null);
  pendingAskOptions = $state<string[]>([]);

  isEmpty = $derived(this.messages.length === 0);
  lastMessage = $derived(this.messages.at(-1) ?? null);

  private abortController: AbortController | null = null;

  /** Collect current file explorer context for the LLM. */
  private getFileContext(): FileContext | undefined {
    const dir = explorerStore.currentPath;
    const file = explorerStore.openFile;
    const selected = explorerStore.selectedFiles;

    // Nothing to send if explorer is at home with nothing open
    if (!dir && !file && selected.size === 0) return undefined;

    const ctx: FileContext = {};
    if (dir) ctx.current_dir = dir;
    if (file) {
      ctx.open_file = file.path;
      ctx.open_file_name = file.name;
      ctx.open_file_extension = file.extension;
      ctx.open_file_size = file.size;
    }
    if (selected.size > 0) {
      ctx.selected_files = [...selected];
    }
    const source = explorerStore.currentSource;
    if (source !== "local") ctx.source = source;
    return ctx;
  }

  sendMessage(content: string, media?: MediaAttachment[]): void {
    // Push user message to the list
    const userMsg: ChatMessage = {
      role: "user",
      content,
      timestamp: new Date().toISOString(),
      media,
    };
    this.messages.push(userMsg);

    // Clear any previous error
    this.error = null;

    // Send via REST SSE stream with file context
    this.streamChat(content, media);
  }

  regenerateLastResponse(): void {
    if (this.isStreaming) return;

    // Find the last user message
    let lastUserIdx = -1;
    for (let i = this.messages.length - 1; i >= 0; i--) {
      if (this.messages[i].role === "user") {
        lastUserIdx = i;
        break;
      }
    }
    if (lastUserIdx === -1) return;

    const userContent = this.messages[lastUserIdx].content;
    const userMedia = this.messages[lastUserIdx].media;

    // Remove everything after (and including) the assistant response that followed
    this.messages = this.messages.slice(0, lastUserIdx + 1);
    this.error = null;

    // Re-send via REST SSE
    this.streamChat(userContent, userMedia);
  }

  /** Answer an AskUserQuestion prompt — sends the chosen option as a chat message. */
  answerAskUser(answer: string): void {
    this.pendingAskQuestion = null;
    this.pendingAskOptions = [];
    this.sendMessage(answer);
  }

  stopGeneration(): void {
    // Abort the in-flight fetch
    this.abortController?.abort();
    this.abortController = null;

    // Tell backend to stop generating
    const sessionId = sessionStore.activeSessionId;
    if (sessionId) {
      try {
        const client = connectionStore.getClient();
        client.stopChat(sessionId).catch(() => {
          // ignore — best-effort stop
        });
      } catch {
        // ignore if not connected
      }
    }

    this.finalizeStream();
  }

  loadHistory(messages: ChatMessage[]): void {
    // Abort any in-flight stream so its callbacks don't clobber the loaded history
    this.abortController?.abort();
    this.abortController = null;

    this.messages = messages;
    this.isStreaming = false;
    this.streamingContent = "";
    this.streamingStatus = null;
    this.error = null;

    activityStore.isAgentWorking = false;
    activityStore.sseActive = false;
  }

  clearMessages(): void {
    // Abort any in-flight stream so its callbacks don't clobber the new state
    this.abortController?.abort();
    this.abortController = null;

    this.messages = [];
    this.isStreaming = false;
    this.streamingContent = "";
    this.streamingStatus = null;
    this.error = null;

    // Reset activity store in case SSE was active
    activityStore.isAgentWorking = false;
    activityStore.sseActive = false;
  }

  private async streamChat(content: string, media?: MediaAttachment[]): Promise<void> {
    this.isStreaming = true;
    this.streamingContent = "";
    this.streamingStatus = "Thinking...";

    // Signal activity store that SSE is driving events
    activityStore.isAgentWorking = true;
    activityStore.sseActive = true;
    activityStore.tokenUsage = null;

    this.abortController?.abort();
    this.abortController = new AbortController();

    try {
      const client = connectionStore.getClient();
      const sessionId = sessionStore.activeSessionId ?? undefined;
      const fileContext = this.getFileContext();

      await client.chatStream(
        content,
        {
          onChunk: (data) => {
            // Clear status on first content chunk
            if (this.streamingStatus) this.streamingStatus = null;
            this.streamingContent += data.content;
          },
          onThinking: (data) => {
            this.streamingStatus = "Thinking...";
            activityStore.pushSSEEvent("thinking", { content: data.content });
          },
          onToolStart: (data) => {
            this.streamingStatus = humanizeToolName(data.tool);
            activityStore.pushSSEEvent("tool_start", {
              tool: data.tool,
              input: data.input,
            });

            // Auto-open file viewer for PDFs and images the agent reads
            if (data.tool === "Read" && data.input?.file_path) {
              const fp = String(data.input.file_path);
              const ext = (fp.split(".").pop() || "").toLowerCase();
              const viewable = new Set([
                "pdf", "jpg", "jpeg", "png", "gif", "svg", "webp", "bmp",
              ]);
              if (viewable.has(ext)) {
                explorerStore.openFileByPath(fp);
              }
            }
          },
          onToolResult: (data) => {
            this.streamingStatus = "Thinking...";
            activityStore.pushSSEEvent("tool_result", {
              tool: data.tool,
              output: data.output,
            });
          },
          onAskUser: (data) => {
            // Just store the options — they'll be attached to the final
            // message in finalizeStream so nothing gets split.
            const optLabels = (data.options || []).map((o: unknown) =>
              typeof o === "string" ? o : (o as Record<string, string>).label || (o as Record<string, string>).text || "Option",
            );
            this.pendingAskOptions = optLabels;
            this.pendingAskQuestion = data.question || "";
          },
          onStreamEnd: (data) => {
            // Capture token usage before finalizeStream resets sseActive
            if (data.usage) {
              activityStore.tokenUsage = {
                input: data.usage.input_tokens,
                output: data.usage.output_tokens,
              };
            }
            this.finalizeStream();
            // Update active session ID if the backend returned one (e.g. new session)
            if (data.session_id && data.session_id !== sessionStore.activeSessionId) {
              sessionStore.setActiveSession(data.session_id);
            }
          },
          onError: (data) => {
            this.error = data.detail || "An error occurred";
            toast.error(this.error);
            this.finalizeStream();
          },
        },
        media,
        sessionId,
        this.abortController.signal,
        fileContext,
      );

      // If stream ended without an explicit stream_end event, finalize
      if (this.isStreaming) {
        this.finalizeStream();
      }
    } catch (err: unknown) {
      // AbortError is expected when user stops generation
      if (err instanceof DOMException && err.name === "AbortError") {
        // Clean up activity store flags (finalizeStream is NOT called here
        // because stopGeneration() already calls it; this handles the case
        // where a new streamChat aborted the previous one).
        activityStore.sseActive = false;
        activityStore.isAgentWorking = false;
        return;
      }
      const message = friendlyErrorMessage(err);
      this.error = message;
      toast.error(message);
      if (this.isStreaming) {
        this.finalizeStream();
      }
    } finally {
      this.abortController = null;
    }
  }

  private finalizeStream(): void {
    if (this.streamingContent) {
      const msg: ChatMessage = {
        role: "assistant",
        content: this.streamingContent,
        timestamp: new Date().toISOString(),
      };
      // Attach pending AskUserQuestion options to this message
      if (this.pendingAskOptions.length > 0) {
        msg.metadata = { askUser: true, options: [...this.pendingAskOptions] };
      }
      this.messages.push(msg);
    }
    this.isStreaming = false;
    this.streamingContent = "";
    this.streamingStatus = null;
    this.pendingAskQuestion = null;
    this.pendingAskOptions = [];

    // Reset activity store SSE state
    activityStore.isAgentWorking = false;
    activityStore.sseActive = false;
  }
}

export const chatStore = new ChatStore();

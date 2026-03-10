import { localFs } from "$lib/filesystem";
import { isTextPreviewable, isPdfFile } from "./file-icon-colors";

// --- Concurrency limiter ---
// Prevents flooding Tauri IPC when scrolling through large directories

const MAX_CONCURRENT = 3;
let activeCount = 0;
const queue: Array<() => void> = [];

function acquire(): Promise<void> {
  if (activeCount < MAX_CONCURRENT) {
    activeCount++;
    return Promise.resolve();
  }
  return new Promise<void>((resolve) => {
    queue.push(resolve);
  });
}

function release(): void {
  activeCount--;
  const next = queue.shift();
  if (next) {
    activeCount++;
    next();
  }
}

// --- Text preview cache (LRU) ---

const MAX_TEXT_CACHE = 300;
const MAX_HEAD_BYTES = 512; // Tiny preview at 7px font — 512 bytes is plenty
const MAX_FILE_SIZE_FOR_PREVIEW = 512_000; // 500 KB guard

const textCache = new Map<string, string>();
const textInflight = new Map<string, Promise<string | null>>();

function evictOldestText() {
  if (textCache.size >= MAX_TEXT_CACHE) {
    const oldest = textCache.keys().next().value;
    if (oldest !== undefined) textCache.delete(oldest);
  }
}

/** Get text preview content for a file. Returns null if not previewable. */
export async function getTextPreview(
  path: string,
  extension: string,
  size: number,
): Promise<string | null> {
  if (!isTextPreviewable(extension)) return null;
  if (size > MAX_FILE_SIZE_FOR_PREVIEW) return null;

  // Check cache (synchronous — no IPC)
  const cached = textCache.get(path);
  if (cached !== undefined) {
    textCache.delete(path);
    textCache.set(path, cached);
    return cached;
  }

  // Deduplicate inflight
  const pending = textInflight.get(path);
  if (pending) return pending;

  const promise = (async (): Promise<string | null> => {
    await acquire();
    try {
      const text = await localFs.readFileHead(path, MAX_HEAD_BYTES);
      if (!text || text.length === 0) return null;

      // Quick binary check — scan first 256 chars for control bytes
      let controlCount = 0;
      const checkLen = Math.min(text.length, 256);
      for (let i = 0; i < checkLen; i++) {
        const code = text.charCodeAt(i);
        if (code < 9 || (code > 13 && code < 32)) {
          controlCount++;
          if (controlCount > 5) return null; // Likely binary
        }
      }

      evictOldestText();
      textCache.set(path, text);
      return text;
    } catch {
      return null;
    } finally {
      release();
      textInflight.delete(path);
    }
  })();

  textInflight.set(path, promise);
  return promise;
}

/** Invalidate a specific text preview from cache */
export function invalidateTextPreview(path: string): void {
  textCache.delete(path);
}

/** Clear all text preview caches */
export function clearTextPreviewCache(): void {
  textCache.clear();
}

// --- PDF thumbnail cache ---

const pdfThumbnailCache = new Map<string, string>();
const pdfInflight = new Map<string, Promise<string | null>>();
const MAX_PDF_CACHE = 100;

function evictOldestPdf() {
  if (pdfThumbnailCache.size >= MAX_PDF_CACHE) {
    const oldest = pdfThumbnailCache.keys().next().value;
    if (oldest !== undefined) pdfThumbnailCache.delete(oldest);
  }
}

// Use the legacy build — the modern build requires Uint8Array.toHex() which
// is not yet supported in Tauri's WebView2 runtime.
let pdfjsReady: Promise<typeof import("pdfjs-dist/legacy/build/pdf.mjs")> | null = null;

/** One-time pdfjs-dist setup (lazy). */
function ensurePdfjs(): Promise<typeof import("pdfjs-dist/legacy/build/pdf.mjs")> {
  if (pdfjsReady) return pdfjsReady;

  pdfjsReady = (async () => {
    const pdfjsLib = await import("pdfjs-dist/legacy/build/pdf.mjs");
    const workerUrl = await import("pdfjs-dist/legacy/build/pdf.worker.min.mjs?url");
    pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl.default;
    return pdfjsLib;
  })();

  return pdfjsReady;
}

/** Render the first page of a PDF as a thumbnail data URL. Lazy-loads pdfjs-dist. */
export async function getPdfThumbnail(path: string): Promise<string | null> {
  // Check cache
  const cached = pdfThumbnailCache.get(path);
  if (cached) {
    pdfThumbnailCache.delete(path);
    pdfThumbnailCache.set(path, cached);
    return cached;
  }

  const pending = pdfInflight.get(path);
  if (pending) return pending;

  const promise = (async (): Promise<string | null> => {
    try {
      const pdfjsLib = await ensurePdfjs();

      // Use Tauri's convertFileSrc to get an asset URL the webview can fetch
      // This avoids reading the entire PDF into memory as base64
      const { convertFileSrc } = await import("@tauri-apps/api/core");
      const fileUrl = convertFileSrc(path);

      const loadingTask = pdfjsLib.getDocument({
        url: fileUrl,
        // Disable features unnecessary for thumbnails
        disableAutoFetch: true,
        disableStream: true,
      });

      const pdf = await loadingTask.promise;
      const page = await pdf.getPage(1);

      // Render at a small size for thumbnail (160px wide to match card width)
      const targetWidth = 160;
      const viewport = page.getViewport({ scale: 1 });
      const scale = targetWidth / viewport.width;
      const scaledViewport = page.getViewport({ scale });

      const canvasEl = document.createElement("canvas");
      canvasEl.width = Math.floor(scaledViewport.width);
      canvasEl.height = Math.floor(scaledViewport.height);

      await page.render({
        canvas: canvasEl,
        viewport: scaledViewport,
      }).promise;

      const result = canvasEl.toDataURL("image/jpeg", 0.8);

      evictOldestPdf();
      pdfThumbnailCache.set(path, result);
      pdf.destroy();
      return result;
    } catch (err) {
      console.warn("[pdf-preview] Failed to render thumbnail:", path, err);
      return null;
    } finally {
      pdfInflight.delete(path);
    }
  })();

  pdfInflight.set(path, promise);
  return promise;
}

/** Check what kind of preview a file should get */
export function getPreviewType(
  extension: string,
  isDir: boolean,
  size: number,
): "text" | "pdf" | "none" {
  if (isDir) return "none";
  if (isTextPreviewable(extension) && size <= MAX_FILE_SIZE_FOR_PREVIEW) return "text";
  if (isPdfFile(extension)) return "pdf";
  return "none";
}

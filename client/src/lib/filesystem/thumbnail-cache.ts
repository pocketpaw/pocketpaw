export const IMAGE_EXTENSIONS = new Set([
  "png",
  "jpg",
  "jpeg",
  "gif",
  "webp",
  "svg",
  "bmp",
]);

export function isImageFile(ext: string): boolean {
  return IMAGE_EXTENSIONS.has(ext.toLowerCase());
}

export function isPdfFile(ext: string): boolean {
  return ext.toLowerCase() === "pdf";
}

interface ThumbnailResult {
  data_url: string;
  width: number;
  height: number;
}

const MAX_CACHE_SIZE = 200;
const cache = new Map<string, string>();
const inflight = new Map<string, Promise<string | null>>();

function evictOldest() {
  if (cache.size >= MAX_CACHE_SIZE) {
    // Map iterates in insertion order — delete the first (oldest) entry
    const oldest = cache.keys().next().value;
    if (oldest !== undefined) {
      cache.delete(oldest);
    }
  }
}

function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export async function getThumbnail(path: string): Promise<string | null> {
  // Check memory cache
  const cached = cache.get(path);
  if (cached) {
    // Move to end (most recently used)
    cache.delete(path);
    cache.set(path, cached);
    return cached;
  }

  // Deduplicate in-flight requests
  const pending = inflight.get(path);
  if (pending) return pending;

  if (!isTauri()) return null;

  const promise = (async () => {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const result: ThumbnailResult = await invoke("fs_thumbnail", { path });
      evictOldest();
      cache.set(path, result.data_url);
      return result.data_url;
    } catch {
      return null;
    } finally {
      inflight.delete(path);
    }
  })();

  inflight.set(path, promise);
  return promise;
}

export function invalidateThumbnail(path: string): void {
  cache.delete(path);
}

export function clearThumbnailCache(): void {
  cache.clear();
}

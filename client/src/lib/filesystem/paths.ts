/**
 * Cross-platform path utilities for the file explorer.
 * These work both in-browser (string manipulation) and via Tauri IPC.
 */

function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/** Normalize path separators to forward slashes for display */
export function normalizeSeparators(p: string): string {
  return p.replace(/\\/g, "/");
}

/** Get the parent directory of a path (string-only, no IPC) */
export function parentDir(filePath: string): string {
  const normalized = normalizeSeparators(filePath);
  const lastSlash = normalized.lastIndexOf("/");
  if (lastSlash <= 0) return normalized.slice(0, lastSlash + 1) || "/";
  return normalized.slice(0, lastSlash);
}

/** Join path segments (string-only, no IPC) */
export function joinPath(...segments: string[]): string {
  if (segments.length === 0) return "";
  let result = segments[0];
  for (let i = 1; i < segments.length; i++) {
    const seg = segments[i];
    if (!seg) continue;
    // If segment is absolute, it replaces the result
    if (seg.startsWith("/") || /^[A-Za-z]:[\\/]/.test(seg)) {
      result = seg;
      continue;
    }
    const sep = result.endsWith("/") || result.endsWith("\\") ? "" : "/";
    result = result + sep + seg;
  }
  return result;
}

/** Check if a path is absolute */
export function isAbsolute(p: string): boolean {
  return p.startsWith("/") || /^[A-Za-z]:[\\/]/.test(p);
}

/**
 * Resolve a relative path against a base directory.
 * Uses Tauri IPC when available for proper OS-level resolution,
 * falls back to string manipulation.
 */
export async function resolvePath(
  path: string,
  baseDir?: string,
): Promise<string> {
  if (isAbsolute(path)) return path;

  if (isTauri()) {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      return await invoke("fs_resolve_path", { path, baseDir: baseDir ?? null });
    } catch {
      // fall through to string-based resolution
    }
  }

  // String-based fallback
  if (baseDir) {
    return joinPath(baseDir, path);
  }
  return path;
}

/**
 * Get the parent directory via Tauri IPC (accurate on all platforms).
 * Falls back to string-based parentDir.
 */
export async function getParentDir(filePath: string): Promise<string> {
  if (isTauri()) {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      return await invoke("fs_parent_dir", { path: filePath });
    } catch {
      // fall through
    }
  }
  return parentDir(filePath);
}

/** Get the file extension from a path */
export function getExtension(filePath: string): string {
  const name = filePath.split(/[\\/]/).pop() ?? "";
  const dotIdx = name.lastIndexOf(".");
  if (dotIdx <= 0) return "";
  return name.slice(dotIdx + 1).toLowerCase();
}

/** Get the file name from a path */
export function getFileName(filePath: string): string {
  return filePath.split(/[\\/]/).pop() ?? "";
}

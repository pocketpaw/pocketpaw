import type { ActionReturn } from "svelte/action";
import { getTextPreview, getPdfThumbnail, getPreviewType } from "./content-preview-cache";

export interface ContentPreviewResult {
  type: "text" | "pdf";
  /** Text content for text previews, data URL for PDF thumbnails */
  content: string;
}

interface ContentPreviewParams {
  path: string;
  extension: string;
  size: number;
  isDir: boolean;
  onLoad: (result: ContentPreviewResult) => void;
}

export function contentPreviewAction(
  node: HTMLElement,
  params: ContentPreviewParams,
): ActionReturn<ContentPreviewParams> {
  let observer: IntersectionObserver | null = null;
  let currentParams = params;
  let loaded = false;

  function setup() {
    cleanup();
    loaded = false;

    const previewType = getPreviewType(
      currentParams.extension,
      currentParams.isDir,
      currentParams.size,
    );

    if (previewType === "none") return;

    observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && !loaded) {
          loaded = true;
          observer?.disconnect();
          loadPreview(previewType);
        }
      },
      { rootMargin: "50px" },
    );

    observer.observe(node);
  }

  async function loadPreview(previewType: "text" | "pdf") {
    if (previewType === "text") {
      const text = await getTextPreview(
        currentParams.path,
        currentParams.extension,
        currentParams.size,
      );
      if (text) {
        currentParams.onLoad({ type: "text", content: text });
      }
    } else if (previewType === "pdf") {
      const thumbUrl = await getPdfThumbnail(currentParams.path);
      if (thumbUrl) {
        currentParams.onLoad({ type: "pdf", content: thumbUrl });
      }
    }
  }

  function cleanup() {
    observer?.disconnect();
    observer = null;
  }

  setup();

  return {
    update(newParams: ContentPreviewParams) {
      const changed =
        newParams.path !== currentParams.path ||
        newParams.extension !== currentParams.extension;
      currentParams = newParams;
      if (changed) setup();
    },
    destroy() {
      cleanup();
    },
  };
}

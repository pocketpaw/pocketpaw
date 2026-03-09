import { getThumbnail, isImageFile } from "$lib/filesystem";
import type { ActionReturn } from "svelte/action";

interface ThumbnailParams {
  path: string;
  extension: string;
  modified: number;
  onLoad: (dataUrl: string) => void;
}

export function thumbnailAction(
  node: HTMLElement,
  params: ThumbnailParams,
): ActionReturn<ThumbnailParams> {
  let observer: IntersectionObserver | null = null;
  let currentParams = params;
  let loaded = false;
  let lastModified = params.modified;

  function setup() {
    cleanup();
    loaded = false;

    if (!isImageFile(currentParams.extension)) return;

    observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && !loaded) {
          loaded = true;
          observer?.disconnect();
          getThumbnail(currentParams.path).then((url) => {
            if (url) currentParams.onLoad(url);
          });
        }
      },
      { rootMargin: "100px" },
    );

    observer.observe(node);
  }

  function cleanup() {
    observer?.disconnect();
    observer = null;
  }

  setup();

  return {
    update(newParams: ThumbnailParams) {
      const pathChanged = newParams.path !== currentParams.path;
      const modifiedChanged = newParams.modified !== lastModified;
      currentParams = newParams;
      if (pathChanged || modifiedChanged) {
        lastModified = newParams.modified;
        setup();
      }
    },
    destroy() {
      cleanup();
    },
  };
}

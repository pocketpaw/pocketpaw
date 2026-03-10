type NativeEffect = "Vibrancy" | "Mica" | "Acrylic" | "None";

class PlatformStore {
  viewportWidth = $state(typeof window !== "undefined" ? window.innerWidth : 1024);
  isTouch = $state(false);
  isNativeMobile = $state(false);
  desktopOS = $state<"macos" | "windows" | "linux" | "unknown">("unknown");
  nativeEffect = $state<NativeEffect>("None");

  isMobile = $derived(this.viewportWidth < 640);
  isTablet = $derived(this.viewportWidth >= 640 && this.viewportWidth < 1024);
  isDesktop = $derived(this.viewportWidth >= 1024);
  hasWindowChrome = $derived(!this.isNativeMobile);
  hasNativeBlur = $derived(this.nativeEffect !== "None");

  constructor() {
    if (typeof window === "undefined") return;

    // Viewport resize listener (plain listener — $effect can't be used at module scope)
    const onResize = () => {
      this.viewportWidth = window.innerWidth;
    };
    window.addEventListener("resize", onResize);

    // Touch detection
    const mq = window.matchMedia("(pointer: coarse)");
    this.isTouch = mq.matches;
    mq.addEventListener("change", (e) => {
      this.isTouch = e.matches;
    });

    // Native mobile + desktop OS detection via Tauri OS plugin
    this.detectNativePlatform();

    // Query current native effect from Rust
    this.queryNativeEffect();

    // Listen for native-effect events from Rust
    this.listenNativeEffect();
  }

  private async detectNativePlatform() {
    try {
      const { platform } = await import("@tauri-apps/plugin-os");
      const os = platform();
      this.isNativeMobile = os === "android" || os === "ios";
      if (os === "macos") this.desktopOS = "macos";
      else if (os === "windows") this.desktopOS = "windows";
      else if (os === "linux") this.desktopOS = "linux";
      else this.desktopOS = "unknown";
    } catch {
      this.isNativeMobile = false;
      this.desktopOS = "unknown";
    }
  }

  private async queryNativeEffect() {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const effect = await invoke<NativeEffect>("get_native_effect");
      this.nativeEffect = effect;
    } catch {
      // Not in Tauri or command not available
      this.nativeEffect = "None";
    }
  }

  private async listenNativeEffect() {
    try {
      const { listen } = await import("@tauri-apps/api/event");
      await listen<NativeEffect>("native-effect", (event) => {
        this.nativeEffect = event.payload;
      });
    } catch {
      // Not in Tauri
    }
  }
}

export const platformStore = new PlatformStore();

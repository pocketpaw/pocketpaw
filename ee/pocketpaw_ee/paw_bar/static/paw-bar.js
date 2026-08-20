// ee/pocketpaw_ee/paw_bar/static/paw-bar.js — the Paw Bar glass-bar LOADER, the
// zero-dependency IIFE a site includes to grow a concierge.
//
// GENERATED, DO NOT EDIT BY HAND. Produced by `bun run build:loader` in the
// paw-bar repo (loader/dist/loader.readable.js) and copied here verbatim.
// Source: qbtrix/paw-bar loader/src/loader.ts @ e8c1d81
//
// It used to be hand-transcribed TypeScript with the annotations stripped by
// hand. That drifts silently: this copy predated a whole session of loader
// fixes and still called goFullscreen() on `pawbar:open`, so a real site opened
// the messenger FULLSCREEN while the source it claimed to mirror had docked it
// to a 400px column for days. Nothing in the build or the tests could see it,
// because the vendored file is not what the paw-bar tests load.
//
// It is vendored rather than fetched because `GET /paw-bar/widget.js` must
// resolve to a real file on ANY machine that runs the backend (a sibling
// checkout is not a deployable dependency), and because a published Paw Site
// embeds this loader at publish time — a missing bundle would mean a site
// shipping a script tag pointing at a 404. `PAW_BAR_WIDGET_JS` overrides the
// path when an operator wants to serve a freshly built bundle instead.
//
// To update: rebuild in paw-bar, copy loader/dist/loader.readable.js over this
// file, and restore this header. tests/cloud/test_paw_bar_widget_js.py checks
// the copy has not fallen behind the behaviours the backend depends on.

"use strict";
(() => {
  // loader/src/loader.ts
  var LOADED_FLAG = "__pawBarLoaderLoaded";
  var FRAME_PATH = "/paw-bar/frame";
  var POS_KEY = "__pawbar_pos_v2";
  var DRAG_MIN_PX = 4;
  var BAR_W = 384;
  var DEFAULT_BAR_H = 96;
  var DEFAULT_CHIP = { w: 240, h: 72 };
  var MIN_H = 48;
  var VIEWPORT_MARGIN = 24;
  var PANEL_W = 400;
  var PANEL_MAX_H = 720;
  var PANEL_MIN_VW = 460;
  var PANEL_MIN_VH = 620;
  var BOX_MS = 260;
  var BOX_EASE = "cubic-bezier(0.16, 1, 0.3, 1)";
  (function bootstrap(win) {
    if (win[LOADED_FLAG]) return;
    const doc = win.document;
    const script = doc.currentScript ?? lastScriptWith("data-site-key", doc);
    if (!script) return;
    const siteKey = attr(script, "data-site-key");
    const widgetId = attr(script, "data-widget-id");
    if (!siteKey || !widgetId) {
      warn("missing data-site-key or data-widget-id");
      return;
    }
    const endpoint = normalizeEndpoint(
      attr(script, "data-endpoint") || originOf(script.src) + "/api/v1"
    );
    let frameOrigin;
    try {
      frameOrigin = new URL(endpoint).origin;
    } catch {
      warn("invalid data-endpoint");
      return;
    }
    win[LOADED_FLAG] = true;
    const parentOrigin = resolveParentOrigin(win);
    const src = endpoint + FRAME_PATH + "?key=" + encodeURIComponent(siteKey) + "&w=" + encodeURIComponent(widgetId) + "&po=" + encodeURIComponent(parentOrigin) + "&s=" + hostScheme(win);
    const iframe = doc.createElement("iframe");
    iframe.title = "Site concierge";
    iframe.setAttribute("allow", "clipboard-write");
    iframe.style.cssText = frameStyle();
    iframe.src = src;
    let view = "bar";
    let dockView = "bar";
    let overlay = false;
    let expanded = false;
    let anchor = readAnchor(win);
    let dragFrom = null;
    const size = {
      bar: { w: BAR_W, h: DEFAULT_BAR_H },
      chip: { w: DEFAULT_CHIP.w, h: DEFAULT_CHIP.h },
      panel: { w: PANEL_W, h: PANEL_MAX_H }
    };
    function panelIsSheet() {
      const vw = win.innerWidth || 0;
      const vh = win.innerHeight || 0;
      return view === "panel" && (vw < PANEL_MIN_VW || vh < PANEL_MIN_VH);
    }
    function dockBox() {
      const vw = win.innerWidth || 0;
      const vh = win.innerHeight || 0;
      const maxW = vw ? vw - VIEWPORT_MARGIN : BAR_W;
      const wantW = view === "bar" ? BAR_W : size[view].w;
      const w = Math.min(wantW, maxW);
      const wantH = view === "panel" ? PANEL_MAX_H : size[view].h;
      const h = vh ? clamp(wantH, MIN_H, vh - VIEWPORT_MARGIN) : Math.max(MIN_H, wantH);
      const cx = anchor ? anchor.cx : (vw || w) / 2;
      const by = anchor ? anchor.by : vh;
      const x = clamp(Math.round(cx - w / 2), 0, Math.max(0, vw - w));
      const y = clamp(Math.round(by - h), 0, Math.max(0, vh - h));
      return { x, y, w, h };
    }
    function reduced() {
      return !!win.matchMedia && win.matchMedia("(prefers-reduced-motion: reduce)").matches;
    }
    function setBox(x, y, w, h, animate) {
      iframe.style.transition = animate && !reduced() ? `left ${BOX_MS}ms ${BOX_EASE}, top ${BOX_MS}ms ${BOX_EASE}, width ${BOX_MS}ms ${BOX_EASE}, height ${BOX_MS}ms ${BOX_EASE}` : "none";
      iframe.style.left = x;
      iframe.style.top = y;
      iframe.style.width = w;
      iframe.style.height = h;
    }
    function applyDock(animate = false) {
      if (expanded || panelIsSheet()) {
        goFullscreen(animate);
        return;
      }
      const b = dockBox();
      setBox(b.x + "px", b.y + "px", b.w + "px", b.h + "px", animate);
    }
    function goFullscreen(animate = false) {
      setBox("0px", "0px", "100vw", "100vh", animate);
    }
    (doc.body || doc.documentElement).appendChild(iframe);
    applyDock();
    let overlayOpen = false;
    function watchHostPointer(on) {
      overlayOpen = on;
    }
    doc.addEventListener(
      "pointerdown",
      (ev) => {
        if (overlayOpen && ev.target !== iframe) postToFrame({ type: "pawbar:host-pointerdown" });
      },
      true
    );
    function postToFrame(msg) {
      const target = iframe.contentWindow;
      if (target) target.postMessage(msg, frameOrigin);
    }
    const schemeQuery = win.matchMedia && win.matchMedia("(prefers-color-scheme: dark)");
    if (schemeQuery && schemeQuery.addEventListener) {
      schemeQuery.addEventListener("change", () => {
        postToFrame({ type: "pawbar:scheme", s: hostScheme(win) });
      });
    }
    win.addEventListener("message", (ev) => {
      if (ev.origin !== frameOrigin) return;
      if (ev.source !== iframe.contentWindow) return;
      const data = ev.data;
      if (!data || typeof data !== "object") return;
      switch (data.type) {
        case "pawbar:resize": {
          if (overlay) break;
          if (view === "panel") break;
          const h = Number(data.h);
          if (Number.isFinite(h)) size[view].h = h;
          const w = Number(data.w);
          if (view !== "bar" && Number.isFinite(w) && w > 0) size[view].w = w;
          applyDock(false);
          break;
        }
        case "pawbar:view": {
          if (data.view === "bar" || data.view === "chip" || data.view === "panel") {
            view = data.view;
            overlay = false;
            if (data.view !== "panel") {
              dockView = data.view;
              expanded = false;
            }
            applyDock(true);
          }
          break;
        }
        case "pawbar:dead":
          watchHostPointer(false);
          iframe.remove();
          break;
        case "pawbar:open":
          view = "panel";
          overlay = false;
          applyDock(true);
          break;
        case "pawbar:expand":
          expanded = data.on === true;
          applyDock(true);
          break;
        case "pawbar:overlay":
          watchHostPointer(data.on === true);
          break;
        case "pawbar:close":
          view = dockView;
          overlay = false;
          expanded = false;
          applyDock(true);
          break;
        case "pawbar:drag": {
          if (data.phase === "start") {
            if (overlay) break;
            const b = dockBox();
            dragFrom = b;
            overlay = true;
            goFullscreen();
            postToFrame({ type: "pawbar:box", x: b.x, y: b.y, w: b.w, h: b.h });
          } else if (data.phase === "end") {
            const x = Number(data.x);
            const y = Number(data.y);
            const from = dragFrom;
            dragFrom = null;
            const moved = from && Number.isFinite(x) && Number.isFinite(y) ? Math.abs(x - from.x) + Math.abs(y - from.y) >= DRAG_MIN_PX : false;
            if (from && moved) {
              anchor = { cx: x + from.w / 2, by: y + from.h };
              writeAnchor(win, anchor);
            }
            overlay = false;
            applyDock();
          }
          break;
        }
      }
    });
    win.addEventListener("resize", () => {
      if (!overlay) applyDock();
    });
    win.PawBar = {
      // Must match `pawbar:open` exactly. It used to call goFullscreen(), so a
      // site with its own "Chat with us" button got the viewport-covering frame
      // the message path had already stopped producing — the same widget behaving
      // two different ways depending on which door the visitor came through.
      open() {
        view = "panel";
        overlay = false;
        applyDock(true);
        postToFrame({ type: "pawbar:host-open" });
      },
      close() {
        view = dockView;
        overlay = false;
        expanded = false;
        applyDock(true);
        postToFrame({ type: "pawbar:host-close" });
      }
    };
  })(window);
  function attr(el, name) {
    return (el.getAttribute(name) || "").trim();
  }
  function lastScriptWith(dataAttr, doc) {
    const list = doc.querySelectorAll("script[" + dataAttr + "]");
    return list.length ? list[list.length - 1] : null;
  }
  function hostScheme(win) {
    const doc = win.document;
    try {
      const declared = win.getComputedStyle(doc.documentElement).colorScheme || "";
      const dark = declared.indexOf("dark") >= 0;
      const light = declared.indexOf("light") >= 0;
      if (dark !== light) return dark ? "d" : "l";
      const roots = [doc.body, doc.documentElement];
      for (let i = 0; i < roots.length; i++) {
        const el = roots[i];
        if (!el) continue;
        const parts = win.getComputedStyle(el).backgroundColor.match(/[\d.]+/g);
        if (!parts || parts.length < 3 || parts.length > 3 && +parts[3] < 0.5) continue;
        const lum = (0.2126 * +parts[0] + 0.7152 * +parts[1] + 0.0722 * +parts[2]) / 255;
        return lum < 0.5 ? "d" : "l";
      }
    } catch {
    }
    return win.matchMedia && win.matchMedia("(prefers-color-scheme: dark)").matches ? "d" : "l";
  }
  function originOf(url) {
    try {
      return new URL(url, location.href).origin;
    } catch {
      return location.origin;
    }
  }
  function normalizeEndpoint(ep) {
    return ep.replace(/\/+$/, "");
  }
  function clamp(n, lo, hi) {
    return n < lo ? lo : n > hi ? hi : n;
  }
  function readAnchor(win) {
    try {
      const raw = win.localStorage.getItem(POS_KEY);
      if (!raw) return null;
      const p = JSON.parse(raw);
      if (Number.isFinite(p.cx) && Number.isFinite(p.by)) {
        return { cx: p.cx, by: p.by };
      }
    } catch {
    }
    return null;
  }
  function writeAnchor(win, a) {
    try {
      win.localStorage.setItem(POS_KEY, JSON.stringify(a));
    } catch {
    }
  }
  function resolveParentOrigin(win) {
    const own = win.location.origin;
    if (own && own !== "null") return own;
    try {
      const ao = win.location.ancestorOrigins;
      if (ao && ao.length && ao[0] && ao[0] !== "null") return ao[0];
    } catch {
    }
    try {
      if (win.document.referrer) {
        const o = new URL(win.document.referrer).origin;
        if (o && o !== "null") return o;
      }
    } catch {
    }
    return own;
  }
  function warn(msg) {
    try {
      console.warn("[PawBar] " + msg);
    } catch {
    }
  }
  function frameStyle() {
    return [
      "position:fixed",
      "left:0",
      "top:0",
      "width:0px",
      "height:0px",
      "max-width:100vw",
      "max-height:100vh",
      "border:0",
      "margin:0",
      "padding:0",
      "z-index:2147483647",
      "color-scheme:normal",
      "background:transparent"
    ].join(";");
  }
})();

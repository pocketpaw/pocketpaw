// ee/pocketpaw_ee/paw_bar/static/paw-bar.js — the Paw Bar glass-bar LOADER, the
// zero-dependency IIFE a site includes to grow a concierge.
//
// GENERATED, DO NOT EDIT BY HAND. Produced by `bun run build:loader` in the
// paw-bar repo (loader/dist/loader.readable.js) and copied here verbatim.
// Source: qbtrix/paw-bar loader/src/loader.ts. The source commit and the sha256 of
// everything below this header live in ../paw-bar-loader.pin.json, and
// tests/cloud/test_paw_bar_widget_js.py fails when the body stops matching it.
//
// It is vendored rather than fetched because `GET /paw-bar/widget.js` must
// resolve to a real file on ANY machine that runs the backend (a sibling
// checkout is not a deployable dependency), and because a published Paw Site
// embeds this loader at publish time — a missing bundle would mean a site
// shipping a script tag pointing at a 404. `PAW_BAR_WIDGET_JS` overrides the
// path when an operator wants to serve a freshly built bundle instead.
//
// What the loader does that the backend relies on: the iframe carries
// sandbox="<PAWBAR_FRAME_SANDBOX>", equal to the CSP sandbox header the router
// sends (test_paw_bar_widget_js.py pins the two), and allow="clipboard-write;
// microphone" for dictation. It reads the host site's look (accent, page colours,
// font and Google Fonts sheet, button radius) and hands it to the frame in the
// `#t=` URL fragment and as {pawbar:site-theme}; the bar layers the owner's
// tokens over it. `?pawbar=off` on the host page mounts nothing; `?pawbar=sniff`
// (the owner preview's scene) mounts nothing and posts the site theme to its
// parent instead.
//
// To update: run scripts/vendor-paw-bar-loader.sh (PAW_BAR_REF picks the ref,
// default origin/main). It builds paw-bar in a throwaway worktree, replaces
// everything below this header with loader/dist/loader.readable.js, and rewrites
// the pin file.
"use strict";
(() => {
  // loader/src/loader.ts
  var LOADED_FLAG = "__pawBarLoaderLoaded";
  var FRAME_PATH = "/paw-bar/frame";
  var FRAME_SANDBOX = "allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads";
  var POS_KEY = "__pawbar_pos_v2";
  var DRAG_MIN_PX = 4;
  var BAR_W = 384;
  var BAR_W_REST = 236;
  var DEFAULT_BAR_H = 96;
  var DEFAULT_CHIP = { w: 240, h: 72 };
  var MIN_H = 48;
  var VIEWPORT_MARGIN = 24;
  var PANEL_W = 520;
  var PANEL_MAX_H = 840;
  var PANEL_MIN_VW = 600;
  var PANEL_MIN_VH = 620;
  var SCRIM_BLUR_PX = 10;
  var SCRIM_DIM = "rgba(9,11,15,0.42)";
  var SCRIM_DIM_NO_BLUR = "rgba(9,11,15,0.58)";
  var BOX_MS = 260;
  var BOX_EASE = "cubic-bezier(0.16, 1, 0.3, 1)";
  function pawbarParam(win) {
    try {
      return new URLSearchParams(win.location.search).get("pawbar");
    } catch {
      return null;
    }
  }
  (function bootstrap(win) {
    if (win[LOADED_FLAG]) return;
    const param = pawbarParam(win);
    if (param === "off") return;
    const doc = win.document;
    const schemeQuery = win.matchMedia && win.matchMedia("(prefers-color-scheme: dark)");
    const onScheme = (fn) => {
      if (schemeQuery && schemeQuery.addEventListener) schemeQuery.addEventListener("change", fn);
    };
    if (param === "sniff") {
      const sniff = () => {
        try {
          win.parent.postMessage({ type: "pawbar:site-theme", theme: detectSiteTheme(win) }, "*");
        } catch {
        }
      };
      sniff();
      win.addEventListener("load", sniff);
      onScheme(sniff);
      win.addEventListener("message", (ev) => {
        if (ev.source === win.parent && ev.data && ev.data.type === "pawbar:sniff") sniff();
      });
      return;
    }
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
    let theme = JSON.stringify(detectSiteTheme(win));
    const src = endpoint + FRAME_PATH + "?key=" + encodeURIComponent(siteKey) + "&w=" + encodeURIComponent(widgetId) + "&po=" + encodeURIComponent(parentOrigin) + "&s=" + hostScheme(win) + (theme === "{}" ? "" : "#t=" + b64url(theme));
    const iframe = doc.createElement("iframe");
    iframe.title = "Site concierge";
    iframe.setAttribute("allow", "clipboard-write; microphone");
    iframe.setAttribute("sandbox", FRAME_SANDBOX);
    iframe.style.cssText = frameStyle();
    iframe.src = src;
    let view = "bar";
    let dockView = "bar";
    let overlay = false;
    let expanded = false;
    let barCompact = false;
    let barOpen = false;
    let barMotionUntil = 0;
    let anchor = readAnchor(win);
    let side = "";
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
      const wantW = view === "bar" ? barCompact && !barOpen ? BAR_W_REST : BAR_W : size[view].w;
      const w = Math.min(wantW, maxW);
      const wantH = view === "panel" ? PANEL_MAX_H : size[view].h;
      const h = vh ? clamp(wantH, MIN_H, vh - VIEWPORT_MARGIN) : Math.max(MIN_H, wantH);
      const cx = anchor ? anchor.cx : side ? side === "left" ? w / 2 + VIEWPORT_MARGIN / 2 : (vw || w) - w / 2 - VIEWPORT_MARGIN / 2 : (vw || w) / 2;
      const by = anchor ? anchor.by : vh;
      const x = clamp(Math.round(cx - w / 2), 0, Math.max(0, vw - w));
      const y = clamp(Math.round(by - h), 0, Math.max(0, vh - h));
      return { x, y, w, h };
    }
    function reduced() {
      return !!win.matchMedia && win.matchMedia("(prefers-reduced-motion: reduce)").matches;
    }
    function setBox(x, y, w, h, motion) {
      const m = reduced() ? "none" : motion;
      iframe.style.transition = m === "box" ? `left ${BOX_MS}ms ${BOX_EASE}, top ${BOX_MS}ms ${BOX_EASE}, width ${BOX_MS}ms ${BOX_EASE}, height ${BOX_MS}ms ${BOX_EASE}` : m === "width" ? `left ${BOX_MS}ms ${BOX_EASE}, width ${BOX_MS}ms ${BOX_EASE}` : "none";
      iframe.style.left = x;
      iframe.style.top = y;
      iframe.style.width = w;
      iframe.style.height = h;
    }
    let scrim = null;
    let scrimOn = false;
    function ensureScrim() {
      if (scrim) return scrim;
      const el = doc.createElement("div");
      el.setAttribute("aria-hidden", "true");
      const bs = el.style;
      const blur = "backdropFilter" in bs || "webkitBackdropFilter" in bs;
      el.style.cssText = "position:fixed;left:0;top:0;width:100%;height:100%;border:0;margin:0;padding:0;z-index:2147483646;opacity:0;pointer-events:none;background-color:" + (blur ? SCRIM_DIM : SCRIM_DIM_NO_BLUR);
      if (blur) bs.webkitBackdropFilter = bs.backdropFilter = `blur(${SCRIM_BLUR_PX}px)`;
      el.addEventListener("pointerdown", (ev) => {
        if (overlayOpen) return;
        ev.preventDefault();
        view = dockView;
        overlay = false;
        expanded = false;
        applyDock("box");
        postToFrame({ type: "pawbar:host-close" });
      });
      (doc.body || doc.documentElement).appendChild(el);
      scrim = el;
      return el;
    }
    function setScrim(on) {
      if (on === scrimOn) return;
      scrimOn = on;
      if (!on && !scrim) return;
      const el = ensureScrim();
      el.style.transition = reduced() ? "none" : `opacity ${BOX_MS}ms ${BOX_EASE}`;
      el.style.opacity = on ? "1" : "0";
      el.style.pointerEvents = on ? "auto" : "none";
    }
    function applyDock(motion = "none") {
      setScrim(view === "panel");
      if (expanded || panelIsSheet()) {
        goFullscreen(motion);
        return;
      }
      const b = dockBox();
      setBox(b.x + "px", b.y + "px", b.w + "px", b.h + "px", motion);
    }
    function goFullscreen(motion = "none") {
      setBox("0px", "0px", "100vw", "100vh", motion);
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
    function postTheme() {
      const next = JSON.stringify(detectSiteTheme(win));
      if (next === theme) return;
      theme = next;
      postToFrame({ type: "pawbar:site-theme", theme: JSON.parse(next) });
    }
    onScheme(() => {
      postToFrame({ type: "pawbar:scheme", s: hostScheme(win) });
      postTheme();
    });
    win.addEventListener("load", postTheme);
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
          side = data.side === "left" || data.side === "right" ? data.side : "";
          const w = Number(data.w);
          if (view !== "bar" && Number.isFinite(w) && w > 0) size[view].w = w;
          applyDock(Date.now() < barMotionUntil ? "width" : "none");
          break;
        }
        case "pawbar:bar": {
          const compact = data.compact === true;
          const open = data.expanded === true;
          if (compact === barCompact && open === barOpen) break;
          barCompact = compact;
          barOpen = open;
          if (view !== "bar") break;
          barMotionUntil = Date.now() + BOX_MS;
          applyDock("width");
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
            applyDock("box");
          }
          break;
        }
        case "pawbar:dead":
          watchHostPointer(false);
          iframe.remove();
          if (scrim) {
            scrim.remove();
            scrim = null;
            scrimOn = false;
          }
          break;
        case "pawbar:open":
          view = "panel";
          overlay = false;
          applyDock("box");
          break;
        case "pawbar:expand":
          expanded = data.on === true;
          applyDock("box");
          break;
        case "pawbar:overlay":
          watchHostPointer(data.on === true);
          break;
        case "pawbar:close":
          view = dockView;
          overlay = false;
          expanded = false;
          applyDock("box");
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
    function postViewport() {
      postToFrame({ type: "pawbar:viewport", w: win.innerWidth, h: win.innerHeight });
    }
    let lastPage = "";
    let watching = false;
    function postPage(force) {
      try {
        const l = win.location;
        const url = l.origin + l.pathname;
        const title = doc.title.slice(0, 120);
        const key = url + " " + title;
        if (!force && key === lastPage) return;
        lastPage = key;
        postToFrame({ type: "pawbar:page", url, title });
      } catch (_) {
      }
    }
    const pageChanged = () => postPage();
    iframe.addEventListener("load", () => {
      postViewport();
      postPage(true);
      if (watching) return;
      watching = true;
      win.addEventListener("popstate", pageChanged);
      win.addEventListener("hashchange", pageChanged);
      win.setInterval(pageChanged, 1e3);
    });
    win.addEventListener("resize", () => {
      if (!overlay) applyDock();
      postViewport();
    });
    win.PawBar = {
      // Must match `pawbar:open` exactly. It used to call goFullscreen(), so a
      // site with its own "Chat with us" button got the viewport-covering frame
      // the message path had already stopped producing — the same widget behaving
      // two different ways depending on which door the visitor came through.
      open() {
        view = "panel";
        overlay = false;
        applyDock("box");
        postToFrame({ type: "pawbar:host-open" });
      },
      close() {
        view = dockView;
        overlay = false;
        expanded = false;
        applyDock("box");
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
  function detectSiteTheme(win) {
    const doc = win.document;
    const t = {};
    try {
      const cs = (el) => win.getComputedStyle(el);
      const probe = doc.createElement("i").style;
      const hex = (v) => {
        probe.color = "";
        probe.color = (v || "").trim();
        if (!probe.color && v) probe.color = "hsl(" + v + ")";
        const p = probe.color.match(/[\d.]+/g);
        if (!p || p.length < 3 || p.length > 3 && +p[3] < 0.5) return "";
        return "#" + p.slice(0, 3).map((n) => (256 | +n).toString(16).slice(1)).join("");
      };
      const brand = (c) => {
        const n = [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
        return c && Math.max(...n) - Math.min(...n) > 32 ? c : "";
      };
      const meta = doc.querySelector("meta[name=theme-color]");
      let accent = brand(hex(meta && meta.getAttribute("content")));
      const root = cs(doc.documentElement);
      "primary,accent,brand,color-primary,primary-color,brand-color,color-accent,accent-color,color-brand".split(",").forEach((n) => accent = accent || brand(hex(root.getPropertyValue("--" + n))));
      let btn = null;
      let filled = null;
      const list = doc.querySelectorAll("button,.btn,[class*=button],a[class*=btn]");
      for (let i = 0; i < list.length && i < 60 && !btn; i++) {
        const s = cs(list[i]);
        const bg = s.display !== "none" && s.visibility !== "hidden" ? hex(s.backgroundColor) : "";
        filled = filled || (bg ? s : null);
        if (brand(bg)) {
          btn = s;
          accent = accent || bg;
        }
      }
      const a = doc.querySelector("a[href]");
      const link = a ? brand(hex(cs(a).color)) : "";
      accent = accent || (link === "#0000ee" ? "" : link);
      if (accent) t.accent = accent;
      for (const el of [doc.body, doc.documentElement]) {
        const bg = el && hex(cs(el).backgroundColor);
        if (bg) {
          t.bg = bg;
          break;
        }
      }
      const body = cs(doc.body || doc.documentElement);
      const fg = hex(body.color);
      if (fg) t.fg = fg;
      if (body.fontFamily) t.font = body.fontFamily.slice(0, 200);
      const gf = doc.querySelector(
        'link[rel=stylesheet][href^="https://fonts.googleapis.com/css"]'
      );
      if (gf) t.fontHref = gf.href;
      const b = btn || filled;
      const r = b ? b.borderTopLeftRadius || b.borderRadius : "";
      if (r && r.indexOf("%") < 0 && isFinite(parseFloat(r))) t.radius = clamp(Math.round(parseFloat(r)), 0, 32);
    } catch {
    }
    return t;
  }
  function b64url(s) {
    return btoa(unescape(encodeURIComponent(s))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
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

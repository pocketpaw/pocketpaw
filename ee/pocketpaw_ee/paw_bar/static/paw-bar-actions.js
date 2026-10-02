// ee/pocketpaw_ee/paw_bar/static/paw-bar-actions.js — the Paw Bar PAGE-ACTIONS
// host script, the opt-in IIFE a site includes beside the loader so the
// concierge can act on the host page (scroll to and highlight an element, open a
// link) and call tools the site declares on window.pawbarTools. It reads that
// array, replaces its push so later declarations register too, and offers the
// tools to the frame. It obeys only `pawbar:act` messages posted by a
// /paw-bar/frame iframe whose origin matches its own endpoint, and replies with
// `pawbar:act-result`.
//
// GENERATED, DO NOT EDIT BY HAND. Produced by `bun run build:actions` in the
// paw-bar repo (actions/dist/actions.readable.js) and copied here verbatim.
// Source: qbtrix/paw-bar actions/src/actions.ts @ 663feef (PR #40)
//
// Vendored rather than fetched for the same reason as paw-bar.js: the URL
// `GET /paw-bar/actions.js` is shown to site owners to paste into their pages,
// so it has to resolve on any machine that runs the backend.
// `PAW_BAR_ACTIONS_JS` overrides the path to serve a freshly built bundle.
//
// To update: rebuild in paw-bar, copy actions/dist/actions.readable.js over this
// file, and restore this header. tests/cloud/test_paw_bar_actions_js.py checks
// the copy still speaks the protocol the frame uses.
"use strict";
(() => {
  // actions/src/actions.ts
  var LOADED_FLAG = "__pawBarActionsLoaded";
  var SELF_PATH = /\/paw-bar\/actions\.js$/;
  var FRAME_PATH = /\/paw-bar\/frame$/;
  var ID_TARGET = /^#[A-Za-z][\w-]{0,63}$/;
  var TARGET_MAX = 120;
  var HIGHLIGHT_MS = 2e3;
  var FADE_MS = 300;
  var TOOL_NAME = /^[a-z][a-z0-9_]{0,39}$/;
  var TOOLS_MAX = 12;
  var SCHEMA_MAX = 2048;
  var TOOL_MS = 1e4;
  (function boot(win) {
    if (win[LOADED_FLAG]) return;
    const doc = win.document;
    const script = doc.currentScript || Array.from(doc.scripts).reverse().find((s) => SELF_PATH.test(s.src.split(/[?#]/)[0]));
    if (!script) return;
    let frameOrigin;
    try {
      frameOrigin = new URL(script.getAttribute("data-endpoint") || script.src, win.location.href).origin;
    } catch {
      return;
    }
    if (frameOrigin === "null") return;
    win[LOADED_FLAG] = true;
    let overlay = null;
    let overlayTimer = 0;
    let sendTimer = 0;
    const tools = /* @__PURE__ */ new Map();
    function frameWin(source) {
      for (const f of Array.from(doc.querySelectorAll("iframe"))) {
        const w = f.contentWindow;
        if (!w || source !== void 0 && w !== source) continue;
        try {
          const u = new URL(f.src);
          if (u.origin === frameOrigin && FRAME_PATH.test(u.pathname)) return w;
        } catch {
        }
      }
      return null;
    }
    function addTool(t) {
      try {
        const j = JSON.stringify(t.inputSchema);
        if (TOOL_NAME.test(t.name) && typeof t.description == "string" && typeof t.execute == "function" && j.length <= SCHEMA_MAX && (tools.size < TOOLS_MAX || tools.has(t.name))) {
          const w = { name: t.name, description: t.description, inputSchema: JSON.parse(j), confirm: t.confirm !== false };
          tools.set(t.name, [w, t.execute]);
          return;
        }
      } catch {
      }
      console.warn("paw-bar: bad tool", t);
    }
    function sendTools(w = frameWin()) {
      w?.postMessage({ type: "pawbar:tools", tools: Array.from(tools.values(), (t) => t[0]) }, frameOrigin);
    }
    function sendSoon() {
      clearTimeout(sendTimer);
      sendTimer = setTimeout(sendTools, 50);
    }
    function runTool(d, reply) {
      const t = tools.get(d.name);
      if (!t) return reply({ ok: false, error: "not_found" });
      let done = 0;
      const fin = (r) => done++ || reply(r);
      setTimeout(() => fin({ ok: false, error: "timeout" }), TOOL_MS);
      new Promise((res) => res(t[1](d.args ?? {}))).then(
        (r) => {
          const ok = r?.ok !== false;
          const m = r?.message;
          fin({ ok, error: ok ? void 0 : "failed", message: typeof m == "string" && m ? m.slice(0, 160) : void 0 });
        },
        () => fin({ ok: false, error: "failed" })
      );
    }
    const queue = Array.isArray(win.pawbarTools) ? win.pawbarTools : win.pawbarTools = [];
    queue.splice(0).forEach(addTool);
    queue.push = (...ts) => {
      ts.forEach(addTool);
      sendSoon();
    };
    if (tools.size) sendSoon();
    const norm = (p) => p.replace(/\/+$/, "") || "/";
    function navigate(to) {
      clearOverlay();
      if (typeof to !== "string") return "unsupported";
      let url;
      try {
        url = new URL(to, win.location.href);
      } catch {
        return "blocked";
      }
      const loc = win.location;
      if (url.origin !== loc.origin) return "blocked";
      const want = norm(url.pathname) + url.search;
      if (want === norm(loc.pathname) + loc.search) {
        const el = url.hash ? find(url.hash) : null;
        return () => el?.scrollIntoView({ block: "center", behavior: reduced() ? "auto" : "smooth" });
      }
      const link = Array.from(doc.querySelectorAll("a[href]")).find(
        (a) => a.origin === loc.origin && norm(a.pathname) + a.search === want && a.hash === url.hash && !a.hasAttribute("download") && (!a.target || a.target === "_self")
      );
      return () => link ? link.click() : loc.assign(url.href);
    }
    function find(target) {
      if (typeof target !== "string" || target.length > TARGET_MAX || /[<>]/.test(target)) return null;
      if (ID_TARGET.test(target)) {
        const el = doc.getElementById(target.slice(1));
        if (el) return el;
      }
      const q = target.trim().toLowerCase();
      if (!q) return null;
      for (const h of Array.from(doc.querySelectorAll("h1,h2,h3,h4"))) {
        if ((h.textContent || "").toLowerCase().includes(q)) return h;
      }
      return null;
    }
    function reduced() {
      try {
        return win.matchMedia("(prefers-reduced-motion: reduce)").matches;
      } catch {
        return false;
      }
    }
    function clearOverlay() {
      win.clearTimeout(overlayTimer);
      overlay?.remove();
      overlay = null;
    }
    function fadeOverlay() {
      if (!overlay || reduced()) return clearOverlay();
      overlay.addEventListener("transitionend", clearOverlay, { once: true });
      overlayTimer = win.setTimeout(clearOverlay, FADE_MS + 100);
      overlay.style.opacity = "0";
    }
    function highlight(el) {
      clearOverlay();
      const r = el.getBoundingClientRect();
      const box = doc.createElement("div");
      box.setAttribute("data-pawbar-highlight", "");
      box.setAttribute("aria-hidden", "true");
      const pad = 6;
      box.style.cssText = `position:absolute;pointer-events:none;z-index:2147483646;box-sizing:border-box;border:3px solid #3b82f6;border-radius:10px;box-shadow:0 0 0 6px rgba(59,130,246,.25);top:${r.top + win.scrollY - pad}px;left:${r.left + win.scrollX - pad}px;width:${r.width + pad * 2}px;height:${r.height + pad * 2}px;` + (reduced() ? "" : `transition:opacity ${FADE_MS}ms;`);
      doc.documentElement.appendChild(box);
      overlay = box;
      overlayTimer = win.setTimeout(fadeOverlay, HIGHLIGHT_MS);
    }
    function run(d) {
      if (d.do === "navigate") return navigate(d.to);
      if (d.do !== "scroll_to" && d.do !== "highlight") return "unsupported";
      const el = find(d.target);
      if (!el) return "not_found";
      return () => {
        el.scrollIntoView({ block: "center", behavior: reduced() ? "auto" : "smooth" });
        if (d.do === "highlight") highlight(el);
      };
    }
    win.addEventListener("popstate", clearOverlay);
    win.addEventListener("message", (ev) => {
      if (ev.origin !== frameOrigin) return;
      const d = ev.data;
      if (!d || typeof d !== "object") return;
      if (d.type === "pawbar:tools-request") return sendTools(frameWin(ev.source));
      if (d.type !== "pawbar:act" || typeof d.id !== "string") return;
      const target = frameWin(ev.source);
      if (!target) return;
      const reply = (r) => target.postMessage({ type: "pawbar:act-result", id: d.id, ...r }, frameOrigin);
      if (d.do === "tool") return runTool(d, reply);
      let out;
      try {
        out = run(d);
      } catch {
        out = "unsupported";
      }
      const ok = typeof out === "function";
      reply({ ok, ...ok ? {} : { error: out } });
      if (ok) {
        try {
          out();
        } catch {
        }
      }
    });
  })(window);
})();

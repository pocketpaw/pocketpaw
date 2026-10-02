// ee/pocketpaw_ee/paw_bar/static/paw-bar-actions.js — the Paw Bar PAGE-ACTIONS
// host script, the opt-in IIFE a site includes beside the loader so the
// concierge can act on the host page (scroll to and highlight an element, open a
// link). It obeys only `pawbar:act` messages posted by a /paw-bar/frame iframe
// whose origin matches its own endpoint, and replies with `pawbar:act-result`.
//
// GENERATED, DO NOT EDIT BY HAND. Produced by `bun run build:actions` in the
// paw-bar repo (actions/dist/actions.readable.js) and copied here verbatim.
// Source: qbtrix/paw-bar actions/src/actions.ts @ 947592a (PR #38)
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
    function frameFor(source) {
      if (!source) return null;
      for (const f of Array.from(doc.querySelectorAll("iframe"))) {
        if (f.contentWindow !== source) continue;
        try {
          const u = new URL(f.src);
          if (u.origin === frameOrigin && FRAME_PATH.test(u.pathname)) return f;
        } catch {
        }
      }
      return null;
    }
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
      if (!d || typeof d !== "object" || d.type !== "pawbar:act" || typeof d.id !== "string") return;
      const frame = frameFor(ev.source);
      const target = frame?.contentWindow;
      if (!target) return;
      let out;
      try {
        out = run(d);
      } catch {
        out = "unsupported";
      }
      const ok = typeof out === "function";
      target.postMessage(
        { type: "pawbar:act-result", id: d.id, ok, ...ok ? {} : { error: out } },
        frameOrigin
      );
      if (ok) {
        try {
          out();
        } catch {
        }
      }
    });
  })(window);
})();

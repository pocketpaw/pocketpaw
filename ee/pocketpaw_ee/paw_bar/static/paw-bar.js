// ee/pocketpaw_ee/paw_bar/static/paw-bar.js — the Paw Bar glass-bar LOADER, the
// ~7KB zero-dependency IIFE a site includes to grow a concierge.
//
// Vendored 2026-07-30 (feat/paw-bar-autoembed) from the paw-bar repo's
// ``loader/src/loader.ts`` (branch ``feat/glass-bar``), type annotations stripped
// — this is that file as plain ES2020, nothing added, nothing removed. It is
// vendored rather than fetched because ``GET /paw-bar/widget.js`` must resolve to
// a real file on ANY machine that runs the backend (a sibling checkout is not a
// deployable dependency), and because a published Paw Site now embeds this loader
// automatically at publish time — a missing bundle would mean a site that ships a
// script tag pointing at a 404. ``PAW_BAR_WIDGET_JS`` overrides the path when an
// operator wants to serve a freshly built bundle instead of this copy.
//
// It finds its own <script> tag, reads the embed config off it (data-site-key /
// data-widget-id / data-endpoint), computes the host (parent) origin, and mounts
// the concierge iframe pointing at the frame endpoint
// (/paw-bar/frame?key=&w=&po=). The loader owns ONLY the iframe box (size +
// position); the glass app renders INSIDE the iframe and drives the box over
// postMessage.
//
// Bar-first docking: the docked resting state is a center-bottom BAR (width capped
// at BAR_MAX_W) that the app can flip to a minimized CHIP ({pawbar:view}).
// {pawbar:resize,h,w} sizes the docked box — height always, width only for the chip
// (the bar width is loader policy; using the app-reported width for the bar would
// feed back and shrink it). OPEN is a full-viewport overlay (the app draws the dim
// backdrop + centered palette). MOVE: on {pawbar:drag,phase:start} the loader
// snapshots the dock box, goes full-viewport, and replies {pawbar:box,x,y,w,h} so
// the app can track the pointer; {pawbar:drag,phase:end,x,y} adopts the new anchor
// and persists it (host localStorage) so the placement survives reloads. The anchor
// is the box's CENTER-BOTTOM point, so the bar and the (narrower) chip stay pinned
// to the same visual spot; a sub-DRAG_MIN_PX "drag" is a click on the grip and
// adopts nothing (else the default-centered dock gets silently pinned).
//
// SECURITY: inbound messages are honoured ONLY when event.origin === the frame
// origin AND event.source === the iframe's own contentWindow. Every outbound post
// pins targetOrigin to the frame origin — never "*". Idempotent; exposes
// window.PawBar = { open, close } for programmatic control.
//
// The whole file is wrapped in ONE outer IIFE. The TypeScript original is a
// MODULE (it ends in ``export {}``) and esbuild bundles it ``format: 'iife'``, so
// its constants and helpers were never global. Served raw as a classic script
// they would be, and this loader runs on a page we do not own — a bare top-level
// ``const clamp`` / ``function warn`` would collide with the host site's own
// globals (and a second ``const`` of the same name is a hard SyntaxError). The
// wrapper restores exactly the scoping the bundler gave it: the ONLY global this
// file creates is ``window.PawBar``.

(function () {
  'use strict';

  const LOADED_FLAG = '__pawBarLoaderLoaded';
  const FRAME_PATH = '/paw-bar/frame';
  // v2: the anchor is the box's CENTER-BOTTOM point {cx, by}, not a top-left —
  // a top-left pins the smaller chip to the bar's left edge when views flip.
  const POS_KEY = '__pawbar_pos_v2';
  // Pointer travel below this is a click on the grip, not a move — adopting an
  // anchor for it would silently pin the default-centered dock forever.
  const DRAG_MIN_PX = 4;

  // Dock policy. Heights (and the chip width) are app-reported via
  // {pawbar:resize}; these are just the pre-report defaults and caps.
  const BAR_MAX_W = 720;
  const DEFAULT_BAR_H = 96;
  const DEFAULT_CHIP = { w: 240, h: 72 };
  const MIN_H = 48;
  const VIEWPORT_MARGIN = 24; // keep the dock off the very edge on small screens

  (function bootstrap(win) {
    // Idempotent: a duplicate paste / double-include must be a silent no-op.
    if (win[LOADED_FLAG]) return;

    const doc = win.document;

    // 1. Locate our own <script> tag and read the embed config off it.
    const script = doc.currentScript || lastScriptWith('data-site-key', doc);
    if (!script) return;

    const siteKey = attr(script, 'data-site-key');
    const widgetId = attr(script, 'data-widget-id');
    if (!siteKey || !widgetId) {
      // Missing required config — fail quietly; embedders find it in the console.
      warn('missing data-site-key or data-widget-id');
      return;
    }

    const endpoint = normalizeEndpoint(
      attr(script, 'data-endpoint') || originOf(script.src) + '/api/v1',
    );
    let frameOrigin;
    try {
      frameOrigin = new URL(endpoint).origin;
    } catch (err) {
      warn('invalid data-endpoint');
      return;
    }

    // Mark loaded only after the config validates, so a broken first include does
    // not block a corrected second one.
    win[LOADED_FLAG] = true;

    // 2. The origin the iframe must post back to is the host page's origin.
    const parentOrigin = resolveParentOrigin(win);

    // 3. Build the frame URL and mount the docked iframe.
    const src =
      endpoint +
      FRAME_PATH +
      '?key=' +
      encodeURIComponent(siteKey) +
      '&w=' +
      encodeURIComponent(widgetId) +
      '&po=' +
      encodeURIComponent(parentOrigin);

    const iframe = doc.createElement('iframe');
    iframe.title = 'Site concierge';
    iframe.setAttribute('allow', 'clipboard-write');
    // Inline styles are required here: the loader runs on a foreign page and must
    // neither depend on nor inject a stylesheet. One fixed, borderless box;
    // max-*:100v* is a CSS safety net so it can never exceed the viewport.
    iframe.style.cssText = frameStyle();
    iframe.src = src;

    // Dock state. `anchor` is the user-chosen CENTER-BOTTOM point (null = default
    // centered at the viewport bottom); `overlay` = panel open or mid-drag, when
    // the iframe is the whole viewport and dock sizing must not apply. `dragFrom`
    // is the box snapshot at drag start, for the no-move guard and coordinate
    // conversion at drag end.
    let view = 'bar';
    let overlay = false;
    let anchor = readAnchor(win);
    let dragFrom = null;
    const size = { bar: { h: DEFAULT_BAR_H }, chip: { w: DEFAULT_CHIP.w, h: DEFAULT_CHIP.h } };

    function dockBox() {
      const vw = win.innerWidth || 0;
      const vh = win.innerHeight || 0;
      const maxW = vw ? vw - VIEWPORT_MARGIN : BAR_MAX_W;
      const w = view === 'bar' ? Math.min(BAR_MAX_W, maxW) : Math.min(size.chip.w, maxW);
      const h = vh ? clamp(size[view].h, MIN_H, vh - VIEWPORT_MARGIN) : Math.max(MIN_H, size[view].h);
      // Derive this box's top-left from the center-bottom anchor so bar and chip
      // stay visually anchored to the same spot despite their different sizes.
      const cx = anchor ? anchor.cx : (vw || w) / 2;
      const by = anchor ? anchor.by : vh;
      const x = clamp(Math.round(cx - w / 2), 0, Math.max(0, vw - w));
      const y = clamp(Math.round(by - h), 0, Math.max(0, vh - h));
      return { x: x, y: y, w: w, h: h };
    }

    function applyDock() {
      const b = dockBox();
      iframe.style.left = b.x + 'px';
      iframe.style.top = b.y + 'px';
      iframe.style.width = b.w + 'px';
      iframe.style.height = b.h + 'px';
    }

    function goFullscreen() {
      iframe.style.left = '0px';
      iframe.style.top = '0px';
      iframe.style.width = '100vw';
      iframe.style.height = '100vh';
    }

    (doc.body || doc.documentElement).appendChild(iframe);
    applyDock();

    function postToFrame(msg) {
      // ALWAYS pin to the frame origin — never "*".
      const target = iframe.contentWindow;
      if (target) target.postMessage(msg, frameOrigin);
    }

    // 4. postMessage handshake — accept ONLY messages provably from our iframe:
    //    exact origin match AND source-identity match. Anything else is ignored.
    win.addEventListener('message', function (ev) {
      if (ev.origin !== frameOrigin) return;
      if (ev.source !== iframe.contentWindow) return;
      const data = ev.data;
      if (!data || typeof data !== 'object') return;
      switch (data.type) {
        case 'pawbar:resize': {
          if (overlay) break; // the overlay is viewport-sized; dock reports wait
          const h = Number(data.h);
          if (Number.isFinite(h)) size[view].h = h;
          const w = Number(data.w);
          // Width is honoured for the chip only — the bar width is loader policy.
          if (view === 'chip' && Number.isFinite(w) && w > 0) size.chip.w = w;
          applyDock();
          break;
        }
        case 'pawbar:view': {
          if (data.view === 'bar' || data.view === 'chip') {
            view = data.view;
            overlay = false;
            applyDock();
          }
          break;
        }
        case 'pawbar:dead':
          // The frame declined to render (concierge disabled / unusable
          // allowlist): remove the iframe entirely so the site shows NOTHING.
          iframe.remove();
          break;
        case 'pawbar:open':
          overlay = true;
          goFullscreen();
          break;
        case 'pawbar:close':
          overlay = false;
          applyDock();
          break;
        case 'pawbar:drag': {
          if (data.phase === 'start') {
            if (overlay) break;
            const b = dockBox();
            dragFrom = b;
            overlay = true;
            goFullscreen();
            postToFrame({ type: 'pawbar:box', x: b.x, y: b.y, w: b.w, h: b.h });
          } else if (data.phase === 'end') {
            const x = Number(data.x);
            const y = Number(data.y);
            const from = dragFrom;
            dragFrom = null;
            const moved =
              from && Number.isFinite(x) && Number.isFinite(y)
                ? Math.abs(x - from.x) + Math.abs(y - from.y) >= DRAG_MIN_PX
                : false;
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

    // Re-clamp the dock on rotation / resize (mobile). The overlay is vw/vh-sized
    // and tracks the viewport by itself.
    win.addEventListener('resize', function () {
      if (!overlay) applyDock();
    });

    // 5. Programmatic control for embedders. Resizes the chrome the loader owns
    //    AND forwards a pinned host-intent to the app (forward-compatible: the app
    //    honours pawbar:host-open / pawbar:host-close).
    win.PawBar = {
      open: function () {
        overlay = true;
        goFullscreen();
        postToFrame({ type: 'pawbar:host-open' });
      },
      close: function () {
        overlay = false;
        applyDock();
        postToFrame({ type: 'pawbar:host-close' });
      },
    };
  })(window);

  // ── helpers ─────────────────────────────────────────────────────────────────

  function attr(el, name) {
    return (el.getAttribute(name) || '').trim();
  }

  function lastScriptWith(dataAttr, doc) {
    const list = doc.querySelectorAll('script[' + dataAttr + ']');
    return list.length ? list[list.length - 1] : null;
  }

  function originOf(url) {
    try {
      return new URL(url, location.href).origin;
    } catch (err) {
      return location.origin;
    }
  }

  function normalizeEndpoint(ep) {
    return ep.replace(/\/+$/, '');
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
    } catch (err) {
      /* storage denied / corrupt — fall back to the default placement */
    }
    return null;
  }

  function writeAnchor(win, a) {
    try {
      win.localStorage.setItem(POS_KEY, JSON.stringify(a));
    } catch (err) {
      /* storage denied — the placement just doesn't persist */
    }
  }

  function resolveParentOrigin(win) {
    // The iframe posts to window.parent (this loader's window); its origin is the
    // value the frame must target. location.origin is correct in every nesting
    // case EXCEPT a sandboxed / opaque origin ("null"), where we best-effort
    // recover the real host origin from the ancestor chain, then the referrer.
    const own = win.location.origin;
    if (own && own !== 'null') return own;
    try {
      const ao = win.location.ancestorOrigins;
      if (ao && ao.length && ao[0] && ao[0] !== 'null') return ao[0];
    } catch (err) {
      /* ancestorOrigins unsupported (Firefox) — fall through */
    }
    try {
      if (win.document.referrer) {
        const o = new URL(win.document.referrer).origin;
        if (o && o !== 'null') return o;
      }
    } catch (err) {
      /* malformed referrer — fall through */
    }
    return own;
  }

  function warn(msg) {
    try {
      console.warn('[PawBar] ' + msg);
    } catch (err) {
      /* no console */
    }
  }

  function frameStyle() {
    return [
      'position:fixed',
      'left:0',
      'top:0',
      'width:0px',
      'height:0px',
      'max-width:100vw',
      'max-height:100vh',
      'border:0',
      'margin:0',
      'padding:0',
      'z-index:2147483647',
      'color-scheme:normal',
      'background:transparent',
    ].join(';');
  }
})();

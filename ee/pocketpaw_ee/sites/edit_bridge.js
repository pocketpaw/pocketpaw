// ee/pocketpaw_ee/sites/edit_bridge.js — the paw-sites postMessage edit-bridge, vendored
// for the draft preview origin's html lane (preview_origin.py injects it into html
// pages served with ?paw_edit=1). Svelte and react drafts carry their own copy, baked
// by the armed build, so this file is ONLY used where no build runs.
//
// Generated from paw-sites src/edit-bridge.ts editBridgeScript('__PAW_BUILDER_ORIGIN__')
// (paw-sites 72f1f46). Do not hand-edit: regenerate from paw-sites when the bridge
// protocol changes. The quoted placeholder is replaced with the builder origin.

(function () {
  try {
    var params = new URLSearchParams(window.location.search);
    if (params.get("paw_edit") !== '1') return;
    if (window.parent === window) return;
    var ORIGIN = "__PAW_BUILDER_ORIGIN__";
    var NONCE = params.get("paw_nonce") || "";
    function ancestorWith(el, attr) {
      var node = el;
      while (node && node.nodeType === 1) {
        if (node.hasAttribute && node.hasAttribute(attr)) return node;
        node = node.parentElement;
      }
      return null;
    }
    function boxOf(el) {
      var r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, width: r.width, height: r.height, top: r.top, left: r.left };
    }
    var TAG_ROLE={h1:"heading",h2:"heading",h3:"heading",h4:"heading",h5:"heading",h6:"heading",button:"button",a:"link",img:"image",picture:"image",svg:"icon",input:"input field",textarea:"input field",select:"dropdown",label:"field label",form:"form",p:"paragraph",span:"text",ul:"list",ol:"list",li:"list item",nav:"navigation",header:"header",footer:"footer",section:"container",article:"container",aside:"container",main:"container",div:"container"};
    var TEXT_TAGS={h1:1,h2:1,h3:1,h4:1,h5:1,h6:1,p:1,a:1,button:1,span:1,li:1,blockquote:1,label:1,strong:1,em:1,small:1,figcaption:1};
    var CARD_RE=/(^|[-_ ])(card|tile)([-_ ]|$)/i;
    function titleCase(s){return s?s.charAt(0).toUpperCase()+s.slice(1):s;}
    function humanize(s){var t=(s||"").replace(/[-_]/g," ").replace(/([a-z0-9])([A-Z])/g,"$1 $2").trim();return t?t.charAt(0).toUpperCase()+t.slice(1):(s||"");}
    function classTokens(el){return (el.getAttribute("class")||"").split(/\s+/).filter(function(t){return t&&t.indexOf("svelte-")!==0;});}
    function roleOf(el){return CARD_RE.test(el.getAttribute("class")||"")?"card":(TAG_ROLE[el.tagName.toLowerCase()]||null);}
    function textOf(el){var tag=el.tagName.toLowerCase();if(tag==="img"){var alt=(el.getAttribute("alt")||"").trim();return alt||null;}if(!TEXT_TAGS[tag])return null;var t=(el.textContent||"").trim();return t||null;}
    function ordinalOf(el,scope){var root=scope||el.ownerDocument;var tag=el.tagName.toLowerCase();var tokens=classTokens(el);var same=Array.prototype.slice.call(root.querySelectorAll(tag));var peers=tokens.length?same.filter(function(p){var pt=classTokens(p);return pt.some(function(t){return tokens.indexOf(t)>=0;});}):same;if(peers.length<=1)return null;var i=peers.indexOf(el);return i>=0?i+1:null;}
    function refOf(target,sectionEl){
      if(!target||target.nodeType!==1)return null;
      var secId=sectionEl?(sectionEl.getAttribute("data-paw-section")||""):"";
      var secLabel=secId?humanize(secId):"";
      var tag=target.tagName.toLowerCase();
      if(sectionEl===target){ if(!secId)return null; return {id:secId,tag:tag,role:null,label:secLabel,text:null,sectionId:secId,sectionLabel:secLabel,ordinal:null,level:"section",boundKind:null}; }
      var ordinal=ordinalOf(target,sectionEl||target.ownerDocument);
      var role=roleOf(target);
      var uid=target.getAttribute("data-uid");
      return {id:uid||(secId+":"+tag+":"+(ordinal||0)),tag:tag,role:role,label:titleCase(role||tag),text:textOf(target),sectionId:secId,sectionLabel:secLabel,ordinal:ordinal,level:"element",boundKind:null};
    }
    function post(type, sectionEl, leafEl, target) {
      var el = target ? refOf(target, sectionEl) : null;
      window.parent.postMessage({
        type: type,
        section: sectionEl ? sectionEl.getAttribute("data-paw-section") : null,
        rect: sectionEl ? boxOf(sectionEl) : (leafEl ? boxOf(leafEl) : null),
        uid: leafEl ? leafEl.getAttribute("data-uid") : null,
        uidRect: leafEl ? boxOf(leafEl) : null,
        element: el,
        elementRect: (el && target) ? boxOf(target) : null,
        nonce: NONCE
      }, ORIGIN);
    }
    document.addEventListener("mouseover", function (e) {
      var sec = ancestorWith(e.target, "data-paw-section");
      var leaf = ancestorWith(e.target, "data-uid");
      if (sec || leaf) post("paw-edit-hover", sec, leaf, e.target);
    }, true);
    document.addEventListener("click", function (e) {
      var sec = ancestorWith(e.target, "data-paw-section");
      var leaf = ancestorWith(e.target, "data-uid");
      if (sec || leaf) { e.preventDefault(); post("paw-edit-click", sec, leaf, e.target); }
    }, true);
  } catch (_) { /* bridge must never break a public page */ }
})();

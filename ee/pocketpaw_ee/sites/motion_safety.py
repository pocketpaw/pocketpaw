# ee/pocketpaw_ee/sites/motion_safety.py — what an LLM-written motion composition
# may contain, and what the renderer process may see.
#
# The motion engine (``motion_video.py``) renders HTML an LLM wrote, from a brief
# a user (or a prompt-injected page) wrote, in headless Chrome, and publishes the
# frames to a public bucket. Three layers keep that from becoming an SSRF or a
# credential leak:
#
#   1. NETWORK: ``vendor/hf-network-guard.cjs`` is preloaded into the CLI and
#      points Chrome at a dead proxy with only the CLI's own project file server
#      bypassed, and no DNS. THIS IS THE BOUNDARY; it holds even if 2 is fooled.
#   2. CONTENT (this module): BEST-EFFORT, not a sandbox. Static checks over JS
#      text cannot be complete, and html.parser and Chrome disagree at the edges
#      (raw-text elements inside <svg>, script escape states), so the scanner
#      rejects those edges outright instead of modelling them. Its job is to keep
#      honest compositions simple and catch obvious abuse early, with a clear
#      repair message. ``unsafe_constructs`` is an ALLOW-list check over a
#      real HTML parse. Only known tags; the only external script is the vendored
#      anime.js; the only URLs are bare filenames present in the temp project or
#      ``#fragment`` references; CSS cannot load anything; inline JS may not touch
#      network, navigation, storage, eval-likes, URL-bearing properties, or build
#      names by string concatenation.
#   3. ENVIRONMENT: ``renderer_env`` passes the CLI an allow-list of variables
#      (PATH, home/temp dirs, locale, HYPERFRAMES_*/PRODUCER_* minus the browser
#      path overrides), never the server's secrets.
#
# Anything this module rejects goes back to the LLM once as a repair problem.

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from html.parser import HTMLParser

_HTML_TAGS = {
    "html", "head", "meta", "title", "style", "script", "body", "div", "span",
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "section", "header", "main", "footer",
    "figure", "img", "em", "strong", "b", "i", "small", "br", "ul", "ol", "li",
}  # fmt: skip
_SVG_TAGS = {
    "svg", "g", "path", "circle", "ellipse", "rect", "line", "polyline", "polygon",
    "defs", "lineargradient", "radialgradient", "stop", "filter", "feturbulence",
    "fecolormatrix", "fegaussianblur", "feblend", "fecomposite", "feoffset",
    "fedisplacementmap", "femerge", "femergenode", "feflood", "mask", "clippath",
    "text", "tspan", "pattern", "desc",
}  # fmt: skip
ALLOWED_TAGS = frozenset(_HTML_TAGS | _SVG_TAGS)
_VOID_TAGS = frozenset({"meta", "img", "br"})
_SCRIPT_TEXT_RE = re.compile(r"(?i)<\s*/?\s*script")

#: What inline JS may create: never a script, style, meta or img.
_CREATABLE = ALLOWED_TAGS - {"script", "style", "meta", "html", "head", "body", "title", "img"}

#: Attributes that carry a URL. Only the cases ``_check_url_attr`` allows pass.
_URL_ATTRS = {
    "src", "href", "xlink:href", "srcset", "poster", "data", "action", "formaction",
    "background", "codebase", "cite", "longdesc", "manifest", "ping", "lowsrc",
    "dynsrc", "imagesrcset", "srcdoc", "archive", "classid", "profile", "usemap",
}  # fmt: skip

_XMLNS = {
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/1999/xlink",
    "http://www.w3.org/1999/xhtml",
}

_SCHEME_RE = re.compile(
    r"(?i)\b(?:https?|file|data|javascript|vbscript|ftp|wss?|blob|filesystem|chrome|"
    r"chrome-extension|about|mhtml|view-source|jar|ms-[a-z-]+)\s*:"
)
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CSS_URL_RE = re.compile(r"(?i)url\s*\(\s*(['\"]?)(.*?)\1\s*\)")

_CSS_BANNED = (
    (re.compile(r"(?i)@import\b"), "uses @import"),
    (re.compile(r"(?i)@font-face\b"), "declares @font-face (use a bundled family by name)"),
    (re.compile(r"(?i)\bimage-set\s*\(|\bsrc\s*\("), "loads an image-set/src() in CSS"),
    (re.compile(r"(?i)expression\s*\("), "uses a CSS expression()"),
    (re.compile(r"\\"), "uses a CSS escape (backslash)"),
)

_JS_BANNED: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p), why)
    for p, why in (
        (
            r"\bfetch\b|XMLHttpRequest|\bWebSocket\b|\bEventSource\b|sendBeacon",
            "makes a network request",
        ),
        (r"\bimport\s*\(|^\s*import\b|\bimportScripts\b", "imports a module"),
        (r"\b(?:Shared|Service)?Worker\b|serviceWorker", "starts a worker"),
        (r"\blocation\b|\bopen\s*\(|\bopener\b|\bpostMessage\b", "navigates or messages"),
        (r"\bcookie\b|localStorage|sessionStorage|indexedDB|\bcaches\b", "touches storage"),
        (r"\bnavigator\b", "reads navigator"),
        (r"\beval\b|\bFunction\s*\(|\bnew\s+Function\b|constructor", "evaluates code"),
        (r"Object\s*\.\s*assign|defineProperty|__proto__|prototype", "rewires objects"),
        (r"\.\s*(?:join|concat|repeat)\s*\(", "assembles strings at runtime (join/concat/repeat)"),
        (r"<!--|-->|<\s*/?\s*script", "contains markup or script-escape sequences"),
        (
            r"\bReflect\b|\bProxy\b|\bglobalThis\b|\bself\b|\bwindow\s*\[|\bdocument\s*\[",
            "reaches globals dynamically",
        ),
        (r"\bthis\s*\[", "reaches globals dynamically"),
        (
            r"\batob\b|\bbtoa\b|fromCharCode|fromCodePoint|\bunescape\b|decodeURI",
            "decodes strings at runtime",
        ),
        (r"\.(?:src|href|srcset|data|action|poster)\s*=", "assigns a URL-bearing property"),
        (
            r"innerHTML|outerHTML|insertAdjacentHTML|document\s*\.\s*write|createContextualFragment|DOMParser",
            "injects markup",
        ),
        (
            r"\bImage\s*\(|\bAudio\s*\(|\bBlob\b|createObjectURL|\bURL\s*\(",
            "creates a loadable resource",
        ),
        (
            r"cssText|setProperty|backgroundImage|borderImage|listStyleImage|\bmask(?:Image)?\b\s*[=:]",
            "sets a style that can load a URL",
        ),
        (r"(['\"`])\s*\+\s*['\"`]", "builds a string by concatenating literals"),
        (
            r"[\w$)\]]\[[^\]\[]*['\"`][^\]\[]*\+[^\]\[]*\]|[\w$)\]]\[[^\]\[]*\+[^\]\[]*['\"`][^\]\[]*\]",
            "accesses a property by concatenated name",
        ),
        (r"[\w$)\]]\[[^\]\[]*`[^\]\[]*\]", "accesses a property by template string"),
    )
)

_CREATE_RE = re.compile(r"createElement(?:NS)?\s*\(")
_CREATE_LITERAL_RE = re.compile(
    r"createElement(?:NS)?\s*\(\s*(?:(?:null|(['\"])[^'\"]*\1)\s*,\s*)?(['\"])([A-Za-z][\w-]*)\2\s*\)"
)
_SETATTR_RE = re.compile(r"setAttribute(?:NS)?\s*\(")
_SETATTR_LITERAL_RE = re.compile(
    r"setAttribute(?:NS)?\s*\(\s*(?:(?:null|(['\"])[^'\"]*\1)\s*,\s*)?(['\"])([A-Za-z][\w:-]*)\2\s*,"
)


class _Scanner(HTMLParser):
    def __init__(self, allowed_files: frozenset[str]) -> None:
        super().__init__(convert_charrefs=True)
        self.allowed_files = allowed_files
        self.problems: list[str] = []
        self.script_srcs: list[str] = []
        self.inline_js: list[str] = []
        self.css: list[str] = []
        self._raw: str | None = None
        self._stack: list[str] = []

    def _bad(self, message: str) -> None:
        if message not in self.problems:
            self.problems.append(message)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs, push=tag not in _VOID_TAGS)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs, push=False)

    def _open(self, tag: str, attrs: list[tuple[str, str | None]], *, push: bool) -> None:
        if tag not in ALLOWED_TAGS:
            self._bad(f"uses a <{tag}> element, which is not allowed")
            return
        # Chrome parses <svg><style>/<script> children as MARKUP, html.parser as
        # raw text, so the scanner would never see what Chrome builds.
        if tag in ("style", "script") and "svg" in self._stack:
            self._bad(f"puts a <{tag}> inside <svg>; keep styles and scripts outside it")
        if push:
            self._stack.append(tag)
        names = {name for name, _ in attrs}
        if tag == "meta" and not names <= {"charset", "name", "content"}:
            self._bad("uses a <meta> other than charset/viewport (no http-equiv)")
        if tag == "script":
            if not names <= {"src"}:
                self._bad("<script> may carry only src (no type/module/other attributes)")
            src = dict(attrs).get("src")
            if src is not None:
                self.script_srcs.append(src)
            else:
                self._raw = "script"
        if tag == "style":
            self._raw = "style"
        for name, value in attrs:
            self._check_attr(tag, name, value or "")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._raw = None
        if tag in self._stack:
            while self._stack and self._stack.pop() != tag:
                pass

    def handle_data(self, data: str) -> None:
        if self._raw == "script":
            self.inline_js.append(data)
        elif self._raw == "style":
            self.css.append(data)
        elif _SCRIPT_TEXT_RE.search(data):
            # Text that mentions a script tag means the parse and Chrome's
            # disagree about where a script ended.
            self._bad("has script markup in text content")

    def _check_attr(self, tag: str, name: str, value: str) -> None:
        if name.startswith("on"):
            self._bad(f"uses an event-handler attribute ({name})")
            return
        if name == "xmlns" or name.startswith("xmlns:"):
            if value.strip() not in _XMLNS:
                self._bad(f"declares an unknown namespace ({value[:60]})")
            return
        if name == "style":
            self.css.append(value)
            return
        if name in _URL_ATTRS:
            self._check_url_attr(tag, name, value.strip())
            return
        if _SCHEME_RE.search(value):
            self._bad(f"has a URL scheme in {name}= ({value[:60]})")
        for _q, target in _CSS_URL_RE.findall(value):
            if not target.strip().startswith("#"):
                self._bad(f"references url({target[:60]}) in {name}=; only url(#id) is allowed")

    def _check_url_attr(self, tag: str, name: str, value: str) -> None:
        if tag == "script" and name == "src":
            return  # judged as a whole in ``unsafe_constructs``
        if tag == "img" and name == "src":
            if _FILENAME_RE.match(value) and value in self.allowed_files:
                return
            self._bad(f"<img src> must be a provided local file, not {value[:60]!r}")
            return
        if name in ("href", "xlink:href") and value.startswith("#") and tag != "a":
            return
        self._bad(f"has a {name}= on <{tag}> ({value[:60]!r}); only local files are allowed")


def _css_problems(css: str, allowed_files: frozenset[str]) -> list[str]:
    problems = [f"the CSS {why}" for pattern, why in _CSS_BANNED if pattern.search(css)]
    if "<" in css:
        problems.append("the CSS contains '<' (markup inside a style)")
    if _SCHEME_RE.search(css):
        problems.append("the CSS contains a URL scheme")
    for _q, target in _CSS_URL_RE.findall(css):
        target = target.strip()
        if target.startswith("#") or (_FILENAME_RE.match(target) and target in allowed_files):
            continue
        problems.append(f"the CSS loads url({target[:60]}); only provided local files are allowed")
    return problems


def _js_problems(js: str) -> list[str]:
    problems = [f"the script {why}" for pattern, why in _JS_BANNED if pattern.search(js)]
    if _SCHEME_RE.search(js) or "//" in re.sub(r"(?m)^\s*//.*$|\s//.*$", "", js):
        problems.append("the script contains a URL")
    if len(_CREATE_RE.findall(js)) != len(_CREATE_LITERAL_RE.findall(js)) or any(
        m.group(3).lower() not in _CREATABLE for m in _CREATE_LITERAL_RE.finditer(js)
    ):
        problems.append("the script creates an element that is not an allowed literal tag")
    if len(_SETATTR_RE.findall(js)) != len(_SETATTR_LITERAL_RE.findall(js)) or any(
        (n := m.group(3).lower()).startswith("on") or n in _URL_ATTRS or n == "style"
        for m in _SETATTR_LITERAL_RE.finditer(js)
    ):
        problems.append("the script sets an attribute that is dynamic, an event, a URL or style")
    return problems


def unsafe_constructs(html: str, *, allowed_files: Iterable[str], script_src: str) -> list[str]:
    """Every construct outside the allow-list, as repair instructions. Empty
    means the document only uses what a motion composition needs."""
    files = frozenset(allowed_files)
    scanner = _Scanner(files)
    try:
        scanner.feed(html)
        scanner.close()
    except Exception as exc:  # noqa: BLE001 — unparseable markup is itself a problem
        return [f"the document could not be parsed: {exc}"]
    problems = list(scanner.problems)
    if scanner.script_srcs != [script_src]:
        problems.append(
            f'load exactly one external script, <script src="{script_src}"></script> '
            f"(found: {scanner.script_srcs or 'none'})"
        )
    for css in scanner.css:
        problems.extend(p for p in _css_problems(css, files) if p not in problems)
    for js in scanner.inline_js:
        problems.extend(p for p in _js_problems(js) if p not in problems)
    return problems


# ── The renderer's environment ───────────────────────────────────────────

_ENV_ALLOW = frozenset(
    {
        "PATH", "PATHEXT", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
        "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
        "LANG", "LC_ALL", "LC_CTYPE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
        "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
        "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "FONTCONFIG_PATH", "FONTCONFIG_FILE",
        "NPM_CONFIG_CACHE", "NPM_CONFIG_PREFIX",
    }
)  # fmt: skip
_ENV_PREFIXES = ("HYPERFRAMES_", "PRODUCER_")
#: Overrides that would let the environment pick which browser binary runs.
#: The CLI finds its pinned Chrome itself (the image bakes it into the cache).
_ENV_DENY = frozenset({"HYPERFRAMES_BROWSER_PATH", "PRODUCER_HEADLESS_SHELL_PATH"})


def renderer_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The allow-listed subset of ``source`` (default ``os.environ``). Names are
    compared upper-cased (Windows env is case-insensitive)."""
    env = os.environ if source is None else source
    return {
        k: v
        for k, v in env.items()
        if (k.upper() in _ENV_ALLOW or k.upper().startswith(_ENV_PREFIXES))
        and k.upper() not in _ENV_DENY
    }


__all__ = ["ALLOWED_TAGS", "renderer_env", "unsafe_constructs"]

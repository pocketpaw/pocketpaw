"""PocketPaw Interactive Installer.

Single-file installer with InquirerPy prompts for guided setup.
No local imports — designed to run standalone.

Changes:
  - 2026-02-17: Fix #184 — InquirerPy/prompt_toolkit raises OSError(22) on macOS
                when running via curl|sh. All prompts now fall back to plain text
                input on OSError. Disables InquirerPy globally after first failure.
  - 2026-02-13: Fix uv installing deps to wrong Python (--python sys.executable),
                verify imports after each cascade instead of blindly setting _HAS_RICH.

Usage:
    python installer.py                          # Interactive mode
    python installer.py --non-interactive --profile recommended  # Headless
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "0.4.1"
PACKAGE = "pocketpaw"
GIT_REPO = "https://github.com/pocketpaw/pocketpaw.git"
GIT_BRANCH = "main"
CONFIG_DIR = Path.home() / ".pocketpaw"
CONFIG_PATH = CONFIG_DIR / "config.json"

# ── InquirerPy / Rich Bootstrap ────────────────────────────────────────

_HAS_RICH = False
_HAS_INQUIRER = False


def _verify_imports(packages: list[str]) -> bool:
    """Verify packages are actually importable after install. Returns True if all found."""
    global _HAS_RICH, _HAS_INQUIRER
    all_ok = True
    for pkg in packages:
        spec_name = "rich" if pkg == "rich" else "InquirerPy"
        if importlib.util.find_spec(spec_name) is not None:
            if spec_name == "rich":
                _HAS_RICH = True
            else:
                _HAS_INQUIRER = True
        else:
            all_ok = False
    return all_ok


def _bootstrap_deps() -> None:
    """Install InquirerPy and rich if missing, with uv-first cascade."""
    global _HAS_RICH, _HAS_INQUIRER
    missing: list[str] = []
    if importlib.util.find_spec("rich") is None:
        missing.append("rich")
    else:
        _HAS_RICH = True
    if importlib.util.find_spec("InquirerPy") is None:
        missing.append("InquirerPy")
    else:
        _HAS_INQUIRER = True

    if not missing:
        return

    print(f"  Installing UI dependencies: {', '.join(missing)}...")

    # Cascade 1: uv pip install --system (target the running Python explicitly)
    if shutil.which("uv"):
        try:
            cmd = ["uv", "pip", "install", "-q", "--python", sys.executable] + missing
            if not _in_virtualenv():
                cmd.insert(3, "--system")
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            importlib.invalidate_caches()
            if _verify_imports(missing):
                return
        except Exception:
            pass

    # Cascade 2: python -m pip install --user
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--user", "-q"] + missing,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        importlib.invalidate_caches()
        if _verify_imports(missing):
            return
    except subprocess.CalledProcessError as exc:
        # Cascade 3: PEP 668 — retry with --break-system-packages
        stderr_text = exc.stderr.decode(errors="replace") if exc.stderr else ""
        if "externally-managed-environment" in stderr_text:
            try:
                subprocess.check_call(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--user",
                        "-q",
                        "--break-system-packages",
                    ]
                    + missing,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                importlib.invalidate_caches()
                if _verify_imports(missing):
                    return
            except Exception:
                pass
    except Exception:
        pass

    # Cascade 4: pip3 / pip on PATH
    for pip_bin in ("pip3", "pip"):
        if shutil.which(pip_bin):
            try:
                subprocess.check_call(
                    [pip_bin, "install", "--user", "-q"] + missing,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                importlib.invalidate_caches()
                if _verify_imports(missing):
                    return
            except Exception:
                pass

    print(f"  Warning: Could not install {', '.join(missing)}.")
    print("  Falling back to plain text prompts.\n")


def _in_virtualenv() -> bool:
    """Check if running inside a virtual environment."""
    return (
        hasattr(sys, "real_prefix")  # virtualenv
        or (sys.prefix != sys.base_prefix)  # venv
        or bool(os.environ.get("VIRTUAL_ENV"))
    )


_bootstrap_deps()

# Conditional imports — fallbacks defined below
if _HAS_RICH:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()
else:
    console = None  # type: ignore[assignment]

if _HAS_INQUIRER:
    from InquirerPy import inquirer
    from InquirerPy.separator import Separator
else:
    inquirer = None  # type: ignore[assignment]
    Separator = None  # type: ignore[assignment]


# ── Constants (must match pyproject.toml) ──────────────────────────────

PROFILES: dict[str, list[str]] = {
    "recommended": ["recommended"],
    "full": ["all"],
    "minimal": [],
    "custom": [],
}

FEATURE_GROUPS: dict[str, list[tuple[str, str]]] = {
    "Features": [
        ("dashboard", "Web Dashboard"),
        ("browser", "Browser Automation (Playwright)"),
        ("memory", "Semantic Memory (mem0)"),
        ("desktop", "Desktop Control"),
        ("native", "Open Interpreter Backend (Experimental)"),
    ],
    "Channels": [
        ("telegram", "Telegram"),
        ("discord", "Discord"),
        ("slack", "Slack"),
        ("whatsapp-personal", "WhatsApp Personal"),
        ("matrix", "Matrix"),
        ("teams", "Microsoft Teams"),
        ("gchat", "Google Chat"),
    ],
    "Tools": [
        ("image", "Image Generation"),
        ("extract", "URL Extraction"),
        ("voice", "Voice/TTS"),
        ("ocr", "OCR"),
        ("mcp", "MCP Protocol (Beta)"),
    ],
}

BACKENDS = {
    "claude_agent_sdk": "Claude Agent SDK (recommended)",
    "pocketpaw_native": "PocketPaw Native (Anthropic + Open Interpreter)",
    "open_interpreter": "Open Interpreter (Experimental — Ollama/OpenAI/Anthropic)",
}

LLM_PROVIDERS = {
    "anthropic": "Anthropic (Claude)",
    "openai": "OpenAI (GPT-4o)",
    "ollama": "Ollama (local, free)",
    "auto": "Auto-detect (tries Anthropic > OpenAI > Ollama)",
}

CHANNEL_TOKEN_MAP: dict[str, list[tuple[str, str, bool]]] = {
    # (config_key, display_name, is_secret)
    "telegram": [("telegram_bot_token", "Telegram Bot Token", True)],
    "discord": [("discord_bot_token", "Discord Bot Token", True)],
    "slack": [
        ("slack_bot_token", "Slack Bot Token (xoxb-...)", True),
        ("slack_app_token", "Slack App Token (xapp-...)", True),
    ],
    "matrix": [
        ("matrix_homeserver", "Matrix Homeserver URL", False),
        ("matrix_user_id", "Matrix User ID (@bot:matrix.org)", False),
        ("matrix_access_token", "Matrix Access Token", True),
    ],
    "teams": [
        ("teams_app_id", "Teams App ID", False),
        ("teams_app_password", "Teams App Password", True),
    ],
    "gchat": [
        ("gchat_service_account_key", "Service Account Key Path", False),
    ],
    "signal": [
        ("signal_phone_number", "Signal Phone Number (+1234567890)", False),
        ("signal_api_url", "Signal-cli REST API URL", False),
    ],
}


# ── Fallback prompts (plain input when InquirerPy unavailable) ─────────


def _plain_select(message: str, choices: list[dict[str, str]]) -> str:
    """Fallback select prompt using plain input()."""
    print(f"\n{message}")
    for i, c in enumerate(choices, 1):
        print(f"  {i}) {c['name']}")
    while True:
        try:
            idx = int(input("Enter number: ")) - 1
            if 0 <= idx < len(choices):
                return choices[idx]["value"]
        except (ValueError, EOFError):
            pass
        print("  Invalid choice, try again.")


def _plain_checkbox(message: str, choices: list) -> list[str]:
    """Fallback checkbox prompt using plain input()."""
    print(f"\n{message}")
    items = [c for c in choices if isinstance(c, dict)]
    for i, c in enumerate(items, 1):
        print(f"  {i}) {c['name']}")
    print("Enter numbers separated by commas (e.g. 1,3,5), or 'all':")
    raw = input("> ").strip()
    if raw.lower() == "all":
        return [c["value"] for c in items]
    selected = []
    for part in raw.split(","):
        try:
            idx = int(part.strip()) - 1
            if 0 <= idx < len(items):
                selected.append(items[idx]["value"])
        except ValueError:
            pass
    return selected


def _plain_secret(message: str) -> str:
    """Fallback secret prompt."""
    import getpass

    return getpass.getpass(f"{message}: ")


def _plain_text(message: str, default: str = "") -> str:
    """Fallback text prompt."""
    # Strip trailing colon — _safe_text callers include it for InquirerPy
    msg = message.rstrip(": ").rstrip(":")
    suffix = f" [{default}]" if default else ""
    val = input(f"{msg}{suffix}: ").strip()
    return val or default


def _plain_confirm(message: str, default: bool = True) -> bool:
    """Fallback confirm prompt."""
    suffix = " [Y/n]" if default else " [y/N]"
    val = input(f"{message}{suffix}: ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes")


# ── Safe InquirerPy wrappers (Issue #184) ─────────────────────────────
# On macOS, running via `curl | sh` with stdin redirected from /dev/tty
# causes prompt_toolkit (InquirerPy's backend) to raise OSError(22).
# EOFError can also occur if the terminal disconnects mid-prompt.
# These wrappers catch both and fall back to plain text prompts.
# After the first failure, _HAS_INQUIRER is set to False globally.


def _disable_inquirer(context: str = "") -> None:
    """Disable InquirerPy globally after a terminal error."""
    global _HAS_INQUIRER
    _HAS_INQUIRER = False
    if context:
        print(f"  Interactive prompts unavailable ({context}), using text input.\n")
    else:
        print("  Interactive prompts unavailable, using text input.\n")


def _safe_select(message: str, choices: list[dict[str, str]]) -> str:
    """Try InquirerPy select, fall back to plain text on failure."""
    if _HAS_INQUIRER and inquirer:
        try:
            return inquirer.select(message=message, choices=choices).execute()
        except (OSError, EOFError) as e:
            _disable_inquirer(str(e))
    return _plain_select(message, choices)


def _safe_checkbox(message: str, choices: list) -> list[str]:
    """Try InquirerPy checkbox, fall back to plain text on failure."""
    if _HAS_INQUIRER and inquirer and Separator:
        try:
            return inquirer.checkbox(message=message, choices=choices).execute()
        except (OSError, EOFError) as e:
            _disable_inquirer(str(e))
    return _plain_checkbox(message, choices)


def _safe_secret(message: str, default: str = "") -> str:
    """Try InquirerPy secret, fall back to plain text on failure."""
    if _HAS_INQUIRER and inquirer:
        try:
            return inquirer.secret(message=message, default=default).execute()
        except (OSError, EOFError) as e:
            _disable_inquirer(str(e))
    return _plain_secret(message)


def _safe_text(
    message: str,
    default: str = "",
    validate: object = None,
    invalid_message: str = "",
) -> str:
    """Try InquirerPy text, fall back to plain text on failure."""
    if _HAS_INQUIRER and inquirer:
        try:
            kwargs: dict = {"message": message, "default": default}
            if validate is not None:
                kwargs["validate"] = validate
            if invalid_message:
                kwargs["invalid_message"] = invalid_message
            return inquirer.text(**kwargs).execute()
        except (OSError, EOFError) as e:
            _disable_inquirer(str(e))
    return _plain_text(message, default)


def _safe_confirm(message: str, default: bool = True) -> bool:
    """Try InquirerPy confirm, fall back to plain text on failure."""
    if _HAS_INQUIRER and inquirer:
        try:
            return inquirer.confirm(message=message, default=default).execute()
        except (OSError, EOFError) as e:
            _disable_inquirer(str(e))
    return _plain_confirm(message, default)


# ── System Check ───────────────────────────────────────────────────────


@dataclass
class SystemInfo:
    os_name: str = ""
    os_version: str = ""
    python_version: str = ""
    python_path: str = ""
    pip_cmd: str = ""
    disk_free_gb: float = 0.0
    existing_version: str | None = None
    ok: bool = True
    errors: list[str] = field(default_factory=list)


class SystemCheck:
    """Detect and validate system requirements."""

    def __init__(self, pip_cmd: str = "") -> None:
        self.pip_cmd = pip_cmd

    def run_all(self) -> SystemInfo:
        info = SystemInfo()
        info.os_name = platform.system()
        info.os_version = platform.release()
        info.python_version = platform.python_version()
        info.python_path = sys.executable

        # Pip command
        info.pip_cmd = self.pip_cmd or self._detect_pip()
        if not info.pip_cmd:
            info.errors.append(
                "No package installer found. Install uv: "
                "curl -LsSf https://astral.sh/uv/install.sh | sh"
            )
            info.ok = False

        # Disk space
        try:
            usage = shutil.disk_usage(Path.home())
            info.disk_free_gb = usage.free / (1024**3)
        except OSError:
            info.disk_free_gb = -1

        # Check Python version
        if sys.version_info < (3, 11):  # noqa: UP036 — intentional runtime check
            info.errors.append(f"Python 3.11+ required, found {info.python_version}")
            info.ok = False

        # Check disk space (need at least 500MB)
        if 0 < info.disk_free_gb < 0.5:
            info.errors.append(f"Low disk space: {info.disk_free_gb:.1f} GB free")
            info.ok = False

        # Check for existing install
        info.existing_version = self._detect_existing()

        return info

    def _detect_pip(self) -> str:
        """Find best available pip command."""
        if shutil.which("uv"):
            return "uv pip"
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "--version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return f"{sys.executable} -m pip"
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        for cmd in ("pip3", "pip"):
            if shutil.which(cmd):
                return cmd
        # Last resort: try ensurepip bootstrap
        try:
            subprocess.check_call(
                [sys.executable, "-m", "ensurepip", "--upgrade"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return f"{sys.executable} -m pip"
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        return ""

    def _detect_existing(self) -> str | None:
        """Check if pocketpaw is already installed."""
        # Try uv pip show first (works when only uv is installed)
        if shutil.which("uv"):
            try:
                result = subprocess.run(
                    ["uv", "pip", "show", PACKAGE],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        if line.startswith("Version:"):
                            return line.split(":", 1)[1].strip()
            except Exception:
                pass
        # Try pip show
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "show", PACKAGE],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if line.startswith("Version:"):
                        return line.split(":", 1)[1].strip()
        except Exception:
            pass
        # Try importlib.metadata
        try:
            import importlib.metadata

            return importlib.metadata.version(PACKAGE)
        except Exception:
            pass
        return None

    def render(self, info: SystemInfo) -> None:
        """Display system info as a table or plain text."""
        if _HAS_RICH and console:
            table = Table(title="System Check", show_header=False, border_style="dim")
            table.add_column("Item", style="bold")
            table.add_column("Value")

            ok = "[green]OK[/green]"
            fail = "[red]FAIL[/red]"

            table.add_row("OS", f"{info.os_name} {info.os_version}")
            py_status = ok if sys.version_info >= (3, 11) else fail
            table.add_row("Python", f"{info.python_version} ({info.python_path})  {py_status}")
            table.add_row("Installer", info.pip_cmd)
            if info.disk_free_gb > 0:
                disk_status = ok if info.disk_free_gb >= 0.5 else fail
                table.add_row("Disk Free", f"{info.disk_free_gb:.1f} GB  {disk_status}")
            if info.existing_version:
                table.add_row("Installed", f"v{info.existing_version}")

            console.print(table)
            console.print()
        else:
            print(f"  OS:        {info.os_name} {info.os_version}")
            print(f"  Python:    {info.python_version} ({info.python_path})")
            print(f"  Installer: {info.pip_cmd}")
            if info.disk_free_gb > 0:
                print(f"  Disk Free: {info.disk_free_gb:.1f} GB")
            if info.existing_version:
                print(f"  Installed: v{info.existing_version}")
            print()

        if not info.ok:
            for err in info.errors:
                print(f"  ERROR: {err}")
            print()


# ── Installer UI ───────────────────────────────────────────────────────


class InstallerUI:
    """All interactive prompts."""

    def welcome_banner(self) -> None:
        if _HAS_RICH and console:
            banner = (
                "[bold magenta]🐾 PocketPaw[/bold magenta] "
                f"[dim]v{VERSION}[/dim] [yellow](Beta)[/yellow]\n\n"
                "[dim]The AI agent that runs on your laptop, not a datacenter.[/dim]\n"
                "[dim]Self-hosted · Privacy-first · Multi-channel[/dim]"
            )
            console.print(Panel(banner, border_style="magenta", padding=(1, 2)))
            console.print()
        else:
            print(f"\n  === PocketPaw v{VERSION} (Beta) ===")
            print("  The AI agent that runs on your laptop.\n")

    def prompt_upgrade(self, current_version: str) -> str:
        """Ask what to do with an existing installation."""
        choices = [
            {"name": "Upgrade to latest", "value": "upgrade"},
            {"name": "Reconfigure (keep version, re-run setup)", "value": "reconfigure"},
            {"name": "Add extras to existing install", "value": "add_extras"},
            {"name": "Reinstall from scratch", "value": "reinstall"},
            {"name": "Cancel", "value": "cancel"},
        ]
        msg = f"PocketPaw is already installed (v{current_version}). What would you like to do?"
        return _safe_select(msg, choices)

    def prompt_profile(self) -> str:
        """Select installation profile."""
        choices = [
            {
                "name": "Recommended (dashboard + browser + memory + desktop)",
                "value": "recommended",
            },
            {"name": "Full (everything including all channels)", "value": "full"},
            {"name": "Minimal (core only, add extras later)", "value": "minimal"},
            {"name": "Custom (pick individual features)", "value": "custom"},
        ]
        return _safe_select("Choose an installation profile:", choices)

    def prompt_custom_features(self) -> list[str]:
        """Pick individual features from grouped checkboxes."""
        choices: list = []
        for group_name, features in FEATURE_GROUPS.items():
            if _HAS_INQUIRER and Separator:
                choices.append(Separator(f"── {group_name} ──"))
            else:
                choices.append({"name": f"── {group_name} ──", "value": f"__sep_{group_name}"})
            for extra, label in features:
                choices.append({"name": label, "value": extra})

        return _safe_checkbox("Select features to install:", choices)

    def prompt_backend(self) -> str:
        """Select agent backend."""
        choices = [{"name": label, "value": key} for key, label in BACKENDS.items()]
        return _safe_select("Choose the agent backend:", choices)

    def prompt_llm_provider(self) -> str:
        """Select LLM provider."""
        choices = [{"name": label, "value": key} for key, label in LLM_PROVIDERS.items()]
        return _safe_select("Choose your LLM provider:", choices)

    def prompt_api_keys(self, provider: str) -> dict[str, str]:
        """Prompt for API keys based on selected provider."""
        keys: dict[str, str] = {}

        prompts: dict[str, list[tuple[str, str]]] = {
            "anthropic": [("anthropic_api_key", "Anthropic API Key (sk-ant-...)")],
            "openai": [("openai_api_key", "OpenAI API Key (sk-...)")],
            "ollama": [("ollama_host", "Ollama Host URL")],
            "auto": [
                ("anthropic_api_key", "Anthropic API Key (sk-ant-..., optional)"),
                ("openai_api_key", "OpenAI API Key (sk-..., optional)"),
            ],
        }

        for config_key, label in prompts.get(provider, []):
            is_secret = "api_key" in config_key.lower() or "password" in config_key.lower()
            if is_secret:
                val = _safe_secret(f"{label} (Enter to skip)")
            else:
                default = "http://localhost:11434" if "ollama_host" in config_key else ""
                val = _safe_text(f"{label}:", default)

            if val:
                keys[config_key] = val

        return keys

    def prompt_channel_tokens(self, selected_extras: list[str]) -> dict[str, str]:
        """Prompt for channel-specific tokens."""
        tokens: dict[str, str] = {}

        # Map extras to their channel keys
        extra_to_channel = {
            "telegram": "telegram",
            "discord": "discord",
            "slack": "slack",
            "whatsapp-personal": "whatsapp-personal",
            "matrix": "matrix",
            "teams": "teams",
            "gchat": "gchat",
            "signal": "signal",
        }

        for extra in selected_extras:
            channel = extra_to_channel.get(extra)
            if not channel or channel not in CHANNEL_TOKEN_MAP:
                continue

            # whatsapp-personal needs no tokens (QR pairing)
            if channel == "whatsapp-personal":
                continue

            fields = CHANNEL_TOKEN_MAP[channel]
            channel_display = extra.replace("-", " ").title()

            if _HAS_RICH and console:
                console.print(f"\n  [bold]{channel_display} Configuration[/bold]")
            else:
                print(f"\n  {channel_display} Configuration")

            for config_key, label, is_secret in fields:
                if is_secret:
                    val = _safe_secret(f"  {label} (Enter to skip)")
                else:
                    val = _safe_text(f"  {label} (Enter to skip):")

                if val:
                    tokens[config_key] = val

        return tokens

    def prompt_web_port(self) -> int:
        """Ask for web server port."""
        val = _safe_text(
            "Web dashboard port:",
            default="8888",
            validate=lambda v: v.isdigit() and 1024 <= int(v) <= 65535,
            invalid_message="Enter a port number between 1024 and 65535",
        )
        try:
            return int(val)
        except ValueError:
            return 8888

    def prompt_confirmation(self, summary: dict) -> bool:
        """Show summary and confirm."""
        if _HAS_RICH and console:
            table = Table(title="Installation Summary", show_header=False, border_style="cyan")
            table.add_column("Setting", style="bold")
            table.add_column("Value")

            table.add_row("Profile", summary.get("profile", "custom"))
            table.add_row("Extras", ", ".join(summary.get("extras", [])) or "none (core only)")
            table.add_row("Backend", summary.get("backend", "claude_agent_sdk"))
            table.add_row("LLM Provider", summary.get("llm_provider", "auto"))
            table.add_row("Web Port", str(summary.get("web_port", 8888)))

            api_keys_display = []
            for k in ("anthropic_api_key", "openai_api_key"):
                if summary.get("config", {}).get(k):
                    api_keys_display.append(k.replace("_api_key", "").title())
            table.add_row("API Keys", ", ".join(api_keys_display) or "none")

            channels_configured = []
            for extra in summary.get("extras", []):
                if extra in CHANNEL_TOKEN_MAP:
                    channels_configured.append(extra)
            if channels_configured:
                table.add_row("Channel Tokens", ", ".join(channels_configured))

            pip_cmd = summary.get("pip_cmd", "pip")
            install_cmd = self._build_install_display(pip_cmd, summary.get("extras", []))
            table.add_row("Install Command", install_cmd)

            console.print()
            console.print(table)
            console.print()
        else:
            print("\n  === Installation Summary ===")
            print(f"  Profile:     {summary.get('profile', 'custom')}")
            print(f"  Extras:      {', '.join(summary.get('extras', [])) or 'none'}")
            print(f"  Backend:     {summary.get('backend', 'claude_agent_sdk')}")
            print(f"  LLM:         {summary.get('llm_provider', 'auto')}")
            print(f"  Port:        {summary.get('web_port', 8888)}")
            print()

        return _safe_confirm("Proceed with installation?")

    def prompt_launch(self) -> bool:
        """Ask whether to launch PocketPaw after install."""
        return _safe_confirm("Launch PocketPaw now?")

    def _build_install_display(self, pip_cmd: str, extras: list[str]) -> str:
        if not extras:
            return f"{pip_cmd} install {PACKAGE}"
        return f"{pip_cmd} install '{PACKAGE}[{','.join(extras)}]'"


# ── Package Installer ──────────────────────────────────────────────────


class PackageInstaller:
    """Build and run pip/uv install commands."""

    def __init__(self, pip_cmd: str, from_git: bool = False) -> None:
        self.pip_cmd = pip_cmd
        self.from_git = from_git

    def _build_pkg_spec(self, extras: list[str]) -> str:
        """Build the package specifier, optionally pointing at git."""
        extras_suffix = f"[{','.join(extras)}]" if extras else ""
        if self.from_git:
            return f"{PACKAGE}{extras_suffix} @ git+{GIT_REPO}@{GIT_BRANCH}"
        return f"{PACKAGE}{extras_suffix}" if extras else PACKAGE

    def install(self, extras: list[str], upgrade: bool = False) -> bool:
        """Install pocketpaw with given extras. Returns True on success."""
        pkg = self._build_pkg_spec(extras)

        # Prefer `uv tool install` — isolated venv, no sudo, no PEP 668
        if shutil.which("uv"):
            ok = self._install_with_uv_tool(pkg, extras, upgrade)
            if ok:
                return True

        # Fallback: pip-style install
        cmd_parts = self.pip_cmd.split() + ["install"]
        if not _in_virtualenv():
            if "uv" in self.pip_cmd:
                cmd_parts.append("--system")
            else:
                cmd_parts.append("--user")
        if upgrade:
            cmd_parts.append("--upgrade")
        cmd_parts.append(pkg)

        if _HAS_RICH and console:
            with console.status(f"[bold cyan]Installing {pkg}...[/bold cyan]"):
                ok, stderr_text = self._run_cmd_capture(cmd_parts)
        else:
            print(f"  Installing {pkg}...")
            ok, stderr_text = self._run_cmd_capture(cmd_parts)

        if ok:
            return True

        # PEP 668 retry (pip only — uv doesn't need this)
        if "uv" not in self.pip_cmd and "externally-managed-environment" in stderr_text:
            return self._retry_with_pep668_workaround(cmd_parts, pkg)

        return False

    def _install_with_uv_tool(self, pkg: str, extras: list[str], upgrade: bool) -> bool:
        """Install using `uv tool install` — isolated venv in ~/.local/share/uv/tools/."""
        cmd = ["uv", "tool", "install"]
        if upgrade:
            cmd.append("--upgrade")
        else:
            cmd.append("--force")
        if self.from_git:
            # uv tool install needs --from for git sources
            extras_suffix = f"[{','.join(extras)}]" if extras else ""
            cmd.extend(["--from", f"{PACKAGE}{extras_suffix} @ git+{GIT_REPO}@{GIT_BRANCH}"])
            cmd.append(PACKAGE)
        else:
            cmd.append(pkg)

        if _HAS_RICH and console:
            with console.status(f"[bold cyan]Installing {pkg} (uv tool)...[/bold cyan]"):
                return self._run_cmd(cmd)
        else:
            print(f"  Installing {pkg} (uv tool)...")
            return self._run_cmd(cmd)

    def install_playwright(self) -> bool:
        """Install Playwright browsers."""
        if _HAS_RICH and console:
            with console.status("[bold cyan]Installing Playwright browsers...[/bold cyan]"):
                return self._run_cmd([sys.executable, "-m", "playwright", "install", "chromium"])
        else:
            print("  Installing Playwright browsers...")
            return self._run_cmd([sys.executable, "-m", "playwright", "install", "chromium"])

    def _run_cmd(self, cmd: list[str]) -> bool:
        """Run a command, return True on success."""
        ok, _ = self._run_cmd_capture(cmd)
        return ok

    def _run_cmd_capture(self, cmd: list[str]) -> tuple[bool, str]:
        """Run a command, return (success, stderr_text)."""
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode != 0:
                stderr_text = result.stderr or ""
                # Silently return for PEP 668 — caller will handle retry
                if "externally-managed-environment" in stderr_text:
                    return False, stderr_text
                print(f"\n  Command failed: {' '.join(cmd)}")
                if stderr_text:
                    lines = stderr_text.strip().splitlines()[-20:]
                    for line in lines:
                        print(f"    {line}")
                print()
                return False, stderr_text
            return True, ""
        except subprocess.TimeoutExpired:
            print("\n  Installation timed out (10 minutes). Try again with a better connection.\n")
            return False, ""
        except FileNotFoundError:
            print(f"\n  Command not found: {cmd[0]}")
            print(f"  Make sure {self.pip_cmd} is installed and on your PATH.\n")
            return False, ""

    def _retry_with_pep668_workaround(self, cmd_parts: list[str], pkg: str) -> bool:
        """Retry pip install with --break-system-packages for PEP 668 environments."""
        print("  Detected PEP 668 (externally-managed-environment), retrying...")
        retry_cmd = cmd_parts.copy()
        # Insert --break-system-packages after "install"
        try:
            idx = retry_cmd.index("install") + 1
        except ValueError:
            idx = len(retry_cmd)
        retry_cmd.insert(idx, "--break-system-packages")

        if _HAS_RICH and console:
            with console.status(f"[bold cyan]Retrying {pkg}...[/bold cyan]"):
                ok, _ = self._run_cmd_capture(retry_cmd)
        else:
            ok, _ = self._run_cmd_capture(retry_cmd)
        return ok


# ── Config Writer ──────────────────────────────────────────────────────


class ConfigWriter:
    """Write ~/.pocketpaw/config.json, merging with existing."""

    def write(self, config: dict) -> None:
        """Write config, preserving existing keys not in the new config."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        existing: dict = {}
        if CONFIG_PATH.exists():
            try:
                existing = json.loads(CONFIG_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        # Merge: new values override existing, but don't remove existing keys
        merged = {**existing, **config}

        # Remove empty/None values to keep config clean
        merged = {k: v for k, v in merged.items() if v is not None and v != ""}

        CONFIG_PATH.write_text(json.dumps(merged, indent=2) + "\n")

        # Set restrictive permissions (contains API keys)
        try:
            CONFIG_PATH.chmod(0o600)
        except OSError:
            pass  # Windows doesn't support Unix permissions

        # Also secure the directory
        try:
            CONFIG_DIR.chmod(0o700)
        except OSError:
            pass


# ── Main Orchestrator ──────────────────────────────────────────────────


class PocketPawInstaller:
    """Main installer orchestrating all steps."""

    def __init__(
        self, pip_cmd: str = "", non_interactive: bool = False, from_git: bool = False
    ) -> None:
        self.system = SystemCheck(pip_cmd)
        self.ui = InstallerUI()
        self.config_writer = ConfigWriter()
        self.non_interactive = non_interactive
        self.from_git = from_git

        # Collected state
        self.profile = "recommended"
        self.extras: list[str] = []
        self.backend = "claude_agent_sdk"
        self.llm_provider = "auto"
        self.web_port = 8888
        self.config: dict = {}
        self.pip_cmd = pip_cmd
        self.system_info: SystemInfo | None = None

    def run(self, args: argparse.Namespace) -> int:
        """Run the full installer flow. Returns exit code."""
        try:
            return self._run_inner(args)
        except KeyboardInterrupt:
            print("\n\n  Installation cancelled.\n")
            return 0
        except Exception as exc:
            print(f"\n  Unexpected error: {exc}\n")
            return 1

    def _run_inner(self, args: argparse.Namespace) -> int:
        # 1. Banner
        self.ui.welcome_banner()

        # 2. System check
        self.system_info = self.system.run_all()
        self.system.render(self.system_info)

        if not self.system_info.ok:
            print("  Cannot proceed due to system requirements. See errors above.\n")
            return 1

        self.pip_cmd = self.system_info.pip_cmd
        if args.pip_cmd:
            self.pip_cmd = args.pip_cmd

        # Non-interactive fast path
        if self.non_interactive:
            return self._run_non_interactive(args)

        # 2b. Upgrade detection
        if self.system_info.existing_version:
            action = self.ui.prompt_upgrade(self.system_info.existing_version)
            if action == "cancel":
                print("  Cancelled.\n")
                return 0
            if action == "upgrade":
                pkg_installer = PackageInstaller(self.pip_cmd)
                if not pkg_installer.install(["all"], upgrade=True):
                    return 1
                self._print_success()
                return 0
            # reconfigure / add_extras / reinstall all continue below

        # 3. Profile selection
        self.profile = self.ui.prompt_profile()

        # 4. Custom features (if applicable)
        if self.profile == "custom":
            self.extras = self.ui.prompt_custom_features()
            # Filter out separator values
            self.extras = [e for e in self.extras if not e.startswith("__sep_")]
        else:
            self.extras = list(PROFILES.get(self.profile, []))

        # 5. Backend selection
        self.backend = self.ui.prompt_backend()
        if self.backend == "pocketpaw_native" and "native" not in self.extras:
            self.extras.append("native")

        # 6. LLM provider
        self.llm_provider = self.ui.prompt_llm_provider()

        # 7. API keys
        api_keys = self.ui.prompt_api_keys(self.llm_provider)
        self.config.update(api_keys)

        # 8. Channel tokens
        channel_tokens = self.ui.prompt_channel_tokens(self.extras)
        self.config.update(channel_tokens)

        # 8b. Web port (if dashboard is included)
        has_dashboard = "dashboard" in self.extras or self.profile in ("recommended", "full")
        if has_dashboard:
            self.web_port = self.ui.prompt_web_port()

        # 9. Confirmation
        summary = {
            "profile": self.profile,
            "extras": self.extras,
            "backend": self.backend,
            "llm_provider": self.llm_provider,
            "web_port": self.web_port,
            "config": self.config,
            "pip_cmd": self.pip_cmd,
        }
        if not self.ui.prompt_confirmation(summary):
            print("  Installation cancelled.\n")
            return 0

        # 10. Install
        return self._do_install()

    def _run_non_interactive(self, args: argparse.Namespace) -> int:
        """Handle --non-interactive mode using CLI args."""
        self.profile = args.profile or "recommended"
        if args.extras:
            self.extras = [e.strip() for e in args.extras.split(",")]
        else:
            self.extras = list(PROFILES.get(self.profile, []))

        self.backend = args.backend or "claude_agent_sdk"
        self.llm_provider = args.llm_provider or "auto"
        self.web_port = args.web_port or 8888

        if args.anthropic_api_key:
            self.config["anthropic_api_key"] = args.anthropic_api_key
        if args.openai_api_key:
            self.config["openai_api_key"] = args.openai_api_key
        if args.ollama_host:
            self.config["ollama_host"] = args.ollama_host

        return self._do_install(launch=not args.no_launch)

    def _do_install(self, launch: bool | None = None) -> int:
        """Execute the installation."""
        pkg_installer = PackageInstaller(self.pip_cmd, from_git=self.from_git)

        # Install package
        upgrade = self.system_info is not None and self.system_info.existing_version is not None
        if not pkg_installer.install(self.extras, upgrade=upgrade):
            print("  Installation failed. Check the errors above.\n")
            print("  Suggestions:")
            print(f"    - Try: {self.pip_cmd} install --upgrade pip")
            print(f"    - Try: {self.pip_cmd} install {PACKAGE}")
            print("    - Check your internet connection\n")
            return 1

        # Install Playwright browsers if selected
        if "browser" in self.extras or self.profile in ("recommended", "full"):
            if not pkg_installer.install_playwright():
                print("  Warning: Playwright browsers not installed.")
                print("  Run later: python -m playwright install chromium\n")

        # Write config
        self.config["agent_backend"] = self.backend
        self.config["llm_provider"] = self.llm_provider
        self.config["web_port"] = self.web_port

        self.config_writer.write(self.config)

        # Success
        self._print_success()

        # Launch
        if launch is None:
            launch = self.ui.prompt_launch()
        if launch:
            self._launch()

        return 0

    def _print_success(self) -> None:
        """Print success message with next steps."""
        if _HAS_RICH and console:
            msg = (
                "[bold green]Installation complete![/bold green]\n\n"
                f"[dim]Config:[/dim] {CONFIG_PATH}\n"
                f"[dim]Data:[/dim]   {CONFIG_DIR}/\n\n"
                "[bold]Next steps:[/bold]\n"
                "  1. Run [cyan]pocketpaw[/cyan] to start the web dashboard\n"
                "  2. Run [cyan]pocketpaw --telegram[/cyan] for Telegram mode\n"
                "  3. Run [cyan]pocketpaw --help[/cyan] for all options\n"
                "  4. Run [cyan]pocketpaw --security-audit[/cyan] to check security"
            )
            console.print()
            console.print(Panel(msg, title="🐾 PocketPaw", border_style="green", padding=(1, 2)))
            console.print()
        else:
            print("\n  === Installation complete! ===")
            print(f"  Config: {CONFIG_PATH}")
            print(f"  Data:   {CONFIG_DIR}/")
            print()
            print("  Next steps:")
            print("    1. Run 'pocketpaw' to start the web dashboard")
            print("    2. Run 'pocketpaw --telegram' for Telegram mode")
            print("    3. Run 'pocketpaw --help' for all options")
            print("    4. Run 'pocketpaw --security-audit' to check security")
            print()

    def _launch(self) -> None:
        """Launch pocketpaw."""
        print("  Starting PocketPaw...\n")
        try:
            os.execvp("pocketpaw", ["pocketpaw"])
        except FileNotFoundError:
            # Might not be on PATH yet, try python -m
            try:
                os.execvp(sys.executable, [sys.executable, "-m", "pocketpaw"])
            except Exception as exc:
                print(f"  Could not launch: {exc}")
                print("  Try running 'pocketpaw' manually.\n")


# ── CLI Argument Parsing ───────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PocketPaw Interactive Installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--pip-cmd",
        default="",
        help="Override pip command (e.g. 'uv pip', 'pip3')",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip interactive prompts, use CLI args",
    )
    parser.add_argument(
        "--profile",
        choices=["recommended", "full", "minimal", "custom"],
        default=None,
        help="Installation profile (non-interactive)",
    )
    parser.add_argument(
        "--extras",
        default=None,
        help="Comma-separated extras to install (non-interactive)",
    )
    parser.add_argument(
        "--backend",
        choices=["claude_agent_sdk", "pocketpaw_native", "open_interpreter"],
        default=None,
        help="Agent backend (non-interactive)",
    )
    parser.add_argument(
        "--llm-provider",
        choices=["anthropic", "openai", "ollama", "auto"],
        default=None,
        help="LLM provider (non-interactive)",
    )
    parser.add_argument(
        "--anthropic-api-key",
        default=None,
        help="Anthropic API key (non-interactive)",
    )
    parser.add_argument(
        "--openai-api-key",
        default=None,
        help="OpenAI API key (non-interactive)",
    )
    parser.add_argument(
        "--ollama-host",
        default=None,
        help="Ollama host URL (non-interactive)",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=None,
        help="Web dashboard port (non-interactive)",
    )
    parser.add_argument(
        "--no-launch",
        action="store_true",
        help="Don't launch PocketPaw after install (non-interactive)",
    )
    parser.add_argument(
        "--uv-available",
        action="store_true",
        help="Hint that uv is on PATH (passed by install.sh)",
    )
    parser.add_argument(
        "--from-git",
        action="store_true",
        help="Install from the main branch on GitHub instead of PyPI",
    )
    return parser


# ── Entry Point ────────────────────────────────────────────────────────


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    installer = PocketPawInstaller(
        pip_cmd=args.pip_cmd,
        non_interactive=args.non_interactive,
        from_git=args.from_git,
    )
    exit_code = installer.run(args)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

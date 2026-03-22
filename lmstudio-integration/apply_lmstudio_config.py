import argparse
import json
import sys
from pathlib import Path


def config_path() -> Path:
    return Path.home() / ".pocketpaw" / "config.json"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Merge LM Studio defaults into PocketPaw config.json")
    parser.add_argument(
        "--host",
        default="http://127.0.0.1:1234",
        help="LM Studio server URL",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Model id (OpenAI /v1/models id). Empty = provider and host only",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print merged JSON, do not write",
    )
    args = parser.parse_args()
    host = args.host.rstrip("/")
    model = args.model.strip()
    dry = args.dry_run

    path = config_path()
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"Error reading {path}: {e}", file=sys.stderr)
            return 1

    patch = {
        "agent_backend": "claude_agent_sdk",
        "llm_provider": "lmstudio",
        "claude_sdk_provider": "lmstudio",
        "lmstudio_host": host,
    }
    if model:
        patch["lmstudio_model"] = model
        patch["claude_sdk_model"] = model

    merged = {**data, **patch}

    if dry:
        print(json.dumps(merged, indent=2, ensure_ascii=False))
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Updated {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

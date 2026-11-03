"""Write searxng/settings.yml from settings.example.yml with a random secret_key.

Run by the setup scripts. Does nothing when settings.yml already exists, so re-running
setup never changes the key of a running SearXNG. Standard library only.
"""
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "searxng" / "settings.example.yml"
TARGET = ROOT / "searxng" / "settings.yml"
PLACEHOLDER = "__GENERATED_BY_SETUP__"


def main() -> int:
    if TARGET.is_dir():
        # Docker creates a directory when it mounts a file that does not exist yet.
        print(f"{TARGET} is a directory (docker compose ran before setup). "
              "Remove it and run this again.", file=sys.stderr)
        return 1
    if TARGET.exists():
        print("searxng/settings.yml already exists, left as it is.")
        return 0
    text = EXAMPLE.read_text(encoding="utf-8")
    if PLACEHOLDER not in text:
        print(f"{EXAMPLE} has no {PLACEHOLDER} placeholder.", file=sys.stderr)
        return 1
    TARGET.write_text(text.replace(PLACEHOLDER, secrets.token_hex(32)), encoding="utf-8")
    print("searxng/settings.yml written with a new secret_key.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

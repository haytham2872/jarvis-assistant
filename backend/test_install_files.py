"""The files a new owner installs from stay true to the code.

- every setting the backend reads is documented in .env.example, and every setting
  documented there is read somewhere;
- a copied .env.example leaves every default intact: an uncommented line with an empty
  value is not the same as unset (OPENROUTER_STANDARD_MODEL= selected no model at all,
  found while writing docs/SETUP.md), so only settings that read "" as unset may be
  uncommented;
- no empty value carries a comment on its line: python-dotenv reads `KEY=   # note` as
  the value "# note" (found by the fresh-install test: the copied OPENROUTER_API_KEY was
  "# https://openrouter.ai/keys", so it counted as configured);
- the systemd template, the setup script and the SearXNG template carry what the
  install depends on.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"

# Read by the code but not a setting for an owner: set by the process itself, or only
# by tests and the phone app's development server.
_NOT_SETTINGS = {"TZ", "JARVIS_DB_SYNCHRONOUS", "MOBILE_DEV_ORIGINS", "MOBILE_DEV_PORT"}

# Settings that may be uncommented and empty in .env.example: each one reads "" as unset.
_EMPTY_IS_UNSET = {"OPENROUTER_API_KEY", "DISCORD_BOT_TOKEN", "DISCORD_ALLOWED_USER_ID",
                   "JARVIS_OWNER_NAME", "JARVIS_OWNER_PRONOUNS", "JARVIS_TIMEZONE",
                   "JARVIS_BASE_CURRENCY", "JARVIS_CONTACT_EMAIL"}

_READERS = re.compile(
    r"""(?:getenv|environ\.get|environ\[|environ\.setdefault|_setting|_env_float|api_key_env=)"""
    r"""\(?\s*["']([A-Z][A-Z0-9_]{2,})["']""")


def _backend_sources():
    for path in (ROOT / "backend").rglob("*.py"):
        name = path.name
        if name.startswith("test_") or name == "conftest.py" or "live_probe" in name:
            continue
        yield path.read_text(encoding="utf-8")


def _documented() -> dict[str, str | None]:
    """{name: value if the line is active, None if it is commented out}."""
    out: dict[str, str | None] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(#\s*)?([A-Z][A-Z0-9_]+)=([^#]*)", line)
        if m:
            out[m.group(2)] = None if m.group(1) else m.group(3).strip()
    return out


def test_every_setting_the_backend_reads_is_documented():
    read = set()
    for src in _backend_sources():
        read.update(_READERS.findall(src))
    missing = sorted(read - set(_documented()) - _NOT_SETTINGS)
    assert not missing, f"read by the backend but not in .env.example: {missing}"


def test_every_documented_setting_is_read_somewhere():
    sources = "\n".join(_backend_sources())
    stale = sorted(name for name in _documented() if f'"{name}"' not in sources
                   and f"'{name}'" not in sources)
    assert not stale, f"in .env.example but read nowhere in backend/: {stale}"


def test_only_settings_that_read_empty_as_unset_are_uncommented():
    active = {name: value for name, value in _documented().items() if value is not None}
    risky = sorted(name for name, value in active.items()
                   if value == "" and name not in _EMPTY_IS_UNSET)
    assert not risky, f"uncommented with an empty value (not the same as unset): {risky}"
    assert "OPENROUTER_API_KEY" in active and "DISCORD_BOT_TOKEN" in active


def test_no_empty_value_has_a_comment_on_its_line():
    """Commented-out lines too: removing the "# " in front of one must not set a comment."""
    bad = [line for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
           if re.match(r"^(#\s*)?[A-Z][A-Z0-9_]+=\s+#", line)]
    assert not bad, f"an empty value followed by a comment reads as that comment: {bad}"


def test_a_copied_env_example_keeps_every_default():
    """Load .env.example's active lines into a fresh process, as a new install would."""
    from dotenv import dotenv_values
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith(("OPENROUTER", "JARVIS_", "DISCORD_")) or k == "QDRANT_HOST")}
    values = dotenv_values(ENV_EXAMPLE)
    assert all(v == "" for v in values.values()), values
    env.update({k: v or "" for k, v in values.items()})
    env["PYTHONPATH"] = str(ROOT)
    code = ("from backend.brain import router, memory; from backend import owner, localtime;"
            "from backend.market import data;"
            "print(repr((router.OPENROUTER.base_url, router.OPENROUTER.standard,"
            " router.OPENROUTER.thinking, router.OPENROUTER_PROVIDER_PREFS is not None,"
            " router.JEV_MODEL, memory.USER_ID, owner.PRONOUNS, localtime.TIMEZONE, data.BASE,"
            " owner.NAME, router.OPENROUTER.configured)))")
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=ROOT / "backend",
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-1500:]
    base_url, standard, thinking, prefs, jev, user, pronouns, zone, currency, name, keyed = eval(
        out.stdout.strip().splitlines()[-1])
    assert base_url == "https://openrouter.ai/api/v1"
    assert standard and thinking and jev and user
    assert prefs is True
    assert (pronouns, zone, currency, name) == ("they", "UTC", "EUR", "")
    assert keyed is False


def test_mem0_sends_no_telemetry_unless_asked():
    """mem0 reports usage to its own analytics service by default; a new install must not."""
    env = {k: v for k, v in os.environ.items() if k != "MEM0_TELEMETRY"}
    env["PYTHONPATH"] = str(ROOT)
    code = ("import backend.brain.memory; from mem0.memory import telemetry;"
            "print(telemetry.MEM0_TELEMETRY)")
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=ROOT / "backend",
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-1500:]
    assert out.stdout.strip().splitlines()[-1] == "False"


def test_the_service_template_runs_as_a_user_and_starts_the_containers():
    unit = (ROOT / "deploy" / "jarvis.service").read_text(encoding="utf-8")
    assert "User=__USER__" in unit and "WorkingDirectory=__DIR__" in unit
    assert "ExecStart=__DIR__/.venv/bin/python -m backend.main --no-wake" in unit
    assert "ExecStartPre=+/usr/bin/docker compose -f __DIR__/mem0/docker-compose.yml up -d" in unit
    assert "TimeoutStopSec=" in unit


def test_setup_script_calls_only_files_that_exist():
    script = (ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")
    for rel in re.findall(r"\b((?:scripts|mem0)/[\w./-]+\.(?:py|sh|yml))", script):
        assert (ROOT / rel).exists(), f"setup.sh refers to a missing file: {rel}"
    assert "cp .env.example .env" in script and "fetch_models.py" in script


def test_searxng_template_holds_no_secret_and_the_real_file_is_ignored():
    example = (ROOT / "searxng" / "settings.example.yml").read_text(encoding="utf-8")
    assert 'secret_key: "__GENERATED_BY_SETUP__"' in example
    assert not re.search(r"secret_key:\s*\"[0-9a-f]{32,}\"", example)
    ignored = subprocess.run(["git", "check-ignore", "-q", "searxng/settings.yml"], cwd=ROOT)
    assert ignored.returncode == 0
    compose = (ROOT / "mem0" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "../searxng/settings.yml:/etc/searxng/settings.yml:ro" in compose


def test_container_images_are_pinned_and_qdrant_matches_its_client():
    """With `:latest`, a fresh install got qdrant-client 1.20 against a cached server
    1.17.1, and every start warned that the versions were incompatible. Two installs
    a month apart could run different databases from the same commit."""
    compose = (ROOT / "mem0" / "docker-compose.yml").read_text(encoding="utf-8")
    images = re.findall(r"^\s*image:\s*(\S+)", compose, re.MULTILINE)
    assert images, "no images found in mem0/docker-compose.yml"
    for image in images:
        assert ":" in image and not image.endswith(":latest"), f"unpinned image: {image}"
    server = re.search(r"image:\s*qdrant/qdrant:v(\d+\.\d+\.\d+)", compose).group(1)
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    client = re.search(r"^qdrant-client==(\d+\.\d+\.\d+)", requirements, re.MULTILINE).group(1)
    assert server.split(".")[:2] == client.split(".")[:2], (server, client)

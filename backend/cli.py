"""Terminal REPL client for JARVIS — connects to the backend WebSocket.

Usage: python backend/cli.py
       (or via: scripts/run.ps1 -NoUI)

Requires the .venv to be active. No extra dependencies beyond websockets,
which is already installed as part of the FastAPI/uvicorn stack.
"""
import asyncio
import json
import os
import sys
import urllib.request

import websockets

WS_PORT = os.getenv("WS_PORT", "8765")
WS_URL = f"ws://127.0.0.1:{WS_PORT}/ws"
SHUTDOWN_URL = f"http://127.0.0.1:{WS_PORT}/shutdown"

# ANSI escape codes — enabled on Windows 10+ via os.system("") below
RESET  = "\033[0m"
DIM    = "\033[2m"
BOLD   = "\033[1m"
CYAN   = "\033[96m"
YELLOW = "\033[33m"
GREEN  = "\033[92m"
RED    = "\033[91m"

_idle = asyncio.Event()   # set when backend state == idle
_stop = asyncio.Event()   # set on Ctrl+C / clean exit


def _call_shutdown() -> None:
    """POST /shutdown so the backend flushes its session before we exit.

    Consolidation (a model call + vault writes) can take up to ~25s on a slow
    connection — use a generous timeout so we don't interrupt it mid-write.
    """
    try:
        urllib.request.urlopen(SHUTDOWN_URL, data=b"", timeout=30)
    except Exception:
        pass


PURPLE = "\033[35m"

async def receive_loop(ws: websockets.WebSocketClientProtocol) -> None:
    """Print all incoming WebSocket messages to the terminal."""
    jarvis_open = False    # True while streaming a JARVIS response line
    thinking_open = False  # True while streaming thinking tokens
    plan_total = 0         # number of steps in current plan

    def _close_line():
        nonlocal jarvis_open, thinking_open
        if jarvis_open or thinking_open:
            sys.stdout.write("\n")
            sys.stdout.flush()
            jarvis_open = thinking_open = False

    try:
        async for raw in ws:
            msg = json.loads(raw)
            t = msg.get("type")

            if t == "state":
                state = msg.get("value", "")
                if state == "thinking":
                    _close_line()
                    plan_total = 0
                    sys.stdout.write(f"\n{DIM}[thinking...]{RESET}\n")
                    sys.stdout.flush()
                elif state == "idle":
                    _close_line()
                    _idle.set()

            elif t == "thinking":
                delta = msg.get("delta", "")
                if delta:
                    thinking_open = True
                    sys.stdout.write(f"{DIM}{delta}{RESET}")
                    sys.stdout.flush()

            elif t == "response":
                delta = msg.get("delta", "")
                if delta:
                    if thinking_open:
                        sys.stdout.write(f"\n{DIM}── ──{RESET}\n")
                        thinking_open = False
                    if not jarvis_open:
                        sys.stdout.write(f"\n{BOLD}JARVIS:{RESET} ")
                        jarvis_open = True
                    sys.stdout.write(delta)
                    sys.stdout.flush()

            elif t == "tool_call":
                tool = msg.get("tool", "?")
                inp = msg.get("input", {})
                detail = (
                    inp.get("query", inp.get("command", inp.get("text", str(inp))))
                    if isinstance(inp, dict) else str(inp)
                )
                _close_line()
                sys.stdout.write(f"{YELLOW}[{tool}: {detail}]{RESET}\n")
                sys.stdout.flush()

            elif t == "plan":
                steps = msg.get("steps", [])
                plan_total = len(steps)
                _close_line()
                sys.stdout.write(f"\n{PURPLE}[PLAN — {plan_total} steps]{RESET}\n")
                for s in steps:
                    tool_note = f" ({s.get('tool', 'no tool')})" if s.get("tool") else ""
                    pg_note   = f" ⚡" if s.get("parallel_group") else ""
                    sys.stdout.write(f"{DIM}  {s['id']}. {s['label']}{tool_note}{pg_note}{RESET}\n")
                sys.stdout.flush()

            elif t == "step_start":
                n = msg.get("n", "?")
                label = msg.get("label", f"Step {n}")
                _close_line()
                total_note = f"/{plan_total}" if plan_total else ""
                sys.stdout.write(f"{PURPLE}[step {n}{total_note}] {label}{RESET}\n")
                sys.stdout.flush()

            elif t == "step_done":
                n = msg.get("n", "?")
                success = msg.get("success", True)
                tools = msg.get("tools", [])
                icon = f"{GREEN}✓{RESET}" if success else f"{RED}✗{RESET}"
                tool_note = f" — {', '.join(tools)}" if tools else ""
                sys.stdout.write(f"{DIM}  {icon}{DIM} step {n}{tool_note}{RESET}\n")
                sys.stdout.flush()

            elif t == "plan_update":
                steps = msg.get("steps", [])
                _close_line()
                sys.stdout.write(f"{PURPLE}[PLAN REVISED — {len(steps)} remaining steps]{RESET}\n")
                for s in steps:
                    sys.stdout.write(f"{DIM}  {s['id']}. {s['label']}{RESET}\n")
                sys.stdout.flush()

            elif t == "error":
                _close_line()
                sys.stdout.write(f"\n{RED}[error: {msg.get('message', '?')}]{RESET}\n")
                sys.stdout.flush()
                _idle.set()

    except websockets.exceptions.ConnectionClosed:
        pass


async def input_loop(ws: websockets.WebSocketClientProtocol) -> None:
    """Wait for idle state, prompt the user, then send their input."""
    loop = asyncio.get_event_loop()

    while not _stop.is_set():
        await _idle.wait()
        if _stop.is_set():
            break
        _idle.clear()

        sys.stdout.write(f"\n{CYAN}You:{RESET} ")
        sys.stdout.flush()

        try:
            text = await loop.run_in_executor(None, sys.stdin.readline)
        except (EOFError, OSError):
            break

        text = text.strip()
        if not text:
            _idle.set()
            continue

        try:
            await ws.send(json.dumps({"type": "text_input", "text": text}))
        except websockets.exceptions.ConnectionClosed:
            break


async def run() -> None:
    """Connect with auto-retry, then run receive + input loops in parallel."""
    attempt = 0

    while not _stop.is_set():
        try:
            async with websockets.connect(
                WS_URL, ping_interval=20, ping_timeout=30
            ) as ws:
                if attempt > 0:
                    sys.stdout.write("\n")
                sys.stdout.write(
                    f"{GREEN}Connected to JARVIS.{RESET}"
                    " Type a message and press Enter.\n"
                )
                sys.stdout.flush()
                attempt = 0

                recv = asyncio.create_task(receive_loop(ws))
                send = asyncio.create_task(input_loop(ws))

                done, pending = await asyncio.wait(
                    [recv, send], return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()

                if _stop.is_set():
                    break

                sys.stdout.write(f"\n{DIM}Disconnected. Reconnecting...{RESET}")
                sys.stdout.flush()

        except (OSError, websockets.exceptions.WebSocketException):
            if attempt == 0:
                sys.stdout.write(f"{DIM}Waiting for JARVIS backend")
            sys.stdout.write(".")
            sys.stdout.flush()
            attempt += 1

        if not _stop.is_set():
            await asyncio.sleep(1)


def main() -> None:
    # Enable ANSI escape codes on Windows 10+ (no-op on other platforms)
    if sys.platform == "win32":
        os.system("")

    sys.stdout.write(f"{DIM}Connecting to {WS_URL}...{RESET}\n")
    sys.stdout.flush()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.stdout.write(f"\n{DIM}Shutting down JARVIS...{RESET}\n")
        sys.stdout.flush()
        _stop.set()
        _idle.set()
        _call_shutdown()
    finally:
        sys.stdout.write(f"{DIM}Done.{RESET}\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

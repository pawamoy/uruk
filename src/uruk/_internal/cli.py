"""Local commands for safely handing Uruk sessions back to a terminal."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from uruk._internal.bot import main as run_bot
from uruk._internal.config import Config
from uruk._internal.store import SessionStore, TaskInfo


def _control_socket_path(data_dir: Path) -> Path:
    return data_dir / "control.sock"


async def _request_release(data_dir: Path, topic_id: int) -> dict[str, object] | None:
    """Ask the running bot to finish a turn and stop owning a session."""
    try:
        reader, writer = await asyncio.open_unix_connection(_control_socket_path(data_dir))
    except (FileNotFoundError, ConnectionRefusedError):
        return None
    try:
        writer.write((json.dumps({"action": "resume", "topic_id": topic_id}) + "\n").encode())
        await writer.drain()
        response = json.loads((await reader.readline()).decode())
        return response if isinstance(response, dict) else {"ok": False, "error": "invalid bot response"}
    finally:
        writer.close()
        await writer.wait_closed()


def _choose_session(sessions: list[TaskInfo]) -> TaskInfo:
    print("Telegram-created sessions:")
    for index, info in enumerate(sessions, start=1):
        print(f"  {index}. {info.provider} · {Path(info.repo).name} · {info.title}")
    while True:
        try:
            value = input("Resume which session? [number, or q] ").strip().lower()
        except EOFError:
            raise SystemExit("uruk resume: no session selected") from None
        if value in {"q", "quit", ""}:
            raise SystemExit("uruk resume: no session selected")
        try:
            selected = sessions[int(value) - 1]
        except (ValueError, IndexError):
            print("Choose one of the listed numbers.")
        else:
            return selected


def resume_main() -> None:
    """Interactively claim a Telegram-created session and exec its native CLI."""
    data_dir = Config.data_dir_from_env()
    store = SessionStore(data_dir / "sessions.json")
    sessions = [
        info
        for info in store.all()
        if info.origin == "telegram" and info.session_id and info.owner == "telegram"
    ]
    if not sessions:
        raise SystemExit("uruk resume: no Telegram-owned sessions are ready to resume")
    sessions.sort(key=lambda info: info.title.casefold())
    info = _choose_session(sessions)

    response = asyncio.run(_request_release(data_dir, info.topic_id))
    if response is None:
        # With no bot process, no SDK client can still be running.  Persist the
        # ownership change before opening the terminal client.
        info.owner = "terminal"
        store.save()
    elif not response.get("ok"):
        raise SystemExit(f"uruk resume: {response.get('error', 'could not release session')}")

    os.chdir(info.repo)
    if info.provider == "claude":
        os.execvp("claude", ["claude", "--resume", info.session_id])
    if info.provider == "openai":
        os.execvp("codex", ["codex", "resume", info.session_id])
    raise SystemExit(f"uruk resume: unsupported provider {info.provider!r}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="uruk")
    parser.add_argument("command", nargs="?", choices=("resume",))
    args = parser.parse_args()
    if args.command is None:
        run_bot()
        return
    if args.command == "resume":
        resume_main()
        return

# SPDX-License-Identifier: ISC
#
# ISC License
#
# Copyright (c) 2026, Timothée Mazzucotelli and contributors
#
# Permission to use, copy, modify, and/or distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

# Why does this file exist, and why not put this in `__main__`?
#
# You might be tempted to import things from `__main__` later,
# but that will cause problems: the code will get executed twice:
#
# - When you run `python -m uruk` python will execute
#   `__main__.py` as a script. That means there won't be any
#   `uruk.__main__` in `sys.modules`.
# - When you import `__main__` it will get executed again (as a module) because
#   there's no `uruk.__main__` in `sys.modules`.

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from uruk._internal import debug
from uruk._internal.bot import main as run_bot
from uruk._internal.config import Config
from uruk._internal.store import SessionStore, TaskInfo


class _DebugInfo(argparse.Action):
    def __init__(self, nargs: int | str | None = 0, **kwargs: Any) -> None:
        super().__init__(nargs=nargs, **kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        debug._print_debug_info()
        sys.exit(0)


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
        info for info in store.all() if info.origin == "telegram" and info.session_id and info.owner == "telegram"
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
    session_id = info.session_id
    if session_id is None:
        raise SystemExit("uruk resume: session has no ID")
    if info.provider == "claude":
        os.execvp("claude", ["claude", "--resume", session_id])  # noqa: S606,S607
    if info.provider == "openai":
        os.execvp("codex", ["codex", "resume", session_id])  # noqa: S606,S607
    raise SystemExit(f"uruk resume: unsupported provider {info.provider!r}")


def get_parser() -> argparse.ArgumentParser:
    """Return the CLI argument parser.

    Returns:
        An argparse parser.
    """
    parser = argparse.ArgumentParser(prog="uruk")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {debug._get_version()}")
    parser.add_argument("--debug-info", action=_DebugInfo, help="Print debug information.")
    return parser


def main(args: list[str] | None = None) -> int:
    """Run the main program.

    This function is executed when you type `uruk` or `python -m uruk`.

    Parameters:
        args: Arguments passed from the command line.

    Returns:
        An exit code.
    """
    parser = get_parser()
    opts = parser.parse_args(args=args)
    if opts.command is None:
        run_bot()
    elif opts.command == "resume":
        resume_main()
    return 0

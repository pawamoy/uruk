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

# Shared agent interface and the Claude Agent SDK implementation.
#
# Each task owns one SDK session pinned to a repository, is fed by a queue of user
# prompts, and reports everything through a `UI` object.

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)
from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from uruk._internal.store import TaskInfo

# Tools whose (auto-approved) use is worth a one-line notice in the topic.
# Reads and searches stay silent; anything that needs approval shows up as a prompt anyway.
SHOWN_TOOLS = {"Bash", "Edit", "Write", "NotebookEdit"}


class UI(Protocol):
    """What the agent side needs from the Telegram side."""

    async def send_text(self, topic_id: int, text: str) -> None: ...
    async def send_activity(self, topic_id: int, text: str) -> None: ...
    async def send_typing(self, topic_id: int) -> None: ...
    async def ask_permission(self, topic_id: int, tool_name: str, input_data: dict) -> bool: ...
    async def ask_question(self, topic_id: int, question: dict) -> str: ...
    async def refresh_status(self, topic_id: int) -> None: ...


class AgentTask(Protocol):
    """Provider-neutral task interface used by the Telegram bot."""

    info: TaskInfo
    turn_running: bool

    @property
    def active(self) -> bool: ...

    async def submit(self, text: str) -> None: ...
    async def interrupt(self) -> bool: ...
    async def close(self) -> None: ...
    async def set_model(self, model: str | None) -> bool: ...
    async def set_effort(self, effort: str | None) -> None: ...


def summarize_tool_input(tool_name: str, input_data: dict[str, Any]) -> str:
    """A short human-readable rendering of a tool call's input."""
    input_data = input_data or {}
    if tool_name == "Bash":
        return input_data.get("command", "")
    if tool_name in {"Edit", "Write", "NotebookEdit", "Read"}:
        return input_data.get("file_path", "")
    if tool_name in {"WebFetch", "WebSearch"}:
        return input_data.get("url") or input_data.get("query", "")
    return json.dumps(input_data, indent=2, ensure_ascii=False, default=str)


class ClaudeAgentTask:
    """One live Claude session bound to a repository and a Telegram topic."""

    def __init__(
        self,
        ui: UI,
        info: TaskInfo,
        *,
        permission_mode: str,
        default_model: str | None,
        on_state_change: Callable[[], None],
    ) -> None:
        self.ui = ui
        self.info = info
        self.permission_mode = permission_mode
        self.default_model = default_model
        self.on_state_change = on_state_change
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.turn_running = False
        self._client: ClaudeSDKClient | None = None
        self._worker: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return self._worker is not None and not self._worker.done()

    async def submit(self, text: str) -> None:
        """Queue a user message; starts (or restarts, resuming) the session if needed."""
        if not self.active:
            self._worker = asyncio.create_task(self._run(), name=f"uruk-task-{self.info.topic_id}")
        await self.inbox.put(text)

    async def interrupt(self) -> bool:
        """Interrupt the current turn. Returns whether there was one to interrupt."""
        if self._client is not None and self.turn_running:
            await self._client.interrupt()
            return True
        return False

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker
            self._worker = None

    async def set_model(self, model: str | None) -> bool:
        """Change the model.

        Returns whether it was applied to a live session
        (otherwise it takes effect when the session next starts).
        """
        self.info.model = model
        # The old resolved model is stale now; the next session init reports the new one.
        self.info.resolved_model = None
        self.on_state_change()
        if self._client is not None:
            await self._client.set_model(model)
            return True
        return False

    async def set_effort(self, effort: str | None) -> None:
        """Change the effort level.

        The SDK has no runtime setter for effort, so the
        session is stopped; the next message restarts it (resuming the conversation)
        with the new value.
        """
        self.info.effort = effort
        self.on_state_change()
        if self.active:
            await self.close()

    async def _run(self) -> None:
        options = ClaudeAgentOptions(
            cwd=self.info.repo,
            permission_mode=self.permission_mode,  # ty: ignore[invalid-argument-type]
            can_use_tool=self._on_permission,
            model=self.info.model or self.default_model,
            effort=self.info.effort,  # ty: ignore[invalid-argument-type]
            resume=self.info.session_id,
            setting_sources=["user", "project", "local"],
        )
        try:
            async with ClaudeSDKClient(options=options) as client:
                self._client = client
                while True:
                    prompt = await self.inbox.get()
                    self.turn_running = True
                    typing = asyncio.create_task(self._typing_loop())
                    try:
                        await client.query(prompt)
                        async for message in client.receive_response():
                            await self._handle_message(message)
                    finally:
                        typing.cancel()
                        self.turn_running = False
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            logger.exception("session for topic {} crashed", self.info.topic_id)
            with contextlib.suppress(Exception):
                await self.ui.send_text(
                    self.info.topic_id,
                    f"💥 Session error: {error!r}\nSend a message to restart (the conversation resumes).",
                )
        finally:
            self._client = None
            self.turn_running = False

    async def _typing_loop(self) -> None:
        try:
            while True:
                with contextlib.suppress(Exception):
                    await self.ui.send_typing(self.info.topic_id)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            return

    async def _handle_message(self, message: object) -> None:
        if isinstance(message, SystemMessage) and message.subtype == "init":
            model = message.data.get("model")
            if model and model != self.info.resolved_model:
                self.info.resolved_model = model
                self.on_state_change()
                await self.ui.refresh_status(self.info.topic_id)
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    await self.ui.send_text(self.info.topic_id, block.text)
                elif isinstance(block, ToolUseBlock) and block.name in SHOWN_TOOLS:
                    summary = summarize_tool_input(block.name, block.input)
                    first_line = summary.splitlines()[0][:180] if summary else ""
                    await self.ui.send_activity(self.info.topic_id, f"⚙️ {block.name}: {first_line}")
        elif isinstance(message, ResultMessage):
            if message.session_id and message.session_id != self.info.session_id:
                self.info.session_id = message.session_id
                self.on_state_change()
            if message.is_error:
                await self.ui.send_activity(self.info.topic_id, f"⚠️ turn ended with error: {message.subtype}")

    async def _on_permission(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext,  # noqa: ARG002
    ) -> PermissionResultAllow | PermissionResultDeny:
        # The agent's clarifying questions route through the same permission channel.
        if tool_name == "AskUserQuestion":
            answers: dict[str, str] = {}
            for question in (input_data or {}).get("questions", []):
                answers[question["question"]] = await self.ui.ask_question(self.info.topic_id, question)
            return PermissionResultAllow(updated_input={**input_data, "answers": answers})

        allowed = await self.ui.ask_permission(self.info.topic_id, tool_name, input_data or {})
        if allowed:
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(message="Denied by the user from their phone.")

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

# Agent tasks backed by OpenAI's Codex SDK.

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from loguru import logger
from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    AsyncThread,
    AsyncTurnHandle,
    CodexConfig,
    Sandbox,
)
from openai_codex.types import Notification, ReasoningEffort

if TYPE_CHECKING:
    from collections.abc import Callable

    from uruk._internal.agent import UI
    from uruk._internal.store import TaskInfo


class CodexAgentTask:
    """One live Codex thread bound to a repository and a Telegram topic."""

    def __init__(
        self,
        ui: UI,
        info: TaskInfo,
        *,
        on_state_change: Callable[[], None],
    ) -> None:
        """Prepare a Codex task that saves state changes through `on_state_change`."""
        self.ui = ui
        """Interface used to send responses and activity notices."""
        self.info = info
        """Persistent session metadata."""
        self.on_state_change = on_state_change
        """Callback used to persist changes to session metadata."""
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        """Queue of prompts waiting to be processed."""
        self.turn_running = False
        """Whether the agent is currently processing a prompt."""
        self._codex: AsyncCodex | None = None
        self._thread: AsyncThread | None = None
        self._turn: AsyncTurnHandle | None = None
        self._worker: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        """Whether the session worker is running."""
        return self._worker is not None and not self._worker.done()

    async def submit(self, text: str) -> None:
        """Queue a user message, starting or resuming the Codex thread as needed."""
        if not self.active:
            self._worker = asyncio.create_task(
                self._run(),
                name=f"uruk-codex-task-{self.info.topic_id}",
            )
        await self.inbox.put(text)

    async def interrupt(self) -> bool:
        """Interrupt the current turn and return whether a turn was interrupted."""
        if self._turn is not None and self.turn_running:
            await self._turn.interrupt()
            return True
        return False

    async def close(self) -> None:
        """Stop the session worker while preserving the resumable conversation."""
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker
            self._worker = None

    async def set_model(self, model: str | None) -> bool:
        """Use the requested model from the next Codex turn onward."""
        self.info.model = model
        self.info.resolved_model = None
        self.on_state_change()
        return self._thread is not None

    async def set_effort(self, effort: str | None) -> None:
        """Use the requested reasoning effort from the next Codex turn onward."""
        self.info.effort = effort
        self.on_state_change()

    async def _run(self) -> None:
        try:
            async with AsyncCodex(CodexConfig(cwd=self.info.repo)) as codex:
                self._codex = codex
                self._thread = await self._open_thread(codex)
                if self._thread.id != self.info.session_id:
                    self.info.session_id = self._thread.id
                    self.on_state_change()

                while True:
                    prompt = await self.inbox.get()
                    self.turn_running = True
                    typing = asyncio.create_task(self._typing_loop())
                    try:
                        self._turn = await self._thread.turn(
                            prompt,
                            approval_mode=ApprovalMode.auto_review,
                            cwd=self.info.repo,
                            effort=self._effort(),
                            model=self.info.model,
                            sandbox=Sandbox.workspace_write,
                        )
                        async for event in self._turn.stream():
                            await self._handle_event(event)
                    finally:
                        typing.cancel()
                        self._turn = None
                        self.turn_running = False
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            logger.exception("Codex session for topic {} crashed", self.info.topic_id)
            with contextlib.suppress(Exception):
                await self.ui.send_text(
                    self.info.topic_id,
                    f"💥 Session error: {error!r}\nSend a message to restart (the conversation resumes).",
                )
        finally:
            self._turn = None
            self._thread = None
            self._codex = None
            self.turn_running = False

    async def _open_thread(self, codex: AsyncCodex) -> AsyncThread:
        if self.info.session_id:
            return await codex.thread_resume(
                self.info.session_id,
                approval_mode=ApprovalMode.auto_review,
                cwd=self.info.repo,
                model=self.info.model,
                sandbox=Sandbox.workspace_write,
            )
        return await codex.thread_start(
            approval_mode=ApprovalMode.auto_review,
            cwd=self.info.repo,
            model=self.info.model,
            sandbox=Sandbox.workspace_write,
        )

    def _effort(self) -> ReasoningEffort | None:
        return ReasoningEffort(self.info.effort) if self.info.effort else None

    async def _typing_loop(self) -> None:
        try:
            while True:
                with contextlib.suppress(Exception):
                    await self.ui.send_typing(self.info.topic_id)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            return

    async def _handle_event(self, event: Notification) -> None:
        payload = event.payload
        if event.method == "item/started":
            await self._handle_item_started(self._item(payload))
        elif event.method == "item/completed":
            await self._handle_item_completed(self._item(payload))
        elif event.method == "turn/completed":
            await self._handle_turn_completed(getattr(payload, "turn", None))

    @staticmethod
    def _item(payload: object) -> Any:
        item = getattr(payload, "item", None)
        return getattr(item, "root", item)

    async def _handle_item_started(self, item: Any) -> None:
        kind = getattr(item, "type", None)
        if kind == "commandExecution":
            command = getattr(item, "command", "")
            first_line = command.splitlines()[0][:180] if command else ""
            await self.ui.send_activity(self.info.topic_id, f"⚙️ Bash: {first_line}")
        elif kind == "mcpToolCall":
            name = f"{getattr(item, 'server', 'MCP')}.{getattr(item, 'tool', 'tool')}"
            await self.ui.send_activity(self.info.topic_id, f"⚙️ {name}")
        elif kind == "dynamicToolCall":
            await self.ui.send_activity(
                self.info.topic_id,
                f"⚙️ {getattr(item, 'tool', 'tool')}",
            )

    async def _handle_item_completed(self, item: Any) -> None:
        kind = getattr(item, "type", None)
        if kind == "agentMessage":
            text = getattr(item, "text", "")
            if text.strip():
                await self.ui.send_text(self.info.topic_id, text)
        elif kind == "fileChange":
            paths = [getattr(change, "path", "") for change in getattr(item, "changes", [])]
            summary = ", ".join(path for path in paths if path)[:180] or "files changed"
            await self.ui.send_activity(self.info.topic_id, f"⚙️ Edit: {summary}")

    async def _handle_turn_completed(self, turn: Any) -> None:
        if turn is None:
            return
        status = getattr(getattr(turn, "status", None), "value", None)
        if status == "interrupted":
            await self.ui.send_activity(self.info.topic_id, "⏹ interrupted")
        elif status == "failed":
            error = getattr(getattr(turn, "error", None), "message", None) or "unknown error"
            await self.ui.send_activity(self.info.topic_id, f"⚠️ turn failed: {error}")

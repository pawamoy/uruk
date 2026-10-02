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

# Telegram side: handlers, approval buttons, and routing between topics and agent sessions.

from __future__ import annotations

import asyncio
import contextlib
import html
import inspect
import itertools
import json
import logging
import secrets
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
from claude_agent_sdk import list_sessions
from insiders import Config as InsidersConfig
from insiders import GitHub, Issue, get_backlog
from insiders import Unset as InsidersUnset
from loguru import logger
from openai_codex import AsyncCodex, CodexConfig
from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegramify_markdown import telegramify
from telegramify_markdown.content import File, Photo, Text

from uruk._internal.agent import AgentTask, ClaudeAgentTask, _InteractionCancelledError, _summarize_tool_input
from uruk._internal.codex_agent import CodexAgentTask
from uruk._internal.config import Config, ConfigError
from uruk._internal.store import SessionStore, TaskInfo

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterator

_MESSAGE_LIMIT = 4000  # Telegram caps messages at 4096 chars; keep headroom.
_MIN_FILE_LINES = 30  # Code blocks longer than this become file attachments.
_MODEL_COMMANDS = {
    "fable": ("claude", "fable"),
    "opus": ("claude", "opus"),
    "sonnet": ("claude", "sonnet"),
    "haiku": ("claude", "haiku"),
    "sol": ("openai", "gpt-5.6-sol"),
    "terra": ("openai", "gpt-5.6-terra"),
    "luna": ("openai", "gpt-5.6-luna"),
}
_MODEL_LABELS = {model: command for command, (_, model) in _MODEL_COMMANDS.items()}
_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_EFFORT_PREFIXES = ("--", "–", "—")  # noqa: RUF001  # Telegram can replace hyphens with these dashes.
_AUTO_DEFAULT_LIMIT = 5
_AUTO_MAX_LIMIT = 20
_DISCOVERY_TIMEOUT = 120
_UPDATE_TIMEOUT = 30


def _parse_effort_flag(argument: str) -> str | None:
    """Return an effort value from an ASCII or Telegram-smart-dash flag."""
    for prefix in _EFFORT_PREFIXES:
        if argument.startswith(prefix):
            return argument.removeprefix(prefix).lower()
    return None


def _chunk(text: str, limit: int = _MESSAGE_LIMIT) -> Iterator[str]:
    """Split text into Telegram-sized pieces, preferring newline boundaries."""
    text = text.strip()
    while len(text) > limit:
        cut = text.rfind("\n", limit // 2, limit)
        if cut == -1:
            cut = limit
        yield text[:cut]
        text = text[cut:].lstrip("\n")
    if text:
        yield text


def _to_entities(entities: list) -> list[MessageEntity] | None:
    """Convert telegramify-markdown entities to python-telegram-bot ones.

    Telegram rejects the whole message if any link entity has a URL without a
    host (agents like to link local paths), so those links become plain text.
    """
    return [
        MessageEntity(
            type=entity.type,
            offset=entity.offset,
            length=entity.length,
            url=entity.url,
            language=entity.language,
            custom_emoji_id=entity.custom_emoji_id,
        )
        for entity in entities
        if entity.url is None or urlparse(entity.url).netloc
    ] or None


@dataclass
class _Pending:
    """An interaction waiting for a button press."""

    kind: str  # "perm", "question", or "auto"
    future: asyncio.Future
    topic_id: int | None
    options: list[str] = field(default_factory=list)
    multi: bool = False
    selected: set[int] = field(default_factory=set)
    payloads: list[object] = field(default_factory=list)


@dataclass(frozen=True)
class _AttachCandidate:
    """A provider session that is not yet tracked by Uruk."""

    provider: str
    session_id: str
    repo: str
    title: str
    status: str = "idle"


class UrukBot:
    """Owns the Telegram application, the agent tasks, and the pending interactions."""

    def __init__(self, config: Config) -> None:
        """Load stored sessions and prepare the bot with the given configuration."""
        self.config = config
        """Telegram settings, agent defaults, and state paths."""
        self.store = SessionStore(config.data_dir / "sessions.json")
        """Persistent session state."""
        interrupted_handoffs = [info for info in self.store.all() if info.owner == "transferring"]
        for info in interrupted_handoffs:
            info.owner = "telegram"
        if interrupted_handoffs:
            self.store.save()
        self.tasks: dict[int, AgentTask] = {}
        """Live agent tasks indexed by Telegram topic ID."""
        self.pending: dict[int, _Pending] = {}
        """Interactions waiting for the user to press a button."""
        self.awaiting_text: dict[int | None, asyncio.Future] = {}
        """Topics waiting for a typed answer to a question."""
        self.prompt_dialogues: dict[int | None, asyncio.Task] = {}
        """Background dialogues used to collect prompts for new sessions."""
        self.auto_runs: dict[int | None, asyncio.Task] = {}
        """Background tasks working through backlog issues."""
        self._attach_runs: dict[int | None, asyncio.Task] = {}
        self._release_runs: dict[int | None, asyncio.Task] = {}
        self._shutting_down = False
        self.control_server: asyncio.AbstractServer | None = None
        """Local server used to hand sessions to terminal clients."""
        self.backlog: list[Issue] = []  # Fetched once, in memory, until a full review cycle ends.
        """Issues fetched for the current backlog review cycle."""
        self.backlog_started: set[str] = set()
        """Issue keys started during the current review cycle."""
        self.backlog_skipped: set[str] = set()
        """Issue keys skipped during the current review cycle."""
        # Buttons from a previous process must not answer a new interaction.
        self._pid = itertools.count(secrets.randbits(48))
        self.app: Application | None = None
        """Attached Telegram application, or `None` before attachment."""

    # -- wiring ------------------------------------------------------------------

    def attach(self, app: Application) -> None:
        """Attach the Telegram application and register its command and message handlers."""
        self.app = app
        for commands, callback in (
            ("id", self._cmd_id),
            ("start", self._cmd_help),
            ("help", self._cmd_help),
            ("repos", self._cmd_repos),
            ("list", self._cmd_list),
            ("attach", self._cmd_attach),
            ("release", self._cmd_release),
            ("interrupt", self._cmd_interrupt),
            ("cancel", self._cmd_cancel),
            ("close", self._cmd_close),
            ("purge", self._cmd_purge),
            ("auto", self._cmd_auto),
            ("model", self._cmd_model),
            ("effort", self._cmd_effort),
            (tuple(_MODEL_COMMANDS), self._cmd_new_model),
        ):
            app.add_handler(CommandHandler(commands, self._bounded_handler(callback)))
        app.add_handler(CallbackQueryHandler(self._bounded_handler(self._on_button)))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._bounded_handler(self._on_text)))
        app.add_handler(MessageHandler(filters.COMMAND, self._bounded_handler(self._on_unknown_command)))
        app.add_handler(MessageHandler(filters.ALL & ~filters.TEXT & ~filters.StatusUpdate.ALL, self._bounded_handler(self._on_non_text)))
        # Handler groups are processed in order. This deliberately comes after the
        # command handlers so the panel follows each command's response.
        app.add_handler(MessageHandler(filters.COMMAND, self._bounded_handler(self._show_command_panel)), group=1)

    def _bounded_handler(self, callback: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]:
        """Keep a stalled command or SDK call from holding the update dispatcher."""
        async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            try:
                async with asyncio.timeout(_UPDATE_TIMEOUT):
                    await callback(update, context)
            except TimeoutError:
                logger.warning("Telegram handler timed out")
                if self._auth(update):
                    await self._send(self._message(update).message_thread_id, "⌛ That action timed out. You can retry it, or use /cancel to stop a waiting flow.")
            except Exception:  # noqa: BLE001
                logger.exception("Telegram handler failed")
                if self._auth(update):
                    await self._send(self._message(update).message_thread_id, "⚠️ That action failed. You can retry it; see the bot logs for details.")

        return handle

    def _application(self) -> Application:
        """Return the Telegram application after it has been attached."""
        if self.app is None:
            raise RuntimeError("Telegram application is not attached")
        return self.app

    async def shutdown(self, app: Application) -> None:  # noqa: ARG002
        """Cancel background work, close agent sessions, and remove the control socket."""
        self._shutting_down = True
        flows = [task for mapping in self._flow_maps() for task in mapping.values()]
        for task in flows:
            task.cancel()
        if flows:
            await asyncio.gather(*flows, return_exceptions=True)
        for mapping in self._flow_maps():
            mapping.clear()
        for topic_id in {pending.topic_id for pending in self.pending.values()} | set(self.awaiting_text):
            self._cancel_interactions(topic_id)
        for task in list(self.tasks.values()):
            await task.close()
        if self.control_server is not None:
            self.control_server.close()
            await self.control_server.wait_closed()
            self.control_server = None
        with contextlib.suppress(OSError):
            self._control_socket_path().unlink()

    @staticmethod
    def _topic_key(topic_id: int | None) -> int | None:
        """Use one key for both forms of Telegram's General topic."""
        return None if topic_id == 1 else topic_id

    def _flow_maps(self) -> tuple[dict[int | None, asyncio.Task], ...]:
        return self.prompt_dialogues, self.auto_runs, self._attach_runs, self._release_runs

    def _flow_running(self, topic_id: int | None) -> bool:
        topic_id = self._topic_key(topic_id)
        return any((task := mapping.get(topic_id)) is not None and not task.done() for mapping in self._flow_maps())

    def _start_flow(self, mapping: dict[int | None, asyncio.Task], topic_id: int | None, operation: Callable[[], Awaitable[None]]) -> None:
        """Run a dialogue without blocking Telegram's update dispatcher."""
        topic_id = self._topic_key(topic_id)

        async def run() -> None:
            try:
                await operation()
            except TimeoutError:
                await self._send(topic_id, "⌛ This flow timed out. Start it again when you are ready.")
            except _InteractionCancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception("conversation flow in topic {} failed", topic_id)
                await self._send(topic_id, "⚠️ This flow failed. Start it again; see the bot logs for details.")
            finally:
                if mapping.get(topic_id) is asyncio.current_task():
                    mapping.pop(topic_id, None)
                if topic_id is None and not self._shutting_down:
                    await self._maybe_show_panel(topic_id)

        mapping[topic_id] = asyncio.create_task(run())

    def _cancel_interactions(self, topic_id: int | None) -> bool:
        """Cancel all button and text waits in one topic."""
        topic_id = self._topic_key(topic_id)
        cancelled = False
        for pid, pending in list(self.pending.items()):
            if pending.topic_id == topic_id:
                self.pending.pop(pid, None)
                if not pending.future.done():
                    if pending.kind == "perm":
                        pending.future.set_result(False)
                    else:
                        pending.future.set_exception(_InteractionCancelledError("Telegram interaction cancelled"))
                cancelled = True
        if (future := self.awaiting_text.pop(topic_id, None)) is not None:
            if not future.done():
                future.set_exception(_InteractionCancelledError("Telegram interaction cancelled"))
            cancelled = True
        return cancelled

    async def _wait_for_reply(self, pid: int, pending: _Pending, text: str, *, expires_after: float | None, **kwargs: Any) -> Any:
        """Wait for a reply with the caller's timeout and clean up on every exit."""
        pending.topic_id = self._topic_key(pending.topic_id)
        self.pending[pid] = pending
        message = None
        try:
            async with asyncio.timeout(expires_after):
                message = await self._send(pending.topic_id, text, **kwargs)
                if message is None:
                    raise RuntimeError("could not send the interaction to Telegram")
                return await pending.future
        finally:
            self.pending.pop(pid, None)
            if self.awaiting_text.get(pending.topic_id) is pending.future:
                self.awaiting_text.pop(pending.topic_id, None)
            if not pending.future.done():
                pending.future.cancel()
            elif not pending.future.cancelled():
                pending.future.exception()
            if message is not None:
                with contextlib.suppress(TelegramError):
                    await message.edit_reply_markup(None)

    def _control_socket_path(self) -> Path:
        """The local-only control socket used by ``uruk resume``."""
        return self.config.data_dir / "control.sock"

    async def start_control_server(self, app: Application) -> None:  # noqa: ARG002
        """Accept local requests to hand a Telegram session to a terminal.

        This keeps the ownership change in the bot process: it can wait for the
        current turn to finish before its SDK client is closed.  The Unix socket
        is deliberately local-only and lives beside the session state.
        """
        path = self._control_socket_path()
        with contextlib.suppress(OSError):
            path.unlink()
        self.control_server = await asyncio.start_unix_server(self._on_control_connection, path)

    async def _on_control_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        response: dict[str, object]
        try:
            raw = await asyncio.wait_for(reader.readline(), _DISCOVERY_TIMEOUT)
            request = json.loads(raw)
            if request.get("action") != "resume" or not isinstance(request.get("topic_id"), int):
                raise ValueError("unknown request")  # noqa: TRY301
            await self._release_to_terminal(request["topic_id"], telegram_origin_only=True)
            response = {"ok": True}
        except Exception as error:  # noqa: BLE001
            logger.warning("local control request failed: {}", error)
            response = {"ok": False, "error": str(error)}
        writer.write((json.dumps(response) + "\n").encode())
        with contextlib.suppress(Exception):
            await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    async def _release_to_terminal(self, topic_id: int, *, telegram_origin_only: bool = False) -> None:
        """Wait for a Telegram turn, then stop its client and release the session."""
        info = self.store.get(topic_id)
        if info is None:
            raise RuntimeError("session is no longer tracked by Uruk")
        if telegram_origin_only and info.origin != "telegram":
            raise RuntimeError("only Telegram-created sessions are available through uruk resume")
        if info.owner == "terminal":
            return
        if info.owner == "transferring":
            raise RuntimeError("a handoff is already in progress")
        if any(pending.topic_id == topic_id for pending in self.pending.values()):
            raise RuntimeError("answer the pending Telegram question or approval before handing off")

        info.owner = "transferring"
        self.store.save()
        try:
            async with asyncio.timeout(self.config.interaction_timeout):
                task = self._get_task(topic_id)
                if task is not None:
                    if task.turn_running:
                        await self._send(topic_id, "⏳ Finishing this turn before releasing it to your terminal… Use /cancel to stop the handoff.")
                    while task.turn_running:  # noqa: ASYNC110
                        await asyncio.sleep(0.25)
                    # Close the SDK client between turns, after the last turn finishes.
                    await task.close()
                    self.tasks.pop(topic_id, None)
                info.owner = "terminal"
                self.store.save()
        finally:
            if info.owner == "transferring":
                info.owner = "telegram"
                self.store.save()
        await self._send(
            topic_id,
            "💻 Released to your terminal. This topic is now read-only until you attach it again.",
        )

    def _authorized(self, user_id: int | None, chat_id: int | None) -> bool:
        if not self.config.configured:
            return False
        if chat_id == self.config.chat_id and user_id == self.config.owner_id:
            return True
        logger.warning("ignoring update from chat={} user={}", chat_id, user_id)
        return False

    def _auth(self, update: Update) -> bool:
        return self._authorized(
            update.effective_user.id if update.effective_user else None,
            update.effective_chat.id if update.effective_chat else None,
        )

    @staticmethod
    def _message(update: Update) -> Message:
        """Return the message supplied to a message or command handler."""
        message = update.effective_message
        if message is None:
            raise ValueError("update has no message")
        return message

    @staticmethod
    def _query(update: Update) -> CallbackQuery:
        """Return the query supplied to a callback query handler."""
        query = update.callback_query
        if query is None:
            raise ValueError("update has no callback query")
        return query

    @staticmethod
    def _is_main_thread(message: Message) -> bool:
        """Whether a message is in a chat's main/General thread.

        Telegram identifies the General forum topic as thread 1. Non-forum chats
        do not have a thread id, which is also their main thread.
        """
        return message.message_thread_id in (None, 1)

    def _get_task(self, topic_id: int) -> AgentTask | None:
        """Return the task for a topic, reviving it from the store if needed."""
        task = self.tasks.get(topic_id)
        if task is not None:
            return task
        info = self.store.get(topic_id)
        if info is None:
            return None
        task = self._make_task(info)
        self.tasks[topic_id] = task
        return task

    def _make_task(self, info: TaskInfo) -> AgentTask:
        if info.provider == "openai":
            return CodexAgentTask(
                ui=self,
                info=info,
                on_state_change=self.store.save,
            )
        return ClaudeAgentTask(
            ui=self,
            info=info,
            permission_mode=self.config.permission_mode,
            default_model=self.config.model,
            on_state_change=self.store.save,
        )

    # -- UI protocol (called from AgentTask) --------------------------------------

    async def send_text(self, topic_id: int, text: str) -> None:
        """Send an agent response with Telegram formatting and file attachments."""
        try:
            boxes = await telegramify(text, max_message_length=_MESSAGE_LIMIT, min_file_lines=_MIN_FILE_LINES)
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning("telegramify failed for topic {}, sending plain text", topic_id)
            for part in _chunk(text):
                await self._send(topic_id, part)
            return
        for box in boxes:
            try:
                await self._send_box(topic_id, box)
            except TelegramError:
                logger.opt(exception=True).warning("sending {} to topic {} failed", type(box).__name__, topic_id)
                if isinstance(box, Text):
                    await self._send(topic_id, box.text)

    async def _send_box(self, topic_id: int, box: Text | File | Photo) -> None:
        if isinstance(box, Text):
            await self._application().bot.send_message(
                chat_id=self.config.chat_id,
                text=box.text,
                entities=_to_entities(box.entities),
                message_thread_id=topic_id,
            )
        elif isinstance(box, File):
            await self._application().bot.send_document(
                chat_id=self.config.chat_id,
                document=box.file_data,
                filename=box.file_name,
                caption=box.caption_text or None,
                caption_entities=_to_entities(box.caption_entities),
                message_thread_id=topic_id,
            )
        else:  # Photo (e.g. rendered mermaid diagrams)
            await self._application().bot.send_photo(
                chat_id=self.config.chat_id,
                photo=box.file_data,
                caption=box.caption_text or None,
                caption_entities=_to_entities(box.caption_entities),
                message_thread_id=topic_id,
            )

    async def send_activity(self, topic_id: int, text: str) -> None:
        """Send an activity notice in italics to a topic."""
        await self._send(topic_id, f"<i>{html.escape(text)}</i>", parse_mode=ParseMode.HTML)

    async def send_typing(self, topic_id: int) -> None:
        """Show a typing indicator in a topic."""
        await self._application().bot.send_chat_action(
            chat_id=self.config.chat_id,
            action=ChatAction.TYPING,
            message_thread_id=topic_id,
        )

    async def ask_permission(self, topic_id: int, tool_name: str, input_data: dict) -> bool:
        """Show Allow and Deny buttons and wait indefinitely for the decision."""
        pid = next(self._pid)
        pending = _Pending(kind="perm", future=asyncio.get_running_loop().create_future(), topic_id=topic_id)
        summary = _summarize_tool_input(tool_name, input_data)[:1500]
        text = f"🔧 <b>{html.escape(tool_name)}</b>"
        if summary:
            text += f"\n<pre>{html.escape(summary)}</pre>"
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Allow", callback_data=f"p:{pid}:a"),
                    InlineKeyboardButton("❌ Deny", callback_data=f"p:{pid}:d"),
                ],
            ],
        )
        return await self._wait_for_reply(pid, pending, text, expires_after=None, parse_mode=ParseMode.HTML, reply_markup=markup)

    async def ask_question(self, topic_id: int | None, question: dict) -> str:
        """Wait for a selected or typed answer, expiring only setup questions in General."""
        pid = next(self._pid)
        options = [option["label"] for option in question.get("options", [])]
        pending = _Pending(
            kind="question",
            future=asyncio.get_running_loop().create_future(),
            topic_id=topic_id,
            options=options,
            multi=bool(question.get("multiSelect")),
        )
        lines = [f"❓ <b>{html.escape(question.get('header', 'Question'))}</b>", html.escape(question["question"])]
        for option in question.get("options", []):
            description = option.get("description", "")
            lines.append(f"• <b>{html.escape(option['label'])}</b> — {html.escape(description)}")
        return await self._wait_for_reply(
            pid,
            pending,
            "\n".join(lines),
            expires_after=self.config.interaction_timeout if self._topic_key(topic_id) is None else None,
            parse_mode=ParseMode.HTML,
            reply_markup=self._question_markup(pid, pending),
        )

    def _question_markup(self, pid: int, pending: _Pending) -> InlineKeyboardMarkup:
        rows = []
        for index, label in enumerate(pending.options):
            mark = "✅ " if index in pending.selected else ""
            rows.append([InlineKeyboardButton(f"{mark}{label}"[:60], callback_data=f"q:{pid}:{index}")])
        if pending.multi:
            rows.append([InlineKeyboardButton("☑️ Done", callback_data=f"q:{pid}:done")])
        rows.append([InlineKeyboardButton("✍️ Other…", callback_data=f"q:{pid}:other")])
        return InlineKeyboardMarkup(rows)

    async def _send(self, topic_id: int | None, text: str, **kwargs: Any) -> Message | None:
        try:
            return await self._application().bot.send_message(
                chat_id=self.config.chat_id,
                text=text,
                message_thread_id=self._topic_key(topic_id),
                **kwargs,
            )
        except TelegramError:
            logger.exception("failed to send message to topic {}", topic_id)
            return None

    # -- button presses ------------------------------------------------------------

    async def _on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = self._query(update)
        if not self._authorized(query.from_user.id, query.message.chat.id if query.message else None):
            await query.answer()
            return
        if not isinstance(query.data, str):
            await query.answer("Unknown action.")
            return
        if query.data.startswith("c:"):
            await self._handle_command_button(update, context)
            return
        try:
            kind, pid_str, arg = query.data.split(":", 2)
            pid = int(pid_str)
        except ValueError:
            await query.answer("Unknown action.")
            return
        pending = self.pending.get(pid)
        if pending is None or pending.future.done():
            await query.answer("Expired.")
            with contextlib.suppress(TelegramError):
                await query.edit_message_reply_markup(None)
            return

        expected_kind = {"perm": "p", "question": "q", "attach": "x", "auto": "a"}.get(pending.kind)
        if kind != expected_kind or not isinstance(query.message, Message) or self._topic_key(query.message.message_thread_id) != pending.topic_id:
            await query.answer("Unknown action.")
            return
        if self.awaiting_text.get(pending.topic_id) is pending.future:
            await query.answer("Type your answer, or use /cancel.")
            return

        if kind == "p":
            if arg not in {"a", "d"}:
                await query.answer("Unknown action.")
                return
            allowed = arg == "a"
            del self.pending[pid]
            pending.future.set_result(allowed)
            await query.answer("Allowed" if allowed else "Denied")
            await self._finalize(query, "✅ Allowed" if allowed else "❌ Denied")
            return

        if kind == "q":
            if arg == "other":
                if pending.topic_id in self.awaiting_text:
                    await query.answer("Answer the current text question first.")
                    return
                self.awaiting_text[pending.topic_id] = pending.future
                await query.answer()
                await self._finalize(query, "✍️ Type your answer as a message in this topic.")
            elif arg == "done":
                if not pending.multi:
                    await query.answer("Unknown action.")
                    return
                labels = [pending.options[index] for index in sorted(pending.selected)]
                answer = ", ".join(labels) if labels else "(none)"
                del self.pending[pid]
                pending.future.set_result(answer)
                await query.answer()
                await self._finalize(query, f"→ {answer}")
            else:
                try:
                    index = int(arg)
                except ValueError:
                    await query.answer("Unknown option.")
                    return
                if not 0 <= index < len(pending.options):
                    await query.answer("Unknown option.")
                    return
                if pending.multi:
                    pending.selected.symmetric_difference_update({index})
                    await query.answer()
                    with contextlib.suppress(TelegramError):
                        await query.edit_message_reply_markup(self._question_markup(pid, pending))
                else:
                    answer = pending.options[index]
                    del self.pending[pid]
                    pending.future.set_result(answer)
                    await query.answer()
                    await self._finalize(query, f"→ {answer}")

        if kind == "x":
            try:
                index = int(arg)
                if index < 0:
                    raise IndexError  # noqa: TRY301
                candidate = pending.payloads[index]
            except (IndexError, ValueError):
                await query.answer("Unknown session.")
                return
            del self.pending[pid]
            pending.future.set_result(candidate)
            await query.answer()
            await self._finalize(query, "→ Selected")
            return

        if kind == "a":
            if arg != "skip" and arg not in _MODEL_COMMANDS:
                await query.answer("Unknown model.")
                return
            del self.pending[pid]
            pending.future.set_result(None if arg == "skip" else arg)
            await query.answer()
            await self._finalize(query, "⏭ Skipped" if arg == "skip" else f"→ {arg}")
            return

    async def _handle_command_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Dispatch a shortcut from the persistent command panel."""
        query = self._query(update)
        message = query.message
        if not isinstance(message, Message) or not self._is_main_thread(message):
            await query.answer("Use the command panel in General.")
            return
        if not isinstance(query.data, str):
            await query.answer("Unknown action.")
            return
        command = query.data.removeprefix("c:")
        handlers = {
            "auto": self._cmd_auto,
            "attach": self._cmd_attach,
            "purge": self._cmd_purge,
            "repos": self._cmd_repos,
            "list": self._cmd_list,
            "help": self._cmd_help,
            "id": self._cmd_id,
        }
        if command == "prompt":
            topic_id = self._topic_key(message.message_thread_id)
            if self._flow_running(topic_id):
                await query.answer("Finish the current flow or use /cancel first.")
                return
            self._start_flow(self.prompt_dialogues, topic_id, lambda: self._run_prompt_dialogue(message, topic_id))
            await query.answer("Prompt setup started")
            return
        handler = handlers.get(command)
        if handler is None:
            await query.answer("Unknown action.")
            return
        await query.answer()
        await handler(update, context)
        await self._maybe_show_panel(message.message_thread_id)

    async def _run_prompt_dialogue(self, message: Any, topic_id: int | None) -> None:
        """Collect model, effort, repository, and prompt from the command panel."""
        model_command = await self.ask_question(
            topic_id,
            {
                "header": "Choose a model",
                "question": "Which model should run this task?",
                "options": [
                    {"label": command, "description": f"{provider.title()} · {model}"}
                    for command, (provider, model) in _MODEL_COMMANDS.items()
                ],
            },
        )
        if model_command not in _MODEL_COMMANDS:
            await self._send(topic_id, "That model is unavailable. Start Prompt again.")
            return
        effort_choice = await self.ask_question(
            topic_id,
            {
                "header": "Reasoning effort",
                "question": "How much reasoning effort should it use?",
                "options": [
                    {"label": "default", "description": "Use the model's default."},
                    *[{"label": effort, "description": ""} for effort in _EFFORT_LEVELS],
                ],
            },
        )
        if effort_choice not in {*_EFFORT_LEVELS, "default"}:
            await self._send(topic_id, "That effort level is unavailable. Start Prompt again.")
            return
        repo = await self._choose_repo_for_prompt(topic_id)
        if repo is None:
            return
        prompt = await self._request_dialogue_text(topic_id, "✍️ Send the prompt for this task.")
        provider, model = _MODEL_COMMANDS[model_command]
        await self._start_session(
            message,
            provider,
            model,
            None if effort_choice == "default" else effort_choice,
            str(repo),
            prompt.strip(),
        )

    async def _choose_repo_for_prompt(self, topic_id: int | None) -> Path | None:
        """Search immediate repositories under the configured root, then offer matches."""
        if self.config.repos_root is None:
            path = await self._request_dialogue_text(
                topic_id,
                "✍️ URUK_REPOS_ROOT is not set. Send an absolute repository path.",
            )
            repo = self._resolve_repo(path.strip())
            if repo is None:
                await self._send(topic_id, "Not a directory. Start Prompt again.")
            return repo

        repos = sorted(
            (path for path in self.config.repos_root.iterdir() if path.is_dir() and not path.name.startswith(".")),
            key=lambda path: path.name.casefold(),
        )
        while True:
            search = await self._request_dialogue_text(
                topic_id,
                "🔎 Send part of a repository name to filter it (or an absolute path).",
            )
            search = search.strip()
            direct = self._resolve_repo(search)
            if direct is not None and Path(search).is_absolute():
                return direct
            matches = [path for path in repos if search.casefold() in path.name.casefold()]
            if not matches:
                await self._send(topic_id, "No repositories match that. Try another filter.")
                continue
            if len(matches) > 30:  # noqa: PLR2004
                await self._send(topic_id, f"{len(matches)} repositories match. Please make the filter more specific.")
                continue
            chosen = await self.ask_question(
                topic_id,
                {
                    "header": "Choose a repository",
                    "question": f"{len(matches)} match{'es' if len(matches) != 1 else ''} for “{search}”.",
                    "options": [{"label": path.name, "description": str(path)} for path in matches],
                },
            )
            for path in matches:
                if path.name == chosen:
                    return path
            # “Other…” provides a convenient way to refine the search.
            direct = self._resolve_repo(chosen.strip())
            if direct is not None:
                return direct
            await self._send(topic_id, "That is not one of the matches. Try another filter.")

    async def _request_dialogue_text(self, topic_id: int | None, text: str) -> str:
        """Ask for one text response in a command-panel dialogue."""
        topic_id = self._topic_key(topic_id)
        if topic_id in self.awaiting_text:
            raise RuntimeError(f"topic {topic_id} is already awaiting text")
        future = asyncio.get_running_loop().create_future()
        self.awaiting_text[topic_id] = future
        try:
            async with asyncio.timeout(self.config.interaction_timeout):
                if await self._send(topic_id, text) is None:
                    raise RuntimeError("could not send the text prompt to Telegram")
                return await future
        finally:
            if self.awaiting_text.get(topic_id) is future:
                self.awaiting_text.pop(topic_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def _finalize(self, query: Any, suffix: str) -> None:
        """Append the outcome to the prompt message and drop its buttons."""
        with contextlib.suppress(TelegramError, AttributeError):
            await query.edit_message_text(
                f"{query.message.text_html}\n\n{suffix}",
                parse_mode=ParseMode.HTML,
            )

    # -- messages ------------------------------------------------------------------

    async def _on_unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: ARG002
        if self._auth(update):
            await self._message(update).reply_text("Unknown command. Use /help for commands, or /cancel to stop the current flow.")

    async def _on_non_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: ARG002
        if self._auth(update):
            await self._message(update).reply_text("Please send text or choose one of the buttons. Use /cancel to stop the current flow.")

    async def _on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: ARG002
        if not self._auth(update):
            return
        message = self._message(update)
        topic_id = self._topic_key(message.message_thread_id)
        if message.text is None:
            return

        if topic_id in self.awaiting_text:
            future = self.awaiting_text.pop(topic_id)
            if not future.done():
                if not message.text.strip():
                    self.awaiting_text[topic_id] = future
                    await message.reply_text("Please send a non-empty answer, or use /cancel.")
                    return
                future.set_result(message.text)
                with contextlib.suppress(TelegramError):
                    await message.set_reaction("✍")
                return

        for pending in self.pending.values():
            if pending.topic_id == topic_id and not pending.future.done():
                await message.reply_text("Choose one of the buttons. For a question, use Other… to type an answer. Use /cancel to stop waiting.")
                return

        if topic_id is None:
            await message.reply_text(
                "Send messages inside a task topic, or start one with /<model> [--<effort>] <repo> <task>.",
            )
            return

        task = self._get_task(topic_id)
        if task is None:
            await message.reply_text(
                "No session is attached to this topic. Start one with a model command in the General topic.",
            )
            return
        if task.info.owner != "telegram":
            await message.reply_text(
                "This session is checked out to a terminal. Attach it again from General before sending it Telegram messages.",
            )
            return
        await task.submit(message.text)
        with contextlib.suppress(TelegramError):
            await message.set_reaction("👍")

    # -- commands ------------------------------------------------------------------

    async def _show_command_panel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: ARG002
        """Keep a shortcut panel in the chat's main (General) thread."""
        if not self._auth(update):
            return
        message = self._message(update)
        if not self._is_main_thread(message):
            return
        await self._maybe_show_panel(message.message_thread_id)

    async def _maybe_show_panel(self, topic_id: int | None) -> None:
        """Repost the panel, unless a flow in this topic will repost it when it finishes."""
        if self._flow_running(topic_id):
            return
        await self._send_panel(topic_id)

    async def _send_panel(self, topic_id: int | None) -> None:
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🎯 Prompt", callback_data="c:prompt"),
                    InlineKeyboardButton("⚡ Auto", callback_data="c:auto"),
                ],
                [
                    InlineKeyboardButton("📲 Attach", callback_data="c:attach"),
                    InlineKeyboardButton("🗑 Purge", callback_data="c:purge"),
                    InlineKeyboardButton("📁 Repos", callback_data="c:repos"),
                ],
                [
                    InlineKeyboardButton("📋 Sessions", callback_data="c:list"),
                    InlineKeyboardButton("❓ Help", callback_data="c:help"),
                ],
                [
                    InlineKeyboardButton("🆔 IDs", callback_data="c:id"),
                ],
            ],
        )
        # The Bot API rejects the General topic's thread id (1) on plain sends.
        await self._send(None if topic_id == 1 else topic_id, "Command panel", reply_markup=markup)

    async def _cmd_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: ARG002
        chat = update.effective_chat
        user = update.effective_user
        if chat is None or user is None:
            return
        await self._message(update).reply_text(
            f"chat_id: {chat.id}\n"
            f"user_id: {user.id}\n"
            f"chat type: {chat.type}\n"
            f"forum (topics enabled): {bool(chat.is_forum)}",
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: ARG002
        if not self._auth(update):
            return
        await self._message(update).reply_text(
            "/<fable|opus|sonnet|haiku|sol|terra|luna> [--<effort>] <repo> [task…] — start a session\n"
            "/repos — list repositories\n"
            "/list — list sessions\n"
            "/attach — attach an idle local Claude or Codex session (in General)\n"
            "/release — wait for this turn, then release it to a terminal (in a task topic)\n"
            "/auto [N] — offer the next N unreviewed backlog items as new tasks (in General); "
            "the backlog is fetched once and cycles when fully reviewed\n"
            "/interrupt — interrupt the current turn (in a task topic)\n"
            "/cancel — cancel the current setup, question, approval, or handoff\n"
            "/close — end the session and close the topic (in a task topic)\n"
            "/purge — delete topics closed with /close, and forget sessions whose topics were deleted by hand\n"
            "/model [name|default] — show or change this task's model (in a task topic)\n"
            "/effort [low|medium|high|xhigh|max|default] — show or change this task's effort (in a task topic)\n"
            "Any text inside a Telegram-owned task topic goes to that session.",
        )

    async def _cmd_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Offer configured GitHub backlog items as one-click model tasks."""
        if not self._auth(update):
            return
        message = self._message(update)
        if not self._is_main_thread(message):
            await message.reply_text("Use /auto in the General topic.")
            return
        args = context.args or []
        if len(args) > 1:
            await message.reply_text("Usage: /auto [number of items]")
            return
        try:
            limit = int(args[0]) if args else _AUTO_DEFAULT_LIMIT
        except ValueError:
            await message.reply_text("The item count must be a number.")
            return
        if not 1 <= limit <= _AUTO_MAX_LIMIT:
            await message.reply_text(f"Choose between 1 and {_AUTO_MAX_LIMIT} items.")
            return
        topic_id = self._topic_key(message.message_thread_id)
        if self._flow_running(topic_id):
            await message.reply_text("Finish the current flow or use /cancel first.")
            return
        self._start_flow(self.auto_runs, topic_id, lambda: self._run_auto(message, topic_id, limit))

    async def _run_auto(self, message: Any, topic_id: int | None, limit: int) -> None:
        try:
            issues = await self._next_backlog_items(topic_id, limit)
        except RuntimeError as error:
            await self._send(topic_id, f"⚠️ Could not fetch the backlog: {html.escape(str(error))}")
            return
        except Exception:  # noqa: BLE001
            logger.exception("fetching the automatic backlog failed")
            await self._send(topic_id, "⚠️ Could not fetch the backlog; see the bot logs for details.")
            return

        if not issues:
            await self._send(topic_id, "No open backlog items were found.")
            return
        for issue in issues:
            key = self._issue_key(issue)
            repo = self._repo_for_backlog_issue(issue)
            if repo is None:
                self.backlog_skipped.add(key)
                await self._send(
                    topic_id,
                    f"⏭ {issue.repository}#{issue.number} has no local repository named "
                    f"{Path(issue.repository).name}; skipped.",
                )
                continue
            model_command = await self._ask_auto_model(topic_id, issue)
            if model_command is None:
                self.backlog_skipped.add(key)
                continue
            self.backlog_started.add(key)
            provider, model = _MODEL_COMMANDS[model_command]
            issue_kind = "pull request" if issue.is_pr else "issue"
            url_part = "pull" if issue.is_pr else "issues"
            prompt = (
                f"Work on GitHub {issue_kind} {issue.repository}#{issue.number}: {issue.title}\n"
                f"https://github.com/{issue.repository}/{url_part}/{issue.number}"
            )
            await self._start_session(message, provider, model, None, str(repo), prompt)
        reviewed = self.backlog_started | self.backlog_skipped
        if all(self._issue_key(issue) in reviewed for issue in self.backlog):
            await self._send(
                topic_id,
                "🏁 Every backlog item has been reviewed; the next /auto fetches a fresh list and starts over.",
            )

    async def _next_backlog_items(self, topic_id: int | None, limit: int) -> list[Issue]:
        """The next unreviewed items from the cached backlog, fetching the full list when needed."""
        reviewed = self.backlog_started | self.backlog_skipped
        remaining = [issue for issue in self.backlog if self._issue_key(issue) not in reviewed]
        if not remaining:
            await self._send(
                topic_id,
                "🔁 Starting a new cycle: fetching the complete backlog…"
                if self.backlog
                else "📥 Fetching the complete backlog…",
            )
            self.backlog = await asyncio.wait_for(asyncio.to_thread(self._fetch_backlog_issues), _DISCOVERY_TIMEOUT)
            self.backlog_started.clear()
            self.backlog_skipped.clear()
            remaining = list(self.backlog)
        return remaining[:limit]

    @staticmethod
    def _issue_key(issue: Issue) -> str:
        return f"{issue.repository}#{issue.number}"

    @staticmethod
    def _fetch_backlog_issues() -> list[Issue]:
        """Load and sort the configured backlog using the current GitHub CLI login."""
        try:
            token_result = subprocess.run(
                ["gh", "auth", "token"],  # noqa: S607
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError as error:
            raise RuntimeError("GitHub CLI is not installed.") from error
        except subprocess.CalledProcessError as error:
            raise RuntimeError("GitHub CLI is not authenticated; run `gh auth login`.") from error
        token = token_result.stdout.strip()
        if not token:
            raise RuntimeError("GitHub CLI did not return a token; run `gh auth login`.")

        config = InsidersConfig.from_default_location()
        if isinstance(config.backlog_namespaces, InsidersUnset):
            raise RuntimeError("Configure [backlog].namespaces in ~/.config/insiders/insiders.toml.")  # noqa: TRY004
        issue_labels = set(config.backlog_issue_labels) if isinstance(config.backlog_issue_labels, dict) else None
        # Passing the token directly is equivalent to setting GITHUB_TOKEN for
        # insiders, but keeps the secret out of this process-wide environment.
        with GitHub(token) as github:
            # GitHub's GraphQL search regularly needs more than httpx's default
            # 5-second read timeout when paginating a large backlog.
            github.http_client.timeout = httpx.Timeout(60.0)
            backlog = get_backlog(
                config.backlog_namespaces,
                github=github,
                issue_labels=issue_labels,
            )
        if not isinstance(config.backlog_sort, InsidersUnset):
            backlog.sort(*config.backlog_sort)
        return backlog.issues

    def _repo_for_backlog_issue(self, issue: Issue) -> Path | None:
        """Map GitHub owner/repository names to local repositories.

        With URUK_REPO_PREFIXES (owner=prefix pairs), the owner's prefix is tried
        first (mkdocstrings/python -> mkdocstrings-python), then the plain name,
        which also covers eponymous repos (mkdocstrings/mkdocstrings -> mkdocstrings).
        """
        owner, _, name = issue.repository.partition("/")
        candidates = []
        prefix = self.config.repo_prefixes.get(owner)
        if prefix is not None:
            candidates.append(f"{prefix}{name}")
        candidates.append(name)
        for candidate in candidates:
            repo = self._resolve_repo(candidate)
            if repo is not None:
                return repo
        return None

    async def _ask_auto_model(self, topic_id: int | None, issue: Issue) -> str | None:
        """Ask which model, if any, should start the current backlog issue."""
        pid = next(self._pid)
        pending = _Pending(
            kind="auto",
            future=asyncio.get_running_loop().create_future(),
            topic_id=topic_id,
        )
        url_part = "pull" if issue.is_pr else "issues"
        url = f"https://github.com/{issue.repository}/{url_part}/{issue.number}"
        text = (
            f"📥 <b>{html.escape(issue.repository)}#{issue.number}</b>\n"
            f'<a href="{url}">{html.escape(issue.title)}</a>\n\n'
            "Start a task for this item? Choose a model, or skip it."
        )
        return await self._wait_for_reply(
            pid,
            pending,
            text,
            expires_after=self.config.interaction_timeout,
            parse_mode=ParseMode.HTML,
            reply_markup=self._auto_model_markup(pid),
        )

    @staticmethod
    def _auto_model_markup(pid: int) -> InlineKeyboardMarkup:
        buttons = [InlineKeyboardButton(command, callback_data=f"a:{pid}:{command}") for command in _MODEL_COMMANDS]
        rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
        rows.append([InlineKeyboardButton("⏭ Skip", callback_data=f"a:{pid}:skip")])
        return InlineKeyboardMarkup(rows)

    async def _cmd_new_deprecated(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: ARG002
        if not self._auth(update):
            return
        await self._message(update).reply_text(
            "Use /<fable|opus|sonnet|haiku|sol|terra|luna> "
            "[--low|--medium|--high|--xhigh|--max] "
            "<repo> [first prompt…].\n"
            "For example: /fable --max myrepo Fix the failing tests",
        )

    async def _cmd_new_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        message = self._message(update)
        if not self._is_main_thread(message):
            await message.reply_text("Start new sessions in the General topic.")
            return
        if self._flow_running(message.message_thread_id):
            await message.reply_text("Finish the current flow or use /cancel first.")
            return
        if message.text is None:
            return
        command, *args = message.text.split()
        model_command, separator, username = command.removeprefix("/").partition("@")
        if separator and username.lower() != context.bot.username.lower():
            return
        model_command = model_command.lower()
        provider, model = _MODEL_COMMANDS[model_command]
        effort = None
        effort_flag = _parse_effort_flag(args[0]) if args else None
        if effort_flag is not None:
            args.pop(0)
            effort = effort_flag
            if effort not in _EFFORT_LEVELS:
                choices = "|".join(f"--{value}" for value in _EFFORT_LEVELS)
                await message.reply_text(f"Unknown effort flag. Choose one of: {choices}.")
                return
        if not args:
            await message.reply_text(
                f"Usage: /{model_command} [--low|--medium|--high|--xhigh|--max] <repo> [first prompt…]",
            )
            return
        prompt = " ".join(args[1:]).strip()
        await self._start_session(message, provider, model, effort, args[0], prompt)

    async def _start_session(  # noqa: PLR0917
        self,
        message: Any,
        provider: str,
        model: str,
        effort: str | None,
        repo_argument: str,
        prompt: str,
    ) -> None:
        """Create a task topic from either a slash command or the prompt dialogue."""
        repo = self._resolve_repo(repo_argument)
        if repo is None:
            hint = " Use /repos to list them." if self.config.repos_root else ""
            await message.reply_text(f"Not a directory: {repo_argument}.{hint}")
            return
        title = prompt[:60] if prompt else "interactive session"
        info = TaskInfo(
            topic_id=0,
            repo=str(repo),
            title=title,
            provider=provider,
            model=model,
            effort=effort,
        )
        try:
            topic = await self._application().bot.create_forum_topic(
                chat_id=self.config.chat_id,
                name=self._topic_name(info),
            )
        except TelegramError as error:
            await message.reply_text(
                f"Could not create a topic ({error}). Is the group a forum, and is the bot "
                "an admin with the 'Manage topics' permission?",
            )
            return
        info.topic_id = topic.message_thread_id
        self.store.set(info)
        task = self._make_task(info)
        self.tasks[info.topic_id] = task
        await self._pin_status(info)
        if prompt:
            await self._send(info.topic_id, f"🎯 {prompt}")
            await task.submit(prompt)
        else:
            await self._send(info.topic_id, "Say what you need.")
        await message.reply_text(f"Started: {repo.name} · {title}")

    def _resolve_repo(self, arg: str) -> Path | None:
        path = Path(arg).expanduser()
        if not path.is_absolute() and self.config.repos_root is not None:
            path = self.config.repos_root / arg
        return path if path.is_dir() else None

    async def _cmd_repos(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: ARG002
        if not self._auth(update):
            return
        if self.config.repos_root is None:
            await self._message(update).reply_text(
                "URUK_REPOS_ROOT is not set; use an absolute path with your model command.",
            )
            return
        names = sorted(
            path.name for path in self.config.repos_root.iterdir() if path.is_dir() and not path.name.startswith(".")
        )
        text = "\n".join(names) or "(empty)"
        for part in _chunk(text):
            await self._message(update).reply_text(part)

    async def _cmd_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: ARG002
        if not self._auth(update):
            return
        lines = []
        for info in self.store.all():
            task = self.tasks.get(info.topic_id)
            if info.owner == "terminal":
                status = "💻 terminal"
            elif info.owner == "transferring":
                status = "🔄 transferring"
            elif task is not None and task.turn_running:
                status = "🟢 running"
            elif task is not None and task.active:
                status = "🔵 idle"
            else:
                status = "💤 resumable"
            lines.append(f"{status} — {Path(info.repo).name} — {info.title}")
        await self._message(update).reply_text("\n".join(lines) or "No sessions.")

    async def _cmd_attach(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: ARG002
        """Offer local Claude and Codex sessions to take over in Telegram."""
        if not self._auth(update):
            return
        message = self._message(update)
        if not self._is_main_thread(message):
            await message.reply_text("Use /attach in the General topic.")
            return
        topic_id = self._topic_key(message.message_thread_id)
        if self._flow_running(topic_id):
            await message.reply_text("Finish the current flow or use /cancel first.")
            return
        self._start_flow(self._attach_runs, topic_id, lambda: self._run_attach(message, topic_id))

    async def _run_attach(self, message: Message, topic_id: int | None) -> None:
        """Discover sessions and collect a choice while updates continue to arrive."""
        await message.reply_text("🔎 Looking for local Claude and Codex sessions…")
        try:
            candidates = await asyncio.wait_for(self._discover_attach_candidates(), _DISCOVERY_TIMEOUT)
        except Exception:  # noqa: BLE001
            logger.exception("discovering local sessions failed")
            await message.reply_text("Could not list local sessions; see the Uruk log for details.")
            return
        if not candidates:
            await message.reply_text("No untracked local sessions were found.")
            return
        candidate = await self._choose_attach_candidate(topic_id, candidates)
        if not isinstance(candidate, _AttachCandidate):
            return
        if candidate.status == "active":
            await message.reply_text(
                "That session still has an active turn. Let it finish and close the terminal client, "
                "then run /attach again. Uruk will not take over a live terminal client.",
            )
            return
        await self._attach_candidate(message, candidate)

    async def _discover_attach_candidates(self) -> list[_AttachCandidate]:
        """List recent provider sessions which are not already owned by Uruk."""
        tracked = {
            info.session_id
            for info in self.store.all()
            if info.session_id and not (info.origin == "terminal" and info.owner == "terminal")
        }
        claude, active_claude = await asyncio.gather(
            asyncio.to_thread(list_sessions, limit=12),
            asyncio.to_thread(self._active_claude_session_ids),
        )
        candidates = [
            _AttachCandidate(
                provider="claude",
                session_id=session.session_id,
                repo=session.cwd or "",
                title=session.summary or session.first_prompt or "Claude session",
                status="active" if session.session_id in active_claude else "idle",
            )
            for session in claude
            if session.session_id not in tracked and session.cwd and Path(session.cwd).is_dir()  # noqa: ASYNC240
        ]
        async with AsyncCodex(CodexConfig()) as codex:
            threads = await codex.thread_list(limit=12)
        for thread in threads.data:
            if thread.id in tracked or not Path(str(thread.cwd)).is_dir():  # noqa: ASYNC240
                continue
            status = getattr(thread.status.root, "type", "idle")
            candidates.append(
                _AttachCandidate(
                    provider="openai",
                    session_id=thread.id,
                    repo=str(thread.cwd),
                    title=thread.name or thread.preview or "Codex session",
                    status=str(getattr(status, "value", status)),
                ),
            )
        # Both providers return most-recent-first lists.  A single compact
        # picker is more usable on a phone than exhaustive pagination.
        return candidates[:20]

    @staticmethod
    def _active_claude_session_ids() -> set[str]:
        """Read the CLI's machine-readable list of live Claude sessions.

        The SDK's transcript listing deliberately has no liveness field.  The
        Claude CLI does, and this parser is tolerant of either snake_case or
        camelCase names while the CLI format evolves.
        """
        try:
            result = subprocess.run(
                ["claude", "agents", "--json"],  # noqa: S607
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            sessions = json.loads(result.stdout)
        except (FileNotFoundError, subprocess.SubprocessError, json.JSONDecodeError):
            return set()
        if not isinstance(sessions, list):
            return set()
        return {
            session_id
            for session in sessions
            if isinstance(session, dict)
            and isinstance((session_id := session.get("session_id") or session.get("sessionId")), str)
        }

    async def _choose_attach_candidate(
        self,
        topic_id: int | None,
        candidates: list[_AttachCandidate],
    ) -> _AttachCandidate | None:
        pid = next(self._pid)
        pending = _Pending(
            kind="attach",
            future=asyncio.get_running_loop().create_future(),
            topic_id=topic_id,
            payloads=list(candidates),
        )
        rows = []
        for index, candidate in enumerate(candidates):
            provider = "Claude" if candidate.provider == "claude" else "Codex"
            status = " · busy" if candidate.status == "active" else ""
            label = f"{provider} · {Path(candidate.repo).name} · {candidate.title}".replace("\n", " ")
            rows.append([InlineKeyboardButton((label[:56] + status)[:64], callback_data=f"x:{pid}:{index}")])
        result = await self._wait_for_reply(
            pid,
            pending,
            "Choose a local session to attach. Busy sessions stay in their terminal until the current turn ends.",
            expires_after=self.config.interaction_timeout,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return result if isinstance(result, _AttachCandidate) else None

    async def _attach_candidate(self, message: Any, candidate: _AttachCandidate) -> None:
        """Create the topic and defer the provider resume until its first message."""
        existing = next(
            (
                info
                for info in self.store.all()
                if info.session_id == candidate.session_id and info.origin == "terminal" and info.owner == "terminal"
            ),
            None,
        )
        if existing is not None:
            existing.owner = "telegram"
            self.store.save()
            self.tasks[existing.topic_id] = self._make_task(existing)
            await self._send(
                existing.topic_id,
                "📲 Attached again. The terminal client is now stale; do not use it. Send a message here to continue.",
            )
            await message.reply_text(f"Attached again: {Path(existing.repo).name} · {existing.title}")
            return
        info = TaskInfo(
            topic_id=0,
            repo=candidate.repo,
            title=candidate.title[:60],
            provider=candidate.provider,
            session_id=candidate.session_id,
            origin="terminal",
            owner="telegram",
        )
        try:
            topic = await self._application().bot.create_forum_topic(
                chat_id=self.config.chat_id,
                name=self._topic_name(info),
            )
        except TelegramError as error:
            await message.reply_text(f"Could not create an attachment topic: {error}")
            return
        info.topic_id = topic.message_thread_id
        self.store.set(info)
        self.tasks[info.topic_id] = self._make_task(info)
        await self._pin_status(info)
        await self._send(
            info.topic_id,
            "📲 Attached from a terminal session. The terminal client is now stale; do not use it. "
            "Send a message here to continue.",
        )
        await message.reply_text(f"Attached: {Path(info.repo).name} · {info.title}")

    async def _cmd_release(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: ARG002
        """Release a topic so its provider session can be resumed in a terminal."""
        if not self._auth(update):
            return
        message = self._message(update)
        topic_id = message.message_thread_id
        if topic_id is None or self.store.get(topic_id) is None:
            await message.reply_text("Use /release inside a task topic.")
            return
        if self._flow_running(topic_id):
            await message.reply_text("A handoff is already in progress. Use /cancel to stop it.")
            return
        self._start_flow(self._release_runs, topic_id, lambda: self._run_release(message, topic_id))

    async def _run_release(self, message: Message, topic_id: int) -> None:
        """Release a session without blocking interrupt or cancel commands."""
        try:
            await self._release_to_terminal(topic_id)
        except RuntimeError as error:
            await message.reply_text(f"Cannot release this session: {error}")
            return
        info = self.store.get(topic_id)
        if info is None or not info.session_id:
            return
        command = (
            f"claude --resume {info.session_id}" if info.provider == "claude" else f"codex resume {info.session_id}"
        )
        await message.reply_text(
            f"Resume it from {info.repo}:\n<code>{html.escape(command)}</code>",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_interrupt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: ARG002
        if not self._auth(update):
            return
        message = self._message(update)
        topic_id = message.message_thread_id
        task = self.tasks.get(topic_id) if topic_id is not None else None
        if task is None:
            await message.reply_text("Use /interrupt inside an active task topic.")
        else:
            self._cancel_interactions(topic_id)
            if await task.interrupt():
                await message.reply_text("⏹ Interrupt sent.")
            else:
                await message.reply_text("Nothing is running in this task.")

    async def _cancel_flows(self, topic_id: int | None) -> bool:
        topic_id = self._topic_key(topic_id)
        flows = [task for mapping in self._flow_maps() if (task := mapping.get(topic_id)) is not None and not task.done()]
        for task in flows:
            task.cancel()
        if flows:
            await asyncio.gather(*flows, return_exceptions=True)
        for mapping in self._flow_maps():
            if mapping.get(topic_id) in flows:
                mapping.pop(topic_id, None)
        return bool(flows)

    async def _cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: ARG002
        """Cancel a dialogue or answer wait without closing the session."""
        if not self._auth(update):
            return
        message = self._message(update)
        topic_id = self._topic_key(message.message_thread_id)
        cancelled = await self._cancel_flows(topic_id)
        cancelled = self._cancel_interactions(topic_id) or cancelled
        await message.reply_text("Cancelled. You can start again." if cancelled else "No interaction is waiting. Use /interrupt to stop an agent turn.")

    def _topic_task(self, update: Update) -> AgentTask | None:
        """The task for the topic this command was sent in, or None (with a hint sent)."""
        topic_id = self._message(update).message_thread_id
        return self._get_task(topic_id) if topic_id is not None else None

    def _topic_name(self, info: TaskInfo) -> str:
        """Topic name showing the repo and title (Telegram caps names at 128 chars)."""
        return f"{Path(info.repo).name} · {info.title}"[:96]

    def _status_text(self, info: TaskInfo) -> str:
        """The compact status message: current model and effort."""
        # The init message of a session reports the actually-resolved model; before the
        # first one arrives, fall back to whatever was requested.
        model = info.resolved_model or info.model
        if model is None and info.provider == "claude":
            model = self.config.model
        model = _MODEL_LABELS.get(model, model or "resolving…")
        effort = info.effort or ("default" if info.provider == "openai" else "high")
        return f"{model}: {effort}"

    async def _pin_status(self, info: TaskInfo) -> None:
        """Send the status message, force a pin transition, and remember its ID."""
        try:
            message = await self._application().bot.send_message(
                chat_id=self.config.chat_id,
                text=self._status_text(info),
                message_thread_id=info.topic_id,
            )
        except TelegramError:
            logger.opt(exception=True).warning("failed to send status message to topic {}", info.topic_id)
            return
        info.status_message_id = message.message_id
        self.store.save()
        try:
            await self._application().bot.unpin_chat_message(
                chat_id=self.config.chat_id,
                message_id=message.message_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "failed to unpin status message {} in topic {} before pinning",
                message.message_id,
                info.topic_id,
            )
        try:
            await self._application().bot.pin_chat_message(
                chat_id=self.config.chat_id,
                message_id=message.message_id,
                disable_notification=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "failed to pin status message {} in topic {}",
                message.message_id,
                info.topic_id,
            )

    async def refresh_status(self, topic_id: int) -> None:
        """UI-protocol hook: an agent session learned its actual model; refresh its status."""
        info = self.store.get(topic_id)
        if info is not None:
            await self._update_status(info)

    async def _update_status(self, info: TaskInfo) -> None:
        """Refresh the status message (creating it for topics that predate it)."""
        if info.status_message_id is None:
            await self._pin_status(info)
            return
        try:
            await self._application().bot.edit_message_text(
                chat_id=self.config.chat_id,
                message_id=info.status_message_id,
                text=self._status_text(info),
            )
        except TelegramError as error:
            if "not modified" not in str(error).lower():
                logger.opt(exception=True).warning("failed to update status message for topic {}", info.topic_id)

    async def _cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        message = self._message(update)
        task = self._topic_task(update)
        if task is None:
            await message.reply_text("Use /model inside a task topic.")
            return
        if task.info.owner != "telegram":
            await message.reply_text("This session is checked out to a terminal; change its model there.")
            return
        if not context.args:
            current = task.info.resolved_model or task.info.model
            if current is None and task.info.provider == "claude":
                current = self.config.model
            current = _MODEL_LABELS.get(current, current or "(default)")
            await message.reply_text(f"Model: {current}\nChange with /model <name>, reset with /model default.")
            return
        requested = context.args[0].lower()
        model = None if requested == "default" else requested
        if model in _MODEL_COMMANDS:
            provider, canonical_model = _MODEL_COMMANDS[model]
            if provider != task.info.provider:
                await message.reply_text(
                    "A task cannot switch SDK providers. Start a new topic with that model command.",
                )
                return
            model = canonical_model
        live = await task.set_model(model)
        await self._update_status(task.info)
        shown = _MODEL_LABELS.get(model, model or "(default)")
        if task.info.provider == "openai":
            when = "takes effect from the next turn"
        else:
            when = "applied to the running session" if live else "takes effect when the session starts"
        await message.reply_text(f"Model set to {shown} — {when}.")

    async def _cmd_effort(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        message = self._message(update)
        task = self._topic_task(update)
        if task is None:
            await message.reply_text("Use /effort inside a task topic.")
            return
        if task.info.owner != "telegram":
            await message.reply_text("This session is checked out to a terminal; change its effort there.")
            return
        if not context.args:
            default_effort = "default" if task.info.provider == "openai" else "high (default)"
            await message.reply_text(
                f"Effort: {task.info.effort or default_effort}\n"
                "Change with /effort <low|medium|high|xhigh|max>, reset with /effort default.",
            )
            return
        value = context.args[0].lower()
        if value not in {*_EFFORT_LEVELS, "default"}:
            await message.reply_text("Effort must be one of: low, medium, high, xhigh, max, default.")
            return
        if task.turn_running:
            await message.reply_text(
                "A turn is running; effort changes apply between turns. "
                "Wait for it to finish (or /interrupt), then retry.",
            )
            return
        effort = None if value == "default" else value
        await task.set_effort(effort)
        await self._update_status(task.info)
        await message.reply_text(
            f"Effort set to {effort or '(default)'} — applies from your next message.",
        )

    async def _cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: ARG002
        if not self._auth(update):
            return
        message = self._message(update)
        topic_id = message.message_thread_id
        if topic_id is None or self.store.get(topic_id) is None:
            await message.reply_text("Use /close inside a task topic.")
            return
        task = self.tasks.pop(topic_id, None)
        await self._cancel_flows(topic_id)
        self._cancel_interactions(topic_id)
        if task is not None:
            await task.close()
        self.store.remove(topic_id)
        await message.reply_text("Closed.")
        with contextlib.suppress(TelegramError):
            await self._application().bot.close_forum_topic(chat_id=self.config.chat_id, message_thread_id=topic_id)
        self.store.add_closed(topic_id)

    async def _cmd_purge(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: ARG002
        if not self._auth(update):
            return
        message = self._message(update)
        deleted, failed = 0, 0
        for topic_id in self.store.closed():
            try:
                await self._application().bot.delete_forum_topic(
                    chat_id=self.config.chat_id,
                    message_thread_id=topic_id,
                )
            except TelegramError as error:
                if "not found" in str(error).lower():  # Already deleted by hand: stop tracking it.
                    self.store.remove_closed(topic_id)
                else:
                    logger.warning("failed to delete topic {}: {}", topic_id, error)
                    failed += 1
            else:
                self.store.remove_closed(topic_id)
                deleted += 1
        orphaned = await self._purge_orphaned_sessions()
        if not deleted and not failed and not orphaned:
            await message.reply_text("Nothing to purge: no closed topics, and every session still has its topic.")
            return
        lines = [f"🗑 Deleted {deleted} closed topic{'s' if deleted != 1 else ''}."]
        if orphaned:
            lines.append(f"🧹 Forgot {orphaned} session{'s' if orphaned != 1 else ''} whose topic no longer exists.")
        if failed:
            lines.append(
                f"⚠️ {failed} could not be deleted — does the bot have the 'Delete messages' "
                "admin permission? They stay tracked; retry with /purge.",
            )
        await message.reply_text("\n".join(lines))

    async def _purge_orphaned_sessions(self) -> int:
        """Forget sessions whose topics were deleted by hand, where /close was impossible.

        The Bot API cannot look up a forum topic, but sending a chat action to its
        thread fails with "message thread not found" once the topic is deleted.
        """
        orphaned = 0
        for info in self.store.all():
            try:
                await self._application().bot.send_chat_action(
                    chat_id=self.config.chat_id,
                    action=ChatAction.TYPING,
                    message_thread_id=info.topic_id,
                )
            except TelegramError as error:
                if "thread not found" not in str(error).lower():
                    logger.warning("could not probe topic {}: {}", info.topic_id, error)
                    continue
                task = self.tasks.pop(info.topic_id, None)
                await self._cancel_flows(info.topic_id)
                self._cancel_interactions(info.topic_id)
                if task is not None:
                    await task.close()
                self.store.remove(info.topic_id)
                orphaned += 1
        return orphaned


class _InterceptHandler(logging.Handler):
    """Route stdlib logging records (telegram, httpx, …) through loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        """Send a standard logging record to Loguru with its caller location."""
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # Walk back to the caller emitting the record, so loguru reports it
        # instead of this handler.
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _run_bot() -> None:
    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.INFO, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        config = Config.from_env()
    except ConfigError as error:
        sys.exit(f"uruk: {error}")
    bot = UrukBot(config)
    app = (
        Application.builder()
        .token(config.token)
        .post_init(bot.start_control_server)
        .post_shutdown(bot.shutdown)
        .build()
    )
    bot.attach(app)
    if config.configured:
        logger.info(
            "starting (chat=%s, owner=%s, repos_root=%s, mode=%s)",
            config.chat_id,
            config.owner_id,
            config.repos_root,
            config.permission_mode,
        )
    else:
        logger.warning("TELEGRAM_CHAT_ID / TELEGRAM_OWNER_ID not set — setup mode: only /id will respond.")
    app.run_polling(allowed_updates=["message", "callback_query"])

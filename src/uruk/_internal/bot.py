"""Telegram side: handlers, approval buttons, and routing between topics and agent sessions."""

from __future__ import annotations

import asyncio
import contextlib
import html
import inspect
import itertools
import json
import logging
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
from claude_agent_sdk import list_sessions
from loguru import logger
from openai_codex import AsyncCodex, CodexConfig
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, Update
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
from telegramify_markdown.content import ContentType, File, Photo, Text

from insiders import Config as InsidersConfig
from insiders import GitHub, Issue, get_backlog
from insiders import Unset as InsidersUnset

from uruk._internal.agent import AgentTask, ClaudeAgentTask, summarize_tool_input
from uruk._internal.codex_agent import CodexAgentTask
from uruk._internal.config import Config, ConfigError
from uruk._internal.store import SessionStore, TaskInfo

MESSAGE_LIMIT = 4000  # Telegram caps messages at 4096 chars; keep headroom.
MIN_FILE_LINES = 30  # Code blocks longer than this become file attachments.
MODEL_COMMANDS = {
    "fable": ("claude", "fable"),
    "opus": ("claude", "opus"),
    "sonnet": ("claude", "sonnet"),
    "haiku": ("claude", "haiku"),
    "sol": ("openai", "gpt-5.6-sol"),
    "terra": ("openai", "gpt-5.6-terra"),
    "luna": ("openai", "gpt-5.6-luna"),
}
MODEL_LABELS = {model: command for command, (_, model) in MODEL_COMMANDS.items()}
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
EFFORT_PREFIXES = ("--", "–", "—")  # ASCII double hyphen, en dash, em dash.
AUTO_DEFAULT_LIMIT = 5
AUTO_MAX_LIMIT = 20


def parse_effort_flag(argument: str) -> str | None:
    """Return an effort value from an ASCII or Telegram-smart-dash flag."""
    for prefix in EFFORT_PREFIXES:
        if argument.startswith(prefix):
            return argument.removeprefix(prefix).lower()
    return None


def chunk(text: str, limit: int = MESSAGE_LIMIT) -> Iterator[str]:
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


def to_entities(entities: list) -> list[MessageEntity] | None:
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
class Pending:
    """An interaction waiting for a button press."""

    kind: str  # "perm", "question", or "auto"
    future: asyncio.Future
    topic_id: int | None
    options: list[str] = field(default_factory=list)
    multi: bool = False
    selected: set[int] = field(default_factory=set)
    payloads: list[object] = field(default_factory=list)


@dataclass(frozen=True)
class AttachCandidate:
    """A provider session that is not yet tracked by Uruk."""

    provider: str
    session_id: str
    repo: str
    title: str
    status: str = "idle"


class UrukBot:
    """Owns the Telegram application, the agent tasks, and the pending interactions."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = SessionStore(config.data_dir / "sessions.json")
        self.tasks: dict[int, AgentTask] = {}
        self.pending: dict[int, Pending] = {}
        self.awaiting_text: dict[int | None, asyncio.Future] = {}
        self.prompt_dialogues: dict[int | None, asyncio.Task] = {}
        self.auto_runs: dict[int | None, asyncio.Task] = {}
        self.control_server: asyncio.AbstractServer | None = None
        self.backlog: list[Issue] = []  # Fetched once, in memory, until a full review cycle ends.
        self.backlog_started: set[str] = set()
        self.backlog_skipped: set[str] = set()
        self._pid = itertools.count(1)
        self.app: Application | None = None

    # -- wiring ------------------------------------------------------------------

    def attach(self, app: Application) -> None:
        self.app = app
        app.add_handler(CommandHandler("id", self.cmd_id))
        app.add_handler(CommandHandler("start", self.cmd_help))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("repos", self.cmd_repos))
        app.add_handler(CommandHandler("list", self.cmd_list))
        app.add_handler(CommandHandler("attach", self.cmd_attach))
        app.add_handler(CommandHandler("release", self.cmd_release))
        app.add_handler(CommandHandler("interrupt", self.cmd_interrupt))
        app.add_handler(CommandHandler("close", self.cmd_close))
        app.add_handler(CommandHandler("purge", self.cmd_purge))
        app.add_handler(CommandHandler("auto", self.cmd_auto))
        app.add_handler(CommandHandler("model", self.cmd_model))
        app.add_handler(CommandHandler("effort", self.cmd_effort))
        app.add_handler(CommandHandler(tuple(MODEL_COMMANDS), self.cmd_new_model))
        app.add_handler(CallbackQueryHandler(self.on_button))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        # Handler groups are processed in order. This deliberately comes after the
        # command handlers so the panel follows each command's response.
        app.add_handler(MessageHandler(filters.COMMAND, self.show_command_panel), group=1)

    async def shutdown(self, app: Application) -> None:
        for dialogue in self.prompt_dialogues.values():
            dialogue.cancel()
        self.prompt_dialogues.clear()
        for run in self.auto_runs.values():
            run.cancel()
        self.auto_runs.clear()
        for task in list(self.tasks.values()):
            await task.close()
        if self.control_server is not None:
            self.control_server.close()
            await self.control_server.wait_closed()
            self.control_server = None
        with contextlib.suppress(OSError):
            self._control_socket_path().unlink()

    def _control_socket_path(self) -> Path:
        """The local-only control socket used by ``uruk resume``."""
        return self.config.data_dir / "control.sock"

    async def start_control_server(self, app: Application) -> None:
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
            raw = await reader.readline()
            request = json.loads(raw)
            if request.get("action") != "resume" or not isinstance(request.get("topic_id"), int):
                raise ValueError("unknown request")
            await self._release_to_terminal(request["topic_id"], telegram_origin_only=True)
            response = {"ok": True}
        except Exception as error:
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
        task = self._get_task(topic_id)
        if task is not None:
            if task.turn_running:
                await self._send(topic_id, "⏳ Finishing this turn before releasing it to your terminal…")
            while task.turn_running:
                await asyncio.sleep(0.25)
            # ``close`` is called only between turns, so it cancels a worker
            # waiting for its next prompt rather than interrupting the last one.
            await task.close()
            self.tasks.pop(topic_id, None)
        info.owner = "terminal"
        self.store.save()
        await self._send(topic_id, "💻 Released to your terminal. This topic is now read-only until you attach it again.")

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
    def _is_main_thread(message) -> bool:
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
        try:
            boxes = await telegramify(text, max_message_length=MESSAGE_LIMIT, min_file_lines=MIN_FILE_LINES)
        except Exception:
            logger.opt(exception=True).warning("telegramify failed for topic {}, sending plain text", topic_id)
            for part in chunk(text):
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
        if box.content_type == ContentType.TEXT:
            await self.app.bot.send_message(
                chat_id=self.config.chat_id,
                text=box.text,
                entities=to_entities(box.entities),
                message_thread_id=topic_id,
            )
        elif box.content_type == ContentType.FILE:
            await self.app.bot.send_document(
                chat_id=self.config.chat_id,
                document=box.file_data,
                filename=box.file_name,
                caption=box.caption_text or None,
                caption_entities=to_entities(box.caption_entities),
                message_thread_id=topic_id,
            )
        else:  # ContentType.PHOTO (e.g. rendered mermaid diagrams)
            await self.app.bot.send_photo(
                chat_id=self.config.chat_id,
                photo=box.file_data,
                caption=box.caption_text or None,
                caption_entities=to_entities(box.caption_entities),
                message_thread_id=topic_id,
            )

    async def send_activity(self, topic_id: int, text: str) -> None:
        await self._send(topic_id, f"<i>{html.escape(text)}</i>", parse_mode=ParseMode.HTML)

    async def send_typing(self, topic_id: int) -> None:
        await self.app.bot.send_chat_action(
            chat_id=self.config.chat_id,
            action=ChatAction.TYPING,
            message_thread_id=topic_id,
        )

    async def ask_permission(self, topic_id: int, tool_name: str, input_data: dict) -> bool:
        pid = next(self._pid)
        pending = Pending(kind="perm", future=asyncio.get_running_loop().create_future(), topic_id=topic_id)
        self.pending[pid] = pending
        summary = summarize_tool_input(tool_name, input_data)[:1500]
        text = f"🔧 <b>{html.escape(tool_name)}</b>"
        if summary:
            text += f"\n<pre>{html.escape(summary)}</pre>"
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Allow", callback_data=f"p:{pid}:a"),
            InlineKeyboardButton("❌ Deny", callback_data=f"p:{pid}:d"),
        ]])
        await self._send(topic_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return await pending.future

    async def ask_question(self, topic_id: int, question: dict) -> str:
        pid = next(self._pid)
        options = [option["label"] for option in question.get("options", [])]
        pending = Pending(
            kind="question",
            future=asyncio.get_running_loop().create_future(),
            topic_id=topic_id,
            options=options,
            multi=bool(question.get("multiSelect")),
        )
        self.pending[pid] = pending
        lines = [f"❓ <b>{html.escape(question.get('header', 'Question'))}</b>", html.escape(question["question"])]
        for option in question.get("options", []):
            description = option.get("description", "")
            lines.append(f"• <b>{html.escape(option['label'])}</b> — {html.escape(description)}")
        await self._send(
            topic_id,
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=self._question_markup(pid, pending),
        )
        answer = await pending.future
        if isinstance(answer, asyncio.Future):
            # "Other…" was picked: the button handler installed a future that the
            # user's next message in this topic resolves.
            answer = await answer
        return answer

    def _question_markup(self, pid: int, pending: Pending) -> InlineKeyboardMarkup:
        rows = []
        for index, label in enumerate(pending.options):
            mark = "✅ " if index in pending.selected else ""
            rows.append([InlineKeyboardButton(f"{mark}{label}"[:60], callback_data=f"q:{pid}:{index}")])
        if pending.multi:
            rows.append([InlineKeyboardButton("☑️ Done", callback_data=f"q:{pid}:done")])
        rows.append([InlineKeyboardButton("✍️ Other…", callback_data=f"q:{pid}:other")])
        return InlineKeyboardMarkup(rows)

    async def _send(self, topic_id: int | None, text: str, **kwargs) -> None:
        try:
            await self.app.bot.send_message(
                chat_id=self.config.chat_id,
                text=text,
                message_thread_id=topic_id,
                **kwargs,
            )
        except TelegramError:
            logger.exception("failed to send message to topic {}", topic_id)

    # -- button presses ------------------------------------------------------------

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self._authorized(query.from_user.id, query.message.chat.id if query.message else None):
            await query.answer()
            return
        if query.data.startswith("c:"):
            await self._handle_command_button(update, context)
            return
        try:
            kind, pid_str, arg = query.data.split(":", 2)
            pid = int(pid_str)
        except (AttributeError, ValueError):
            await query.answer("Unknown action.")
            return
        pending = self.pending.get(pid)
        if pending is None or pending.future.done():
            await query.answer("Expired.")
            with contextlib.suppress(TelegramError):
                await query.edit_message_reply_markup(None)
            return

        if kind == "p":
            allowed = arg == "a"
            del self.pending[pid]
            pending.future.set_result(allowed)
            await query.answer("Allowed" if allowed else "Denied")
            await self._finalize(query, "✅ Allowed" if allowed else "❌ Denied")
            return

        if kind == "q":
            if arg == "other":
                del self.pending[pid]
                text_future = asyncio.get_running_loop().create_future()
                self.awaiting_text[pending.topic_id] = text_future
                pending.future.set_result(text_future)
                await query.answer()
                await self._finalize(query, "✍️ Type your answer as a message in this topic.")
            elif arg == "done":
                labels = [pending.options[index] for index in sorted(pending.selected)]
                answer = ", ".join(labels) if labels else "(none)"
                del self.pending[pid]
                pending.future.set_result(answer)
                await query.answer()
                await self._finalize(query, f"→ {answer}")
            else:
                index = int(arg)
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
                candidate = pending.payloads[int(arg)]
            except (IndexError, ValueError):
                await query.answer("Unknown session.")
                return
            del self.pending[pid]
            pending.future.set_result(candidate)
            await query.answer()
            await self._finalize(query, "→ Selected")
            return

        if kind == "a":
            if arg != "skip" and arg not in MODEL_COMMANDS:
                await query.answer("Unknown model.")
                return
            del self.pending[pid]
            pending.future.set_result(None if arg == "skip" else arg)
            await query.answer()
            await self._finalize(query, "⏭ Skipped" if arg == "skip" else f"→ {arg}")
            return

    async def _handle_command_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Dispatch a shortcut from the persistent command panel."""
        query = update.callback_query
        message = query.message
        if message is None or not self._is_main_thread(message):
            await query.answer("Use the command panel in General.")
            return
        command = query.data.removeprefix("c:")
        handlers = {
            "auto": self.cmd_auto,
            "attach": self.cmd_attach,
            "purge": self.cmd_purge,
            "repos": self.cmd_repos,
            "list": self.cmd_list,
            "help": self.cmd_help,
            "id": self.cmd_id,
        }
        if command == "prompt":
            topic_id = message.message_thread_id
            dialogue = self.prompt_dialogues.get(topic_id)
            if dialogue is not None and not dialogue.done():
                await query.answer("A prompt setup is already in progress.")
                return
            dialogue = asyncio.create_task(self._run_prompt_dialogue(message, topic_id))
            self.prompt_dialogues[topic_id] = dialogue
            dialogue.add_done_callback(lambda task: self._finish_prompt_dialogue(topic_id, task))
            await query.answer("Prompt setup started")
            return
        handler = handlers.get(command)
        if handler is None:
            await query.answer("Unknown action.")
            return
        await query.answer()
        await handler(update, context)
        await self._maybe_show_panel(message.message_thread_id)

    def _finish_prompt_dialogue(self, topic_id: int | None, task: asyncio.Task) -> None:
        """Release a finished dialogue and make unexpected failures visible in logs."""
        self.prompt_dialogues.pop(topic_id, None)
        if task.cancelled():
            return
        if error := task.exception():
            logger.opt(exception=error).error("prompt dialogue in topic {} failed", topic_id)
            return
        asyncio.get_running_loop().create_task(self._send_panel(topic_id))

    async def _run_prompt_dialogue(self, message, topic_id: int | None) -> None:
        """Collect model, effort, repository, and prompt from the command panel."""
        model_command = await self.ask_question(
            topic_id,
            {
                "header": "Choose a model",
                "question": "Which model should run this task?",
                "options": [
                    {"label": command, "description": f"{provider.title()} · {model}"}
                    for command, (provider, model) in MODEL_COMMANDS.items()
                ],
            },
        )
        if model_command not in MODEL_COMMANDS:
            await self._send(topic_id, "That model is unavailable. Start Prompt again.")
            return
        effort_choice = await self.ask_question(
            topic_id,
            {
                "header": "Reasoning effort",
                "question": "How much reasoning effort should it use?",
                "options": [
                    {"label": "default", "description": "Use the model's default."},
                    *[{"label": effort, "description": ""} for effort in EFFORT_LEVELS],
                ],
            },
        )
        if effort_choice not in {*EFFORT_LEVELS, "default"}:
            await self._send(topic_id, "That effort level is unavailable. Start Prompt again.")
            return
        repo = await self._choose_repo_for_prompt(topic_id)
        if repo is None:
            return
        prompt = await self._request_dialogue_text(topic_id, "✍️ Send the prompt for this task.")
        provider, model = MODEL_COMMANDS[model_command]
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
            if len(matches) > 30:
                await self._send(topic_id, f"{len(matches)} repositories match. Please make the filter more specific.")
                continue
            chosen = await self.ask_question(
                topic_id,
                {
                    "header": "Choose a repository",
                    "question": f"{len(matches)} match{'es' if len(matches) != 1 else ''} for “{search}”.",
                    "options": [
                        {"label": path.name, "description": str(path)} for path in matches
                    ],
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
        if topic_id in self.awaiting_text:
            raise RuntimeError(f"topic {topic_id} is already awaiting text")
        future = asyncio.get_running_loop().create_future()
        self.awaiting_text[topic_id] = future
        await self._send(topic_id, text)
        return await future

    async def _finalize(self, query, suffix: str) -> None:
        """Append the outcome to the prompt message and drop its buttons."""
        with contextlib.suppress(TelegramError, AttributeError):
            await query.edit_message_text(
                f"{query.message.text_html}\n\n{suffix}",
                parse_mode=ParseMode.HTML,
            )

    # -- messages ------------------------------------------------------------------

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        message = update.effective_message
        topic_id = message.message_thread_id

        if topic_id in self.awaiting_text:
            future = self.awaiting_text.pop(topic_id)
            if not future.done():
                future.set_result(message.text)
            with contextlib.suppress(TelegramError):
                await message.set_reaction("✍")
            return

        if topic_id is None:
            await message.reply_text(
                "Send messages inside a task topic, or start one with "
                "/<model> [--<effort>] <repo> <task>."
            )
            return

        task = self._get_task(topic_id)
        if task is None:
            await message.reply_text(
                "No session is attached to this topic. Start one with a model command in the General topic."
            )
            return
        if task.info.owner != "telegram":
            await message.reply_text(
                "This session is checked out to a terminal. Attach it again from General before sending it Telegram messages."
            )
            return
        await task.submit(message.text)
        with contextlib.suppress(TelegramError):
            await message.set_reaction("👍")

    # -- commands ------------------------------------------------------------------

    async def show_command_panel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Keep a shortcut panel in the chat's main (General) thread."""
        if not self._auth(update):
            return
        message = update.effective_message
        if not self._is_main_thread(message):
            return
        await self._maybe_show_panel(message.message_thread_id)

    async def _maybe_show_panel(self, topic_id: int | None) -> None:
        """Repost the panel, unless a flow in this topic will repost it when it finishes."""
        for flows in (self.prompt_dialogues, self.auto_runs):
            flow = flows.get(topic_id)
            if flow is not None and not flow.done():
                return
        await self._send_panel(topic_id)

    async def _send_panel(self, topic_id: int | None) -> None:
        markup = InlineKeyboardMarkup([
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
        ])
        # The Bot API rejects the General topic's thread id (1) on plain sends.
        await self._send(None if topic_id == 1 else topic_id, "Command panel", reply_markup=markup)

    async def cmd_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        await update.effective_message.reply_text(
            f"chat_id: {chat.id}\n"
            f"user_id: {user.id}\n"
            f"chat type: {chat.type}\n"
            f"forum (topics enabled): {bool(chat.is_forum)}"
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        await update.effective_message.reply_text(
            "/<fable|opus|sonnet|haiku|sol|terra|luna> [--<effort>] <repo> [task…] — start a session\n"
            "/repos — list repositories\n"
            "/list — list sessions\n"
            "/attach — attach an idle local Claude or Codex session (in General)\n"
            "/release — wait for this turn, then release it to a terminal (in a task topic)\n"
            "/auto [N] — offer the next N unreviewed backlog items as new tasks (in General); "
            "the backlog is fetched once and cycles when fully reviewed\n"
            "/interrupt — interrupt the current turn (in a task topic)\n"
            "/close — end the session and close the topic (in a task topic)\n"
            "/purge — delete topics closed with /close, and forget sessions whose topics were deleted by hand\n"
            "/model [name|default] — show or change this task's model (in a task topic)\n"
            "/effort [low|medium|high|xhigh|max|default] — show or change this task's effort (in a task topic)\n"
            "Any text inside a Telegram-owned task topic goes to that session."
        )

    async def cmd_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Offer configured GitHub backlog items as one-click model tasks."""
        if not self._auth(update):
            return
        message = update.effective_message
        if not self._is_main_thread(message):
            await message.reply_text("Use /auto in the General topic.")
            return
        args = context.args or []
        if len(args) > 1:
            await message.reply_text("Usage: /auto [number of items]")
            return
        try:
            limit = int(args[0]) if args else AUTO_DEFAULT_LIMIT
        except ValueError:
            await message.reply_text("The item count must be a number.")
            return
        if not 1 <= limit <= AUTO_MAX_LIMIT:
            await message.reply_text(f"Choose between 1 and {AUTO_MAX_LIMIT} items.")
            return
        topic_id = message.message_thread_id
        run = self.auto_runs.get(topic_id)
        if run is not None and not run.done():
            await message.reply_text("An automatic backlog run is already in progress.")
            return
        run = asyncio.create_task(self._run_auto(message, topic_id, limit))
        self.auto_runs[topic_id] = run
        run.add_done_callback(lambda task: self._finish_auto_run(topic_id, task))

    def _finish_auto_run(self, topic_id: int | None, task: asyncio.Task) -> None:
        """Release a completed automatic backlog run and log unexpected errors."""
        self.auto_runs.pop(topic_id, None)
        if task.cancelled():
            return
        if error := task.exception():
            logger.opt(exception=error).error("automatic backlog run in topic {} failed", topic_id)
            return
        asyncio.get_running_loop().create_task(self._send_panel(topic_id))

    async def _run_auto(self, message, topic_id: int | None, limit: int) -> None:
        try:
            issues = await self._next_backlog_items(topic_id, limit)
        except RuntimeError as error:
            await self._send(topic_id, f"⚠️ Could not fetch the backlog: {html.escape(str(error))}")
            return
        except Exception:
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
            provider, model = MODEL_COMMANDS[model_command]
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
            self.backlog = await asyncio.to_thread(self._fetch_backlog_issues)
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
                ["gh", "auth", "token"],
                check=True,
                capture_output=True,
                text=True,
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
            raise RuntimeError("Configure [backlog].namespaces in ~/.config/insiders/insiders.toml.")
        issue_labels = (
            set(config.backlog_issue_labels)
            if isinstance(config.backlog_issue_labels, dict)
            else None
        )
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
        pending = Pending(
            kind="auto",
            future=asyncio.get_running_loop().create_future(),
            topic_id=topic_id,
        )
        self.pending[pid] = pending
        url_part = "pull" if issue.is_pr else "issues"
        url = f"https://github.com/{issue.repository}/{url_part}/{issue.number}"
        text = (
            f"📥 <b>{html.escape(issue.repository)}#{issue.number}</b>\n"
            f"<a href=\"{url}\">{html.escape(issue.title)}</a>\n\n"
            "Start a task for this item? Choose a model, or skip it."
        )
        await self._send(
            topic_id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=self._auto_model_markup(pid),
        )
        return await pending.future

    @staticmethod
    def _auto_model_markup(pid: int) -> InlineKeyboardMarkup:
        buttons = [
            InlineKeyboardButton(command, callback_data=f"a:{pid}:{command}")
            for command in MODEL_COMMANDS
        ]
        rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
        rows.append([InlineKeyboardButton("⏭ Skip", callback_data=f"a:{pid}:skip")])
        return InlineKeyboardMarkup(rows)

    async def cmd_new_deprecated(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        await update.effective_message.reply_text(
            "Use /<fable|opus|sonnet|haiku|sol|terra|luna> "
            "[--low|--medium|--high|--xhigh|--max] "
            "<repo> [first prompt…].\n"
            "For example: /fable --max myrepo Fix the failing tests"
        )

    async def cmd_new_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        message = update.effective_message
        command, *args = message.text.split()
        model_command, separator, username = command.removeprefix("/").partition("@")
        if separator and username.lower() != context.bot.username.lower():
            return
        model_command = model_command.lower()
        provider, model = MODEL_COMMANDS[model_command]
        effort = None
        effort_flag = parse_effort_flag(args[0]) if args else None
        if effort_flag is not None:
            args.pop(0)
            effort = effort_flag
            if effort not in EFFORT_LEVELS:
                choices = "|".join(f"--{value}" for value in EFFORT_LEVELS)
                await message.reply_text(f"Unknown effort flag. Choose one of: {choices}.")
                return
        if not args:
            await message.reply_text(
                f"Usage: /{model_command} [--low|--medium|--high|--xhigh|--max] "
                "<repo> [first prompt…]"
            )
            return
        prompt = " ".join(args[1:]).strip()
        await self._start_session(message, provider, model, effort, args[0], prompt)

    async def _start_session(
        self,
        message,
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
            topic = await self.app.bot.create_forum_topic(
                chat_id=self.config.chat_id,
                name=self._topic_name(info),
            )
        except TelegramError as error:
            await message.reply_text(
                f"Could not create a topic ({error}). Is the group a forum, and is the bot "
                "an admin with the 'Manage topics' permission?"
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

    async def cmd_repos(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        if self.config.repos_root is None:
            await update.effective_message.reply_text(
                "URUK_REPOS_ROOT is not set; use an absolute path with your model command."
            )
            return
        names = sorted(
            path.name for path in self.config.repos_root.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        )
        text = "\n".join(names) or "(empty)"
        for part in chunk(text):
            await update.effective_message.reply_text(part)

    async def cmd_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        await update.effective_message.reply_text("\n".join(lines) or "No sessions.")

    async def cmd_attach(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Offer local Claude and Codex sessions to take over in Telegram."""
        if not self._auth(update):
            return
        message = update.effective_message
        if not self._is_main_thread(message):
            await message.reply_text("Use /attach in the General topic.")
            return
        await message.reply_text("🔎 Looking for local Claude and Codex sessions…")
        try:
            candidates = await self._discover_attach_candidates()
        except Exception:
            logger.exception("discovering local sessions failed")
            await message.reply_text("Could not list local sessions; see the Uruk log for details.")
            return
        if not candidates:
            await message.reply_text("No untracked local sessions were found.")
            return
        candidate = await self._choose_attach_candidate(message.message_thread_id, candidates)
        if not isinstance(candidate, AttachCandidate):
            return
        if candidate.status == "active":
            await message.reply_text(
                "That session still has an active turn. Let it finish and close the terminal client, "
                "then run /attach again. Uruk will not take over a live terminal client."
            )
            return
        await self._attach_candidate(message, candidate)

    async def _discover_attach_candidates(self) -> list[AttachCandidate]:
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
            AttachCandidate(
                provider="claude",
                session_id=session.session_id,
                repo=session.cwd or "",
                title=session.summary or session.first_prompt or "Claude session",
                status="active" if session.session_id in active_claude else "idle",
            )
            for session in claude
            if session.session_id not in tracked and session.cwd and Path(session.cwd).is_dir()
        ]
        async with AsyncCodex(CodexConfig()) as codex:
            threads = await codex.thread_list(limit=12)
        for thread in threads.data:
            if thread.id in tracked or not Path(str(thread.cwd)).is_dir():
                continue
            status = getattr(thread.status.root, "type", "idle")
            candidates.append(
                AttachCandidate(
                    provider="openai",
                    session_id=thread.id,
                    repo=str(thread.cwd),
                    title=thread.name or thread.preview or "Codex session",
                    status=str(getattr(status, "value", status)),
                )
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
                ["claude", "agents", "--json"],
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
        candidates: list[AttachCandidate],
    ) -> AttachCandidate | None:
        pid = next(self._pid)
        pending = Pending(
            kind="attach",
            future=asyncio.get_running_loop().create_future(),
            topic_id=topic_id,
            payloads=list(candidates),
        )
        self.pending[pid] = pending
        rows = []
        for index, candidate in enumerate(candidates):
            provider = "Claude" if candidate.provider == "claude" else "Codex"
            status = " · busy" if candidate.status == "active" else ""
            label = f"{provider} · {Path(candidate.repo).name} · {candidate.title}".replace("\n", " ")
            rows.append([InlineKeyboardButton((label[:56] + status)[:64], callback_data=f"x:{pid}:{index}")])
        await self._send(
            topic_id,
            "Choose a local session to attach. Busy sessions stay in their terminal until the current turn ends.",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        result = await pending.future
        return result if isinstance(result, AttachCandidate) else None

    async def _attach_candidate(self, message, candidate: AttachCandidate) -> None:
        """Create the topic and defer the provider resume until its first message."""
        existing = next(
            (
                info
                for info in self.store.all()
                if info.session_id == candidate.session_id
                and info.origin == "terminal"
                and info.owner == "terminal"
            ),
            None,
        )
        if existing is not None:
            existing.owner = "telegram"
            self.store.save()
            self.tasks[existing.topic_id] = self._make_task(existing)
            await self._send(
                existing.topic_id,
                "📲 Attached again. The terminal client is now stale; do not use it. "
                "Send a message here to continue.",
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
            topic = await self.app.bot.create_forum_topic(
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

    async def cmd_release(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Release a topic so its provider session can be resumed in a terminal."""
        if not self._auth(update):
            return
        message = update.effective_message
        topic_id = message.message_thread_id
        if topic_id is None or self.store.get(topic_id) is None:
            await message.reply_text("Use /release inside a task topic.")
            return
        try:
            await self._release_to_terminal(topic_id)
        except RuntimeError as error:
            await message.reply_text(f"Cannot release this session: {error}")
            return
        info = self.store.get(topic_id)
        if info is None or not info.session_id:
            return
        command = (
            f"claude --resume {info.session_id}"
            if info.provider == "claude"
            else f"codex resume {info.session_id}"
        )
        await message.reply_text(f"Resume it from {info.repo}:\n<code>{html.escape(command)}</code>", parse_mode=ParseMode.HTML)

    async def cmd_interrupt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        message = update.effective_message
        topic_id = message.message_thread_id
        task = self.tasks.get(topic_id) if topic_id is not None else None
        if task is None:
            await message.reply_text("Use /interrupt inside an active task topic.")
        elif await task.interrupt():
            await message.reply_text("⏹ Interrupt sent.")
        else:
            await message.reply_text("Nothing is running in this task.")

    def _topic_task(self, update: Update) -> AgentTask | None:
        """The task for the topic this command was sent in, or None (with a hint sent)."""
        topic_id = update.effective_message.message_thread_id
        task = self._get_task(topic_id) if topic_id is not None else None
        return task

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
        model = MODEL_LABELS.get(model, model or "resolving…")
        effort = info.effort or ("default" if info.provider == "openai" else "high")
        return f"{model}: {effort}"

    async def _pin_status(self, info: TaskInfo) -> None:
        """Send the status message, force a pin transition, and remember its ID."""
        try:
            message = await self.app.bot.send_message(
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
            await self.app.bot.unpin_chat_message(
                chat_id=self.config.chat_id,
                message_id=message.message_id,
            )
        except Exception:
            logger.exception(
                "failed to unpin status message {} in topic {} before pinning",
                message.message_id,
                info.topic_id,
            )
        try:
            await self.app.bot.pin_chat_message(
                chat_id=self.config.chat_id,
                message_id=message.message_id,
                disable_notification=True,
            )
        except Exception:
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
            await self.app.bot.edit_message_text(
                chat_id=self.config.chat_id,
                message_id=info.status_message_id,
                text=self._status_text(info),
            )
        except TelegramError as error:
            if "not modified" not in str(error).lower():
                logger.opt(exception=True).warning("failed to update status message for topic {}", info.topic_id)

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        message = update.effective_message
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
            current = MODEL_LABELS.get(current, current or "(default)")
            await message.reply_text(f"Model: {current}\nChange with /model <name>, reset with /model default.")
            return
        requested = context.args[0].lower()
        model = None if requested == "default" else requested
        if model in MODEL_COMMANDS:
            provider, canonical_model = MODEL_COMMANDS[model]
            if provider != task.info.provider:
                await message.reply_text(
                    "A task cannot switch SDK providers. Start a new topic with that model command."
                )
                return
            model = canonical_model
        live = await task.set_model(model)
        await self._update_status(task.info)
        shown = MODEL_LABELS.get(model, model or "(default)")
        if task.info.provider == "openai":
            when = "takes effect from the next turn"
        else:
            when = "applied to the running session" if live else "takes effect when the session starts"
        await message.reply_text(f"Model set to {shown} — {when}.")

    async def cmd_effort(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        message = update.effective_message
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
                "Change with /effort <low|medium|high|xhigh|max>, reset with /effort default."
            )
            return
        value = context.args[0].lower()
        if value not in {*EFFORT_LEVELS, "default"}:
            await message.reply_text("Effort must be one of: low, medium, high, xhigh, max, default.")
            return
        if task.turn_running:
            await message.reply_text(
                "A turn is running; effort changes apply between turns. "
                "Wait for it to finish (or /interrupt), then retry."
            )
            return
        effort = None if value == "default" else value
        await task.set_effort(effort)
        await self._update_status(task.info)
        await message.reply_text(
            f"Effort set to {effort or '(default)'} — applies from your next message."
        )

    async def cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        message = update.effective_message
        topic_id = message.message_thread_id
        if topic_id is None or self.store.get(topic_id) is None:
            await message.reply_text("Use /close inside a task topic.")
            return
        task = self.tasks.pop(topic_id, None)
        if task is not None:
            await task.close()
        self.store.remove(topic_id)
        self.awaiting_text.pop(topic_id, None)
        await message.reply_text("Closed.")
        with contextlib.suppress(TelegramError):
            await self.app.bot.close_forum_topic(chat_id=self.config.chat_id, message_thread_id=topic_id)
        self.store.add_closed(topic_id)

    async def cmd_purge(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        message = update.effective_message
        deleted, failed = 0, 0
        for topic_id in self.store.closed():
            try:
                await self.app.bot.delete_forum_topic(chat_id=self.config.chat_id, message_thread_id=topic_id)
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
                "admin permission? They stay tracked; retry with /purge."
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
                await self.app.bot.send_chat_action(
                    chat_id=self.config.chat_id,
                    action=ChatAction.TYPING,
                    message_thread_id=info.topic_id,
                )
            except TelegramError as error:
                if "thread not found" not in str(error).lower():
                    logger.warning("could not probe topic {}: {}", info.topic_id, error)
                    continue
                task = self.tasks.pop(info.topic_id, None)
                if task is not None:
                    await task.close()
                self.store.remove(info.topic_id)
                self.awaiting_text.pop(info.topic_id, None)
                orphaned += 1
        return orphaned


class InterceptHandler(logging.Handler):
    """Route stdlib logging records (telegram, httpx, …) through loguru."""

    def emit(self, record: logging.LogRecord) -> None:
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


def main() -> None:
    logging.basicConfig(handlers=[InterceptHandler()], level=logging.INFO, force=True)
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
            config.chat_id, config.owner_id, config.repos_root, config.permission_mode,
        )
    else:
        logger.warning("TELEGRAM_CHAT_ID / TELEGRAM_OWNER_ID not set — setup mode: only /id will respond.")
    app.run_polling(allowed_updates=["message", "callback_query"])

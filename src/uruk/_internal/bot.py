"""Telegram side: handlers, approval buttons, and routing between topics and agent sessions."""

from __future__ import annotations

import asyncio
import contextlib
import html
import itertools
import logging
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

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

from uruk._internal.agent import AgentTask, summarize_tool_input
from uruk._internal.config import Config, ConfigError
from uruk._internal.store import SessionStore, TaskInfo

logger = logging.getLogger(__name__)

MESSAGE_LIMIT = 4000  # Telegram caps messages at 4096 chars; keep headroom.
MIN_FILE_LINES = 30  # Code blocks longer than this become file attachments.


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
    """Convert telegramify-markdown entities to python-telegram-bot ones."""
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
    ] or None


@dataclass
class Pending:
    """An interaction waiting for a button press."""

    kind: str  # "perm" or "question"
    future: asyncio.Future
    topic_id: int
    options: list[str] = field(default_factory=list)
    multi: bool = False
    selected: set[int] = field(default_factory=set)


class UrukBot:
    """Owns the Telegram application, the agent tasks, and the pending interactions."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = SessionStore(config.data_dir / "sessions.json")
        self.tasks: dict[int, AgentTask] = {}
        self.pending: dict[int, Pending] = {}
        self.awaiting_text: dict[int, asyncio.Future] = {}
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
        app.add_handler(CommandHandler("interrupt", self.cmd_interrupt))
        app.add_handler(CommandHandler("close", self.cmd_close))
        app.add_handler(CommandHandler("purge", self.cmd_purge))
        app.add_handler(CommandHandler("model", self.cmd_model))
        app.add_handler(CommandHandler("effort", self.cmd_effort))
        app.add_handler(CommandHandler(tuple(MODEL_COMMANDS), self.cmd_new_model))
        app.add_handler(CallbackQueryHandler(self.on_button))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

    async def shutdown(self, app: Application) -> None:
        for task in list(self.tasks.values()):
            await task.close()

    def _authorized(self, user_id: int | None, chat_id: int | None) -> bool:
        if not self.config.configured:
            return False
        if chat_id == self.config.chat_id and user_id == self.config.owner_id:
            return True
        logger.warning("ignoring update from chat=%s user=%s", chat_id, user_id)
        return False

    def _auth(self, update: Update) -> bool:
        return self._authorized(
            update.effective_user.id if update.effective_user else None,
            update.effective_chat.id if update.effective_chat else None,
        )

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
            logger.warning("telegramify failed for topic %s, sending plain text", topic_id, exc_info=True)
            for part in chunk(text):
                await self._send(topic_id, part)
            return
        for box in boxes:
            try:
                await self._send_box(topic_id, box)
            except TelegramError:
                logger.warning("sending %s to topic %s failed", type(box).__name__, topic_id, exc_info=True)
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
            logger.exception("failed to send message to topic %s", topic_id)

    # -- button presses ------------------------------------------------------------

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self._authorized(query.from_user.id, query.message.chat.id if query.message else None):
            await query.answer()
            return
        kind, pid_str, arg = query.data.split(":", 2)
        pending = self.pending.get(int(pid_str))
        if pending is None or pending.future.done():
            await query.answer("Expired.")
            with contextlib.suppress(TelegramError):
                await query.edit_message_reply_markup(None)
            return
        pid = int(pid_str)

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
        await task.submit(message.text)
        with contextlib.suppress(TelegramError):
            await message.set_reaction("👍")

    # -- commands ------------------------------------------------------------------

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
            "/interrupt — interrupt the current turn (in a task topic)\n"
            "/close — end the session and close the topic (in a task topic)\n"
            "/purge — delete all topics previously closed with /close\n"
            "/model [name|default] — show or change this task's model (in a task topic)\n"
            "/effort [low|medium|high|xhigh|max|default] — show or change this task's effort (in a task topic)\n"
            "Any text inside a task topic goes to that session."
        )

    async def cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        message = update.effective_message
        if not context.args:
            await message.reply_text("Usage: /new <repo> [first prompt…]")
            return
        repo = self._resolve_repo(context.args[0])
        if repo is None:
            hint = " Use /repos to list them." if self.config.repos_root else ""
            await message.reply_text(f"Not a directory: {context.args[0]}.{hint}")
            return
        prompt = " ".join(context.args[1:]).strip()
        title = prompt[:60] if prompt else "interactive session"
        info = TaskInfo(topic_id=0, repo=str(repo), title=title)
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
        await self._send(info.topic_id, f"📂 Session in {repo} (mode: {self.config.permission_mode}).")
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
            if task is not None and task.turn_running:
                status = "🟢 running"
            elif task is not None and task.active:
                status = "🔵 idle"
            else:
                status = "💤 resumable"
            lines.append(f"{status} — {Path(info.repo).name} — {info.title}")
        await update.effective_message.reply_text("\n".join(lines) or "No sessions.")

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
        """Topic name showing the repo, title, and current model/effort (128-char Telegram cap)."""
        model = info.model or self.config.model or "default"
        suffix = f" [{model} · {info.effort or 'default'}]"
        base = f"{Path(info.repo).name} · {info.title}"
        return f"{base[: 96 - len(suffix)]}{suffix}"

    async def _rename_topic(self, info: TaskInfo) -> None:
        """Push the current topic name (model/effort included) to Telegram."""
        try:
            await self.app.bot.edit_forum_topic(
                chat_id=self.config.chat_id,
                message_thread_id=info.topic_id,
                name=self._topic_name(info),
            )
        except TelegramError:
            logger.warning("failed to rename topic %s", info.topic_id, exc_info=True)

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update):
            return
        message = update.effective_message
        task = self._topic_task(update)
        if task is None:
            await message.reply_text("Use /model inside a task topic.")
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
        closed = self.store.closed()
        if not closed:
            await message.reply_text("No closed topics to delete (only topics closed with /close are tracked).")
            return
        deleted, failed = 0, 0
        for topic_id in closed:
            try:
                await self.app.bot.delete_forum_topic(chat_id=self.config.chat_id, message_thread_id=topic_id)
            except TelegramError as error:
                if "not found" in str(error).lower():  # Already deleted by hand: stop tracking it.
                    self.store.remove_closed(topic_id)
                else:
                    logger.warning("failed to delete topic %s: %s", topic_id, error)
                    failed += 1
            else:
                self.store.remove_closed(topic_id)
                deleted += 1
        text = f"🗑 Deleted {deleted} closed topic{'s' if deleted != 1 else ''}."
        if failed:
            text += (
                f"\n⚠️ {failed} could not be deleted — does the bot have the 'Delete messages' "
                "admin permission? They stay tracked; retry with /purge."
            )
        await message.reply_text(text)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        config = Config.from_env()
    except ConfigError as error:
        sys.exit(f"uruk: {error}")
    bot = UrukBot(config)
    app = Application.builder().token(config.token).post_shutdown(bot.shutdown).build()
    bot.attach(app)
    if config.configured:
        logger.info(
            "starting (chat=%s, owner=%s, repos_root=%s, mode=%s)",
            config.chat_id, config.owner_id, config.repos_root, config.permission_mode,
        )
    else:
        logger.warning("TELEGRAM_CHAT_ID / TELEGRAM_OWNER_ID not set — setup mode: only /id will respond.")
    app.run_polling(allowed_updates=["message", "callback_query"])

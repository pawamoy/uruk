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

"""Regression tests for Telegram conversation waits and recovery."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from telegram import CallbackQuery, Chat, Message, MessageEntity, Update, User
from telegram.error import TelegramError
from telegram.ext import Application

from uruk import ClaudeAgentTask, Config, ConfigError, TaskInfo, UrukBot
from uruk._internal import bot as bot_module
from uruk._internal.agent import _InteractionCancelledError
from uruk._internal.bot import _AttachCandidate

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@pytest.fixture
def bot(tmp_path: Path) -> UrukBot:
    config = Config(token="test", chat_id=-123, owner_id=456, repos_root=tmp_path, permission_mode="default", model=None, data_dir=tmp_path, repo_prefixes={}, interaction_timeout=10)  # noqa: S106
    bot = UrukBot(config)
    bot.app = MagicMock()
    bot.app.bot = AsyncMock()
    bot.app.bot.send_message.return_value = _message(bot, "sent", 42)
    return bot


def _message(bot: UrukBot, text: str, topic_id: int | None) -> Message:
    message = Message(message_id=10, date=datetime.now(UTC), chat=Chat(-123, "supergroup"), from_user=User(456, "Owner", is_bot=False), text=text, message_thread_id=topic_id)
    message.set_bot(bot._application().bot)
    return message


def _update(bot: UrukBot, text: str = "hello", topic_id: int | None = 42) -> Update:
    return Update(update_id=1, message=_message(bot, text, topic_id))


def _button(bot: UrukBot, data: str, topic_id: int | None = 42) -> Update:
    query = CallbackQuery(id="button", from_user=User(456, "Owner", is_bot=False), chat_instance="chat", message=_message(bot, "question", topic_id), data=data)
    query.set_bot(bot._application().bot)
    return Update(update_id=2, callback_query=query)


async def _wait_until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(1):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0)


async def _answer(bot: UrukBot, answer: str, topic_id: int | None = 42) -> None:
    await _wait_until(lambda: bool(bot.pending))
    pid = next(iter(bot.pending))
    await bot._on_button(_button(bot, f"q:{pid}:{answer}", topic_id), MagicMock())


@pytest.mark.parametrize(
    ("command", "expected_model"),
    [
        ("astra", "gpt-6-astra"),
        ("sol", "gpt-6.1-sol"),
        ("luna", "gpt-6-luna"),
    ],
)
def test_codex_model_commands_start_latest_models(bot: UrukBot, command: str, expected_model: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        repo = bot.config.data_dir / "project"
        repo.mkdir()
        started = AsyncMock()
        monkeypatch.setattr(bot, "_start_session", started)

        await bot._cmd_new_model(_update(bot, f"/{command} project Review this diff", 1), MagicMock())

        started.assert_awaited_once()
        assert started.await_args is not None
        assert started.await_args.args[1:3] == ("openai", expected_model)

    asyncio.run(scenario())


def test_attach_handler_returns_before_the_selection(bot: UrukBot, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        candidate = _AttachCandidate("claude", "session", str(bot.config.data_dir), "task")
        monkeypatch.setattr(bot, "_discover_attach_candidates", AsyncMock(return_value=[candidate]))
        attached = AsyncMock()
        monkeypatch.setattr(bot, "_attach_candidate", attached)

        # Sequential dispatch must be free to process the user's next button press.
        await asyncio.wait_for(bot._cmd_attach(_update(bot, "/attach", 1), MagicMock()), 0.2)
        await _wait_until(lambda: bool(bot.pending))
        pid = next(iter(bot.pending))
        await bot._on_button(_button(bot, f"x:{pid}:0", 1), MagicMock())
        await _wait_until(lambda: not bot._flow_running(None))

        attached.assert_awaited_once()
        assert attached.await_args is not None
        assert attached.await_args.args[1] == candidate
        assert not bot.pending

    asyncio.run(scenario())


def test_prompt_dialogue_handles_general_topic_ids(bot: UrukBot, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        repo = bot.config.data_dir / "project"
        repo.mkdir()
        started = AsyncMock()
        monkeypatch.setattr(bot, "_start_session", started)

        await bot._on_button(_button(bot, "c:prompt", 1), MagicMock())
        await _answer(bot, "0", None)
        await _wait_until(lambda: bool(bot.pending))
        await _answer(bot, "0", 1)
        await _wait_until(lambda: None in bot.awaiting_text)
        await bot._on_text(_update(bot, "project", 1), MagicMock())
        await _answer(bot, "0", 1)
        await _wait_until(lambda: None in bot.awaiting_text)
        await bot._on_text(_update(bot, "Fix the bug", None), MagicMock())
        await _wait_until(lambda: not bot._flow_running(1))

        started.assert_awaited_once()
        assert started.await_args is not None
        assert started.await_args.args[4:] == (str(repo), "Fix the bug")
        assert not bot.pending
        assert not bot.awaiting_text
        assert all(call.kwargs.get("message_thread_id") != 1 for call in bot._application().bot.send_message.await_args_list)

    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["prompt", "auto", "attach"])
def test_setup_flows_cannot_overlap(bot: UrukBot, command: str) -> None:
    async def scenario() -> None:
        await bot._on_button(_button(bot, "c:prompt", 1), MagicMock())
        await _wait_until(lambda: bool(bot.pending))
        original = next(iter(bot.pending))

        await bot._on_button(_button(bot, f"c:{command}", None), MagicMock(args=[]))

        assert list(bot.pending) == [original]
        assert len(bot.prompt_dialogues) == 1
        assert not bot.auto_runs
        assert not bot._attach_runs
        await bot._cmd_cancel(_update(bot, "/cancel", 1), MagicMock())
        assert not bot.pending

    asyncio.run(scenario())


@pytest.mark.parametrize("arg", ["-1", "99", "invalid", "done"])
def test_invalid_question_buttons_keep_the_question_answerable(bot: UrukBot, arg: str) -> None:
    async def scenario() -> None:
        question = asyncio.create_task(bot.ask_question(42, {"question": "Choose", "options": [{"label": "Yes"}]}))
        await _wait_until(lambda: bool(bot.pending))
        pid = next(iter(bot.pending))

        await bot._on_button(_button(bot, f"q:{pid}:{arg}"), MagicMock())
        assert not question.done()
        await _answer(bot, "0")

        assert await question == "Yes"
        assert not bot.pending

    asyncio.run(scenario())


def test_button_kind_and_topic_must_match_the_interaction(bot: UrukBot) -> None:
    async def scenario() -> None:
        question = asyncio.create_task(bot.ask_question(42, {"question": "Choose", "options": [{"label": "Yes"}]}))
        await _wait_until(lambda: bool(bot.pending))
        pid = next(iter(bot.pending))

        await bot._on_button(_button(bot, f"p:{pid}:a"), MagicMock())
        await bot._on_button(_button(bot, f"q:{pid}:0", 43), MagicMock())
        assert not question.done()
        await _answer(bot, "0")

        assert await question == "Yes"

    asyncio.run(scenario())


def test_multiselect_and_free_text_answers(bot: UrukBot) -> None:
    async def scenario() -> None:
        question_data = {"question": "Choose", "options": [{"label": "A"}, {"label": "B"}], "multiSelect": True}
        selected = asyncio.create_task(bot.ask_question(42, question_data))
        await _answer(bot, "0")
        await _answer(bot, "1")
        await _answer(bot, "0")
        await _answer(bot, "done")
        assert await selected == "B"

        typed = asyncio.create_task(bot.ask_question(42, question_data))
        await _answer(bot, "other")
        await bot._on_text(_update(bot, "My own answer"), MagicMock())

        assert await typed == "My own answer"
        assert not bot.awaiting_text
        assert not bot.pending

    asyncio.run(scenario())


@pytest.mark.parametrize("other", [False, True])
def test_setup_question_timeout_removes_both_waiters(bot: UrukBot, other: bool) -> None:
    async def scenario() -> None:
        bot.config = replace(bot.config, interaction_timeout=0.02)
        question = asyncio.create_task(bot.ask_question(None, {"question": "Choose"}))
        if other:
            await _answer(bot, "other", None)

        with pytest.raises(TimeoutError):
            await question

        assert not bot.pending
        assert not bot.awaiting_text
        bot._application().bot.edit_message_reply_markup.assert_awaited()

    asyncio.run(scenario())


def test_cancel_other_does_not_consume_the_next_prompt(bot: UrukBot) -> None:
    async def scenario() -> None:
        question = asyncio.create_task(bot.ask_question(42, {"question": "Choose"}))
        await _answer(bot, "other")
        task = MagicMock(info=TaskInfo(42, str(bot.config.data_dir), "task"))
        task.submit = AsyncMock()
        bot.tasks[42] = task

        await bot._cmd_cancel(_update(bot, "/cancel"), MagicMock())
        with pytest.raises(_InteractionCancelledError):
            await question
        await bot._on_text(_update(bot, "New prompt"), MagicMock())

        task.submit.assert_awaited_once_with("New prompt")
        assert not bot.awaiting_text

    asyncio.run(scenario())


@pytest.mark.parametrize("other", [False, True])
def test_agent_questions_remain_pending_past_the_setup_timeout(bot: UrukBot, other: bool) -> None:
    async def scenario() -> None:
        bot.config = replace(bot.config, interaction_timeout=0.01)
        question = asyncio.create_task(bot.ask_question(42, {"question": "Choose", "options": [{"label": "Yes"}]}))
        await _wait_until(lambda: bool(bot.pending))
        if other:
            await _answer(bot, "other")

        # Leaving the phone unattended must keep both button and typed answers usable.
        await asyncio.sleep(0.05)

        assert not question.done()
        assert len(bot.pending) == 1
        if other:
            assert 42 in bot.awaiting_text
            await bot._on_text(_update(bot, "Yes"), MagicMock())
        else:
            await _answer(bot, "0")
        assert await question == "Yes"
        assert not bot.pending
        assert not bot.awaiting_text

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_approvals_remain_pending_until_answered_or_cancelled(bot: UrukBot, cancel: bool) -> None:
    async def scenario() -> None:
        bot.config = replace(bot.config, interaction_timeout=0.01)
        permission = asyncio.create_task(bot.ask_permission(42, "Bash", {"command": "pwd"}))
        await _wait_until(lambda: bool(bot.pending))

        await asyncio.sleep(0.05)

        assert not permission.done()
        if cancel:
            await bot._cmd_cancel(_update(bot, "/cancel"), MagicMock())
        else:
            pid = next(iter(bot.pending))
            await bot._on_button(_button(bot, f"p:{pid}:a"), MagicMock())

        assert await permission is (not cancel)
        assert not bot.pending

    asyncio.run(scenario())


@pytest.mark.parametrize("text_only", [False, True])
def test_send_failure_does_not_leave_a_waiter(bot: UrukBot, text_only: bool) -> None:
    async def scenario() -> None:
        bot._application().bot.send_message.side_effect = TelegramError("thread not found")

        request = bot._request_dialogue_text(42, "Type an answer") if text_only else bot.ask_question(42, {"question": "Choose"})
        with pytest.raises(RuntimeError, match="could not send"):
            await request

        assert not bot.pending
        assert not bot.awaiting_text

    asyncio.run(scenario())


def test_prompt_timeout_restores_the_command_panel(bot: UrukBot) -> None:
    async def scenario() -> None:
        bot.config = replace(bot.config, interaction_timeout=0.01)
        await bot._on_button(_button(bot, "c:prompt", 1), MagicMock())
        flow = bot.prompt_dialogues[None]

        await flow

        texts = [call.kwargs["text"] for call in bot._application().bot.send_message.await_args_list]
        assert any("timed out" in text for text in texts)
        assert texts[-1] == "Command panel"
        assert not bot.pending
        assert not bot.prompt_dialogues

    asyncio.run(scenario())


def test_auto_timeout_does_not_skip_the_unanswered_issue(bot: UrukBot, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        bot.config = replace(bot.config, interaction_timeout=0.01)
        issue = SimpleNamespace(repository="owner/project", number=1, title="Fix", is_pr=False)
        monkeypatch.setattr(bot, "_next_backlog_items", AsyncMock(return_value=[issue]))
        monkeypatch.setattr(bot, "_repo_for_backlog_issue", lambda issue: bot.config.data_dir)

        await bot._cmd_auto(_update(bot, "/auto", 1), MagicMock(args=[]))
        await bot.auto_runs[None]

        assert not bot.backlog_skipped
        assert not bot.backlog_started
        assert not bot.pending
        assert not bot.auto_runs

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_release_is_nonblocking_and_failed_handoffs_restore_ownership(bot: UrukBot, cancel: bool) -> None:
    async def scenario() -> None:
        bot.config = replace(bot.config, interaction_timeout=0.02)
        info = TaskInfo(42, str(bot.config.data_dir), "task", session_id="session")
        bot.store.set(info)
        task = MagicMock(info=info, turn_running=True)
        task.close = AsyncMock()
        bot.tasks[42] = task

        await asyncio.wait_for(bot._cmd_release(_update(bot, "/release"), MagicMock()), 0.2)
        flow = bot._release_runs[42]
        await _wait_until(lambda: info.owner == "transferring")
        if cancel:
            await bot._cmd_cancel(_update(bot, "/cancel"), MagicMock())
        else:
            await flow

        assert info.owner == "telegram"
        assert bot.store.get(42) is info
        task.close.assert_not_awaited()
        assert not bot._release_runs

    asyncio.run(scenario())


def test_release_finishes_after_the_turn(bot: UrukBot) -> None:
    async def scenario() -> None:
        info = TaskInfo(42, str(bot.config.data_dir), "task", session_id="session")
        bot.store.set(info)
        task = MagicMock(info=info, turn_running=True)
        task.close = AsyncMock()
        bot.tasks[42] = task

        await bot._cmd_release(_update(bot, "/release"), MagicMock())
        flow = bot._release_runs[42]
        await _wait_until(lambda: info.owner == "transferring")
        task.turn_running = False
        await flow

        assert info.owner == "terminal"
        task.close.assert_awaited_once()
        assert 42 not in bot.tasks

    asyncio.run(scenario())


def test_restart_recovers_an_interrupted_handoff(bot: UrukBot) -> None:
    bot.store.set(TaskInfo(42, str(bot.config.data_dir), "task", owner="transferring"))

    restarted = UrukBot(bot.config)

    restored = restarted.store.get(42)
    assert restored is not None
    assert restored.owner == "telegram"


def test_claude_does_not_invent_an_answer_when_a_question_is_cancelled(bot: UrukBot) -> None:
    async def scenario() -> None:
        ui = AsyncMock()
        ui.ask_question.side_effect = _InteractionCancelledError("cancelled")
        agent = ClaudeAgentTask(ui, TaskInfo(42, str(bot.config.data_dir), "task"), permission_mode="default", default_model=None, on_state_change=lambda: None)

        result = await agent._on_permission("AskUserQuestion", {"questions": [{"question": "Choose"}]}, MagicMock())

        assert isinstance(result, PermissionResultDeny)
        assert "Do not assume an answer" in result.message

    asyncio.run(scenario())


def test_claude_accepts_an_answer_after_both_uruk_timeouts(bot: UrukBot, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        bot.config = replace(bot.config, interaction_timeout=0.01)
        monkeypatch.setattr(bot_module, "_UPDATE_TIMEOUT", 0.01)
        agent = ClaudeAgentTask(bot, TaskInfo(42, str(bot.config.data_dir), "task"), permission_mode="default", default_model=None, on_state_change=lambda: None)
        request = asyncio.create_task(agent._on_permission("AskUserQuestion", {"questions": [{"question": "Choose", "options": [{"label": "Yes"}]}]}, MagicMock()))
        await _wait_until(lambda: bool(bot.pending))

        # Agent input waits run outside Telegram's command dispatcher.
        await asyncio.sleep(0.05)

        assert not request.done()
        pid = next(iter(bot.pending))
        await bot._bounded_handler(bot._on_button)(_button(bot, f"q:{pid}:0"), MagicMock())
        result = await request

        assert isinstance(result, PermissionResultAllow)
        assert result.updated_input is not None
        assert result.updated_input["answers"] == {"Choose": "Yes"}

    asyncio.run(scenario())


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "abc"])
def test_interaction_timeout_requires_finite_positive_seconds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    monkeypatch.setenv("URUK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("URUK_INTERACTION_TIMEOUT", value)

    with pytest.raises(ConfigError, match="URUK_INTERACTION_TIMEOUT"):
        Config.from_env()


def test_sequential_telegram_dispatch_can_finish_attach(bot: UrukBot, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        app = Application.builder().token("123:test").build()
        app._initialized = True
        app.bot._bot_user = User(999, "Uruk", is_bot=True, username="uruk")
        monkeypatch.setattr(type(app.bot), "send_message", AsyncMock(return_value=_message(bot, "sent", None)))
        monkeypatch.setattr(type(app.bot), "answer_callback_query", AsyncMock())
        monkeypatch.setattr(type(app.bot), "edit_message_text", AsyncMock())
        bot.attach(app)
        candidate = _AttachCandidate("claude", "session", str(bot.config.data_dir), "task")
        monkeypatch.setattr(bot, "_discover_attach_candidates", AsyncMock(return_value=[candidate]))
        attached = AsyncMock()
        monkeypatch.setattr(bot, "_attach_candidate", attached)
        command = Message(message_id=10, date=datetime.now(UTC), chat=Chat(-123, "supergroup"), from_user=User(456, "Owner", is_bot=False), text="/attach", entities=[MessageEntity("bot_command", 0, 7)], message_thread_id=1)
        command.set_bot(app.bot)

        # Both updates use PTB's default sequential dispatcher, with no network calls.
        await asyncio.wait_for(app.process_update(Update(update_id=1, message=command)), 0.2)
        await _wait_until(lambda: bool(bot.pending))
        pid = next(iter(bot.pending))
        await app.process_update(_button(bot, f"x:{pid}:0", 1))
        await _wait_until(lambda: not bot._flow_running(None))

        attached.assert_awaited_once()
        assert not bot.pending

    asyncio.run(scenario())


def test_stalled_sdk_command_times_out_and_next_command_works(bot: UrukBot, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(bot_module, "_UPDATE_TIMEOUT", 0.01)
        task = MagicMock()

        async def stalled_interrupt() -> bool:
            await asyncio.Event().wait()
            return True

        task.interrupt = AsyncMock(side_effect=stalled_interrupt)
        bot.tasks[42] = task
        interrupt = bot._bounded_handler(bot._cmd_interrupt)

        await interrupt(_update(bot, "/interrupt"), MagicMock())
        await bot._bounded_handler(bot._cmd_help)(_update(bot, "/help"), MagicMock())

        texts = [call.kwargs["text"] for call in bot._application().bot.send_message.await_args_list]
        assert "timed out" in texts[0]
        assert "/cancel" in texts[-1]

    asyncio.run(scenario())


def test_parallel_questions_cannot_overwrite_a_text_answer_wait(bot: UrukBot) -> None:
    async def scenario() -> None:
        first = asyncio.create_task(bot.ask_question(42, {"question": "First", "options": [{"label": "A"}]}))
        await _wait_until(lambda: len(bot.pending) == 1)
        first_pid = next(iter(bot.pending))
        second = asyncio.create_task(bot.ask_question(42, {"question": "Second", "options": [{"label": "B"}]}))
        await _wait_until(lambda: len(bot.pending) == 2)
        second_pid = max(bot.pending)

        await bot._on_button(_button(bot, f"q:{first_pid}:other"), MagicMock())
        await bot._on_button(_button(bot, f"q:{second_pid}:other"), MagicMock())
        await bot._on_text(_update(bot, "First answer"), MagicMock())
        assert await first == "First answer"
        assert not second.done()
        await bot._on_button(_button(bot, f"q:{second_pid}:0"), MagicMock())

        assert await second == "B"
        assert not bot.pending
        assert not bot.awaiting_text

    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["interrupt", "close"])
def test_task_commands_clean_up_other_answers(bot: UrukBot, command: str) -> None:
    async def scenario() -> None:
        info = TaskInfo(42, str(bot.config.data_dir), "task")
        bot.store.set(info)
        task = MagicMock(info=info)
        task.close = AsyncMock()
        task.interrupt = AsyncMock(return_value=True)
        bot.tasks[42] = task
        question = asyncio.create_task(bot.ask_question(42, {"question": "Choose"}))
        await _answer(bot, "other")

        handler = bot._cmd_interrupt if command == "interrupt" else bot._cmd_close
        await handler(_update(bot, f"/{command}"), MagicMock())

        with pytest.raises(_InteractionCancelledError):
            await question
        assert not bot.pending
        assert not bot.awaiting_text

    asyncio.run(scenario())


def test_shutdown_joins_dialogues_and_cleans_waiters(bot: UrukBot) -> None:
    async def scenario() -> None:
        await bot._on_button(_button(bot, "c:prompt", 1), MagicMock())
        await _wait_until(lambda: bool(bot.pending))
        question = asyncio.create_task(bot.ask_question(42, {"question": "Choose"}))
        await _wait_until(lambda: len(bot.pending) == 2)

        await bot.shutdown(bot._application())

        with pytest.raises(_InteractionCancelledError):
            await question
        assert not bot.pending
        assert not bot.awaiting_text
        assert not bot.prompt_dialogues
        texts = [call.kwargs["text"] for call in bot._application().bot.send_message.await_args_list]
        assert "Command panel" not in texts

    asyncio.run(scenario())


def test_unexpected_text_does_not_enter_the_agent_queue(bot: UrukBot) -> None:
    async def scenario() -> None:
        task = MagicMock(info=TaskInfo(42, str(bot.config.data_dir), "task"))
        task.submit = AsyncMock()
        bot.tasks[42] = task
        question = asyncio.create_task(bot.ask_question(42, {"question": "Choose", "options": [{"label": "A"}]}))
        await _wait_until(lambda: bool(bot.pending))

        await bot._on_text(_update(bot, "Unexpected answer"), MagicMock())
        task.submit.assert_not_awaited()
        assert not question.done()
        await _answer(bot, "0")
        assert await question == "A"
        await bot._on_text(_update(bot, "Next prompt"), MagicMock())

        task.submit.assert_awaited_once_with("Next prompt")

    asyncio.run(scenario())

# SPDX-License-Identifier: ISC
# Copyright (c) 2026, Timothée Mazzucotelli and contributors

"""Durable delivery, recovery, ordering, and Telegram shortcut regressions."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.error import BadRequest, RetryAfter, TelegramError, TimedOut

from uruk._internal.outbox import _DeliveryError, _DurableBot, _Outbox

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


async def _wait_until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(3):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0)


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_caller_cancellation_leaves_delivery_running(tmp_path: Path) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        finish = asyncio.Event()

        async def send(operation: str, data: dict) -> dict:
            assert operation == "sendMessage"
            assert data["text"] == "Do not lose this answer"
            started.set()
            await finish.wait()
            return {"message_id": 10}

        outbox = _Outbox(tmp_path / "outbox.sqlite3", send, AsyncMock())
        request = asyncio.create_task(outbox.send("sendMessage", {"chat_id": -123, "text": "Do not lose this answer"}))
        await started.wait()

        # The message is on disk before the network responds. Cancelling the
        # handler must not cancel the independent delivery attempt.
        assert len(outbox.pending()) == 1
        await _cancel(request)
        finish.set()
        await _wait_until(lambda: not outbox.pending())

        await outbox.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["timeout", "server", "flood"])
def test_network_failures_retry_beyond_three_attempts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    monkeypatch.setenv("PTB_TIMEDELTA", "1")
    error = {"timeout": TimedOut(), "server": TelegramError("Invalid server response"), "flood": RetryAfter(0)}[failure]
    async def scenario() -> None:
        sender = AsyncMock(side_effect=[error for _ in range(6)] + [{"message_id": 10}])
        outbox = _Outbox(tmp_path / "outbox.sqlite3", sender, AsyncMock())
        failed = outbox._failed
        monkeypatch.setattr(outbox, "_failed", lambda row, error, delay: failed(row, error, 0))

        result = await asyncio.wait_for(outbox.send("sendMessage", {"chat_id": -123, "text": "Answer"}), 3)

        assert result == {"message_id": 10}
        assert sender.await_count == 7
        assert not outbox.pending()
        await outbox.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(("operation", "field"), [("sendDocument", "document"), ("sendPhoto", "photo")])
def test_files_survive_cancellation_and_restart(tmp_path: Path, operation: str, field: str) -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        async def stalled_send(operation: str, data: dict) -> None:  # noqa: ARG001
            started.set()
            await asyncio.Event().wait()

        path = tmp_path / "outbox.sqlite3"
        outbox = _Outbox(path, stalled_send, AsyncMock())
        request = asyncio.create_task(outbox.send(operation, {"chat_id": -123, field: InputFile(b"original attachment bytes", filename="result.bin"), "caption": "Result"}))
        await started.wait()
        await outbox.stop()
        await _cancel(request)

        # A fresh queue instance represents a new bot process with no old tasks.
        sender = AsyncMock(return_value={"message_id": 20})
        restarted = _Outbox(path, sender, AsyncMock())
        restarted.start()
        await _wait_until(lambda: not restarted.pending())

        sender.assert_awaited_once()
        assert sender.await_args is not None
        assert sender.await_args.args[0] == operation
        file = sender.await_args.args[1][field]
        assert isinstance(file, InputFile)
        assert file.input_file_content == b"original attachment bytes"
        assert file.filename == "result.bin"
        assert sender.await_args.args[1]["caption"] == "Result"
        await restarted.stop()

    asyncio.run(scenario())


def test_whole_answer_is_saved_before_rendering(tmp_path: Path) -> None:
    async def scenario() -> None:
        rendering = asyncio.Event()

        async def stalled_prepare(data: dict) -> list:  # noqa: ARG001
            rendering.set()
            await asyncio.Event().wait()
            return []

        path = tmp_path / "outbox.sqlite3"
        outbox = _Outbox(path, AsyncMock(), stalled_prepare)
        request = asyncio.create_task(outbox.send("renderText", {"chat_id": -123, "text": "The entire original answer"}))
        await rendering.wait()

        with contextlib.closing(sqlite3.connect(path)) as connection:
            payload = connection.execute("SELECT payload FROM outbox").fetchone()[0]
        assert "The entire original answer" in payload

        await outbox.stop()
        await _cancel(request)
        prepare = AsyncMock(return_value=[("sendMessage", {"chat_id": -123, "text": "Recovered answer"})])
        sender = AsyncMock(return_value={"message_id": 10})
        restarted = _Outbox(path, sender, prepare)
        restarted.start()
        await _wait_until(lambda: not restarted.pending())

        assert prepare.await_args is not None
        assert prepare.await_args.args[0]["text"] == "The entire original answer"
        sender.assert_awaited_once_with("sendMessage", {"chat_id": -123, "text": "Recovered answer"})
        await restarted.stop()

    asyncio.run(scenario())


def test_restart_resumes_only_unacknowledged_parts(tmp_path: Path) -> None:
    async def scenario() -> None:
        sending_second = asyncio.Event()
        delivered = []

        async def send(operation: str, data: dict) -> dict:  # noqa: ARG001
            if data["text"] == "Second":
                sending_second.set()
                await asyncio.Event().wait()
            delivered.append(data["text"])
            return {"message_id": 10}

        parts = [("sendMessage", {"chat_id": -123, "text": text}) for text in ("First", "Second", "Third")]
        path = tmp_path / "outbox.sqlite3"
        outbox = _Outbox(path, send, AsyncMock(return_value=parts))
        request = asyncio.create_task(outbox.send("renderText", {"chat_id": -123, "text": "Original"}))
        await sending_second.wait()
        await outbox.stop()
        await _cancel(request)

        assert delivered == ["First"]
        sender = AsyncMock(return_value={"message_id": 20})
        prepare = AsyncMock(side_effect=AssertionError("Saved parts must not be rendered again"))
        restarted = _Outbox(path, sender, prepare)
        restarted.start()
        await _wait_until(lambda: not restarted.pending())

        assert [call.args[1]["text"] for call in sender.await_args_list] == ["Second", "Third"]
        prepare.assert_not_awaited()
        await restarted.stop()

    asyncio.run(scenario())


def test_permanent_errors_remain_inspectable_and_can_be_retried(tmp_path: Path) -> None:
    async def scenario() -> None:
        sender = AsyncMock(side_effect=BadRequest("message thread not found"))
        outbox = _Outbox(tmp_path / "outbox.sqlite3", sender, AsyncMock())

        with pytest.raises(_DeliveryError, match="retained"):
            await outbox.send("sendMessage", {"chat_id": -123, "text": "Original answer"})

        retained = outbox.pending()
        assert len(retained) == 1
        assert retained[0]["state"] == "blocked"
        assert "thread not found" in retained[0]["last_error"]

        sender.side_effect = None
        sender.return_value = {"message_id": 10}
        assert outbox.retry() == 1
        await _wait_until(lambda: not outbox.pending())

        assert sender.await_args is not None
        assert sender.await_args.args[1]["text"] == "Original answer"
        await outbox.stop()

    asyncio.run(scenario())


def test_topic_order_is_preserved_while_other_topics_can_progress(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls = []

        async def send(operation: str, data: dict) -> dict:  # noqa: ARG001
            calls.append(data["text"])
            if data["text"] == "First":
                raise TimedOut
            return {"message_id": 10}

        outbox = _Outbox(tmp_path / "outbox.sqlite3", send, AsyncMock())
        first = asyncio.create_task(outbox.send("sendMessage", {"chat_id": -123, "message_thread_id": 42, "text": "First"}))
        await _wait_until(lambda: calls == ["First"])
        second = asyncio.create_task(outbox.send("sendMessage", {"chat_id": -123, "message_thread_id": 42, "text": "Second"}))
        other = await outbox.send("sendMessage", {"chat_id": -123, "message_thread_id": 99, "text": "Other topic"})

        assert other == {"message_id": 10}
        assert calls == ["First", "Other topic"]
        await outbox.stop()
        await _cancel(first)
        await _cancel(second)

    asyncio.run(scenario())


def test_a_blocked_answer_can_be_recovered_in_another_topic(tmp_path: Path) -> None:
    async def scenario() -> None:
        sender = AsyncMock(side_effect=BadRequest("message thread not found"))
        prepare = AsyncMock(return_value=[("sendMessage", {"chat_id": -123, "message_thread_id": 42, "text": "Original answer", "reply_parameters": {"message_id": 999}})])
        outbox = _Outbox(tmp_path / "outbox.sqlite3", sender, prepare)

        with pytest.raises(_DeliveryError):
            await outbox.send("renderText", {"chat_id": -123, "message_thread_id": 42, "text": "Original answer"})

        delivery_id = outbox.pending()[0]["id"]
        sender.side_effect = None
        sender.return_value = {"message_id": 10}

        assert outbox.resend(delivery_id, -123, 1)
        await _wait_until(lambda: not outbox.pending())

        assert sender.await_args is not None
        assert sender.await_args.args[1] == {"chat_id": -123, "message_thread_id": None, "text": "Original answer"}
        assert not outbox.resend(delivery_id, -123, 99)
        await outbox.stop()

    asyncio.run(scenario())


def test_cancelling_an_in_flight_question_removes_its_buttons_after_delivery(tmp_path: Path) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        finish = asyncio.Event()
        calls = []

        async def send(operation: str, data: dict) -> dict:
            calls.append((operation, data))
            if operation == "sendMessage":
                started.set()
                await finish.wait()
            return {"message_id": 10}

        outbox = _Outbox(tmp_path / "outbox.sqlite3", send, AsyncMock())
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Skip", callback_data="a:123:skip")]])
        request = asyncio.create_task(outbox.send("sendMessage", {"chat_id": -123, "text": "Start this issue?", "reply_markup": markup}))
        await started.wait()

        await _cancel(request)
        finish.set()
        await _wait_until(lambda: not outbox.pending())

        assert [operation for operation, _ in calls] == ["sendMessage", "editMessageReplyMarkup"]
        assert calls[1][1] == {"chat_id": -123, "message_id": 10}
        await outbox.stop()

    asyncio.run(scenario())


def test_replayed_questions_do_not_resurrect_old_buttons(tmp_path: Path) -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        async def stalled_send(operation: str, data: dict) -> None:  # noqa: ARG001
            started.set()
            await asyncio.Event().wait()

        path = tmp_path / "outbox.sqlite3"
        outbox = _Outbox(path, stalled_send, AsyncMock())
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Allow", callback_data="p:123:a")]])
        request = asyncio.create_task(outbox.send("sendMessage", {"chat_id": -123, "text": "Allow this?", "reply_markup": markup}))
        await started.wait()
        await outbox.stop()
        await _cancel(request)

        sender = AsyncMock(return_value={"message_id": 10})
        restarted = _Outbox(path, sender, AsyncMock())
        restarted.start()
        await _wait_until(lambda: not restarted.pending())

        assert sender.await_args is not None
        replay = sender.await_args.args[1]
        assert "reply_markup" not in replay
        assert "Allow this?" in replay["text"]
        assert "interaction has ended" in replay["text"]
        await restarted.stop()

    asyncio.run(scenario())


def test_direct_sends_and_message_shortcuts_both_use_durable_delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def send(operation: str, data: dict) -> dict:
            return await bot.deliver(operation, data)

        outbox = _Outbox(tmp_path / "outbox.sqlite3", send, AsyncMock())
        bot = _DurableBot(token="123:test", outbox=outbox)  # noqa: S106
        requests = []

        async def post(*_args: object, **kwargs: object) -> dict:
            # Both entry points must commit their payload before reaching HTTP.
            assert len(outbox.pending()) == 1
            requests.append(kwargs["request_data"])
            return {"message_id": 10, "date": 0, "chat": {"id": -123, "type": "supergroup"}, "text": "Answer"}

        monkeypatch.setattr(type(bot.request), "post", post)
        message = await bot.send_message(chat_id=-123, text="Answer")
        reply = await message.reply_text("Reply")
        await bot.send_document(chat_id=-123, document=b"file bytes", filename="result.txt")

        assert reply.message_id == 10
        assert len(requests) == 3
        assert not outbox.pending()
        await outbox.stop()

    asyncio.run(scenario())

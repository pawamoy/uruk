# SPDX-License-Identifier: ISC
# Copyright (c) 2026, Timothée Mazzucotelli and contributors

"""Regression tests for shared Telegram pacing and flood-control recovery."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import ExtBot

from uruk._internal import rate_limiter as limiter_module
from uruk._internal.rate_limiter import _TelegramRateLimiter

pytestmark = pytest.mark.filterwarnings("ignore:.*attribute `retry_after`.*:telegram.warnings.PTBDeprecationWarning")


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(limiter_module, "monotonic", lambda: clock.now)
    monkeypatch.setattr(limiter_module, "sleep", clock.sleep)
    return clock


@pytest.mark.parametrize("endpoint", ["sendMessage", "sendDocument", "sendPhoto", "editMessageText"])
def test_concurrent_topics_share_the_group_budget(clock: _Clock, endpoint: str) -> None:
    async def scenario() -> None:
        limiter = _TelegramRateLimiter()
        sent_at = []

        async def send() -> bool:
            sent_at.append(clock.now)
            return True

        # Different topics and output types must not create independent budgets.
        results = await asyncio.gather(
            limiter.process_request(send, (), {}, "sendMessage", {"chat_id": -123, "message_thread_id": 42}, None),
            limiter.process_request(send, (), {}, endpoint, {"chat_id": -123, "message_thread_id": 99}, None),
        )

        assert results == [True, True]
        assert sent_at == pytest.approx([100, 103.1])

    asyncio.run(scenario())


def test_message_replies_use_the_same_limiter_as_direct_sends(clock: _Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        bot = ExtBot(token="123:test", rate_limiter=_TelegramRateLimiter())  # noqa: S106
        sent_at = []

        async def post(*_args: object, **_kwargs: object) -> dict:
            sent_at.append(clock.now)
            return {"message_id": 10, "date": 0, "chat": {"id": -123, "type": "supergroup"}, "text": "Answer", "message_thread_id": 42, "is_topic_message": True}

        # Stub the transport so real ExtBot and Message methods run without a network.
        monkeypatch.setattr(type(bot.request), "post", post)
        message = await bot.send_message(chat_id=-123, text="Answer", message_thread_id=42)
        reply = await message.reply_text("Reply")

        assert reply.message_thread_id == 42
        assert sent_at == pytest.approx([100, 103.1])

    asyncio.run(scenario())


@pytest.mark.parametrize("use_timedelta", [False, True])
def test_flood_control_waits_then_retries_the_original_request(clock: _Clock, monkeypatch: pytest.MonkeyPatch, use_timedelta: bool) -> None:
    if use_timedelta:
        monkeypatch.setenv("PTB_TIMEDELTA", "1")
    else:
        monkeypatch.delenv("PTB_TIMEDELTA", raising=False)
    retry_after = timedelta(seconds=15) if use_timedelta else 15
    callback = AsyncMock(side_effect=[RetryAfter(retry_after), {"message_id": 10}])
    limiter = _TelegramRateLimiter()

    result = asyncio.run(limiter.process_request(callback, ("original",), {"text": "Answer"}, "sendMessage", {"chat_id": -123}, None))

    assert result == {"message_id": 10}
    assert clock.now == pytest.approx(115.1)
    assert callback.await_count == 2
    for call in callback.await_args_list:
        assert call.args == ("original",)
        assert call.kwargs == {"text": "Answer"}


def test_retries_are_bounded_and_keep_the_shared_cooldown(clock: _Clock) -> None:
    async def scenario() -> None:
        limiter = _TelegramRateLimiter()
        rejected = AsyncMock(side_effect=RetryAfter(10))

        with pytest.raises(RetryAfter):
            await limiter.process_request(rejected, (), {}, "sendMessage", {"chat_id": -123}, None)

        assert rejected.await_count == 4

        # Even after the failed request gives up, another topic must wait.
        sent_at = []

        async def send() -> bool:
            sent_at.append(clock.now)
            return True

        await limiter.process_request(send, (), {}, "sendMessage", {"chat_id": -123, "message_thread_id": 99}, None)

        assert sent_at == pytest.approx([140.4])

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [TimedOut(), NetworkError("connection lost"), BadRequest("thread not found")])
def test_other_errors_are_not_retried(clock: _Clock, error: Exception) -> None:
    limiter = _TelegramRateLimiter()
    callback = AsyncMock(side_effect=error)

    with pytest.raises(type(error)):
        asyncio.run(limiter.process_request(callback, (), {}, "sendMessage", {"chat_id": -123}, None))

    callback.assert_awaited_once()
    assert clock.now == 100


def test_cancelling_a_retry_keeps_other_topics_in_cooldown(clock: _Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        limiter = _TelegramRateLimiter()
        sleeping = asyncio.Event()

        async def hold_sleep(seconds: float) -> None:  # noqa: ARG001
            sleeping.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(limiter_module, "sleep", hold_sleep)
        rejected = AsyncMock(side_effect=RetryAfter(15))
        request = asyncio.create_task(limiter.process_request(rejected, (), {}, "sendMessage", {"chat_id": -123}, None))
        await asyncio.wait_for(sleeping.wait(), 1)

        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

        # The next request can acquire the lock, but must still honor the deadline.
        monkeypatch.setattr(limiter_module, "sleep", clock.sleep)
        callback = AsyncMock(return_value=True)
        await limiter.process_request(callback, (), {}, "sendMessage", {"chat_id": -123, "message_thread_id": 99}, None)

        callback.assert_awaited_once()
        assert clock.now == pytest.approx(115.1)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("endpoint", "data"),
    [("answerCallbackQuery", {"callback_query_id": "button"}), ("sendChatAction", {"chat_id": -123})],
)
def test_buttons_and_typing_do_not_wait_for_chat_output(clock: _Clock, endpoint: str, data: dict) -> None:
    async def scenario() -> None:
        limiter = _TelegramRateLimiter()
        started = asyncio.Event()
        finish = asyncio.Event()

        async def slow_send() -> bool:
            started.set()
            await finish.wait()
            return True

        request = asyncio.create_task(limiter.process_request(slow_send, (), {}, "sendMessage", {"chat_id": -123}, None))
        await asyncio.wait_for(started.wait(), 1)

        callback = AsyncMock(return_value=True)
        await asyncio.wait_for(limiter.process_request(callback, (), {}, endpoint, data, None), 1)

        callback.assert_awaited_once()
        assert clock.now == 100
        finish.set()
        assert await request is True

    asyncio.run(scenario())

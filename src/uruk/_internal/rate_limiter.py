# SPDX-License-Identifier: ISC
# Copyright (c) 2026, Timothée Mazzucotelli and contributors

# Pace Telegram messages across topics and share flood-control cooldowns.

from __future__ import annotations

from asyncio import Lock, sleep
from collections import deque
from datetime import timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any

from loguru import logger
from telegram.error import RetryAfter
from telegram.ext import BaseRateLimiter

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

_GROUP_LIMIT = 20
# Allow a prompt update and its next question together, while bounding both
# short bursts and the group's rolling minute. Keep headroom at the boundaries.
_REQUEST_WINDOWS = ((3, 1.1), (_GROUP_LIMIT, 60.1))
_MAX_RETRIES = 3


class _TelegramRateLimiter(BaseRateLimiter[None]):
    """Serialize chat output and honor Telegram cooldowns across concurrent tasks.

    Uruk sends to one group. Topics share that group's message budget, so a
    single lock covers messages, attachments, edits, and command replies.
    Typing indicators and button acknowledgements do not consume that budget.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._requests: deque[float] = deque(maxlen=_GROUP_LIMIT)
        self._retry_at = 0.0

    async def initialize(self) -> None:
        """Keep the limiter ready without starting background tasks."""

    async def shutdown(self) -> None:
        """Leave cancellation of in-flight requests to the application."""

    async def process_request(  # noqa: PLR0917  # Signature required by BaseRateLimiter.
        self,
        callback: Callable[..., Coroutine[Any, Any, Any]],
        args: Any,
        kwargs: dict[str, Any],
        endpoint: str,
        data: dict[str, Any],
        rate_limit_args: None,  # noqa: ARG002
    ) -> Any:
        """Pace chat output and retry only explicit flood-control rejections."""
        paced = data.get("chat_id") is not None and endpoint != "sendChatAction"
        if paced:
            async with self._lock:
                return await self._request(callback, args, kwargs, endpoint, paced=True)
        return await self._request(callback, args, kwargs, endpoint, paced=False)

    async def _request(
        self,
        callback: Callable[..., Coroutine[Any, Any, Any]],
        args: Any,
        kwargs: dict[str, Any],
        endpoint: str,
        *,
        paced: bool,
    ) -> Any:
        attempt = 0
        while True:
            # Recheck after sleeping: another request can extend the cooldown.
            while True:
                deadline = self._retry_at
                if paced:
                    for limit, period in _REQUEST_WINDOWS:
                        if len(self._requests) >= limit:
                            deadline = max(deadline, self._requests[-limit] + period)
                delay = deadline - monotonic()
                if delay <= 0:
                    break
                await sleep(delay)
            if paced:
                self._requests.append(monotonic())
            try:
                return await callback(*args, **kwargs)
            except RetryAfter as error:
                retry_after = error.retry_after
                seconds = retry_after.total_seconds() if isinstance(retry_after, timedelta) else retry_after
                # Store the deadline before waiting, so cancellation cannot end
                # the cooldown early for other topics or command handlers.
                self._retry_at = max(self._retry_at, monotonic() + seconds + 0.1)
                if attempt == _MAX_RETRIES:
                    raise
                attempt += 1
                logger.warning("Telegram flood control for {}; retrying in {} seconds", endpoint, seconds)

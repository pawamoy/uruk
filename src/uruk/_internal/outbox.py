# SPDX-License-Identifier: ISC
# Copyright (c) 2026, Timothée Mazzucotelli and contributors

# Durable Telegram delivery. Acknowledged sends leave the queue; failures stay on disk.

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import sqlite3
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from loguru import logger
from telegram import InputFile, TelegramObject
from telegram.error import BadRequest, ChatMigrated, Forbidden, InvalidToken, NetworkError, RetryAfter, TelegramError
from telegram.ext import ExtBot

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path


def _encode_delivery(value: Any) -> Any:
    if isinstance(value, InputFile):
        content = value.input_file_content
        if not isinstance(content, bytes):
            content = content.read()
        return {"__uruk_type": "file", "content": base64.b64encode(content).decode("ascii"), "filename": value.filename, "mimetype": value.mimetype, "attach_name": value.attach_name}
    if isinstance(value, TelegramObject):
        return value.to_dict()
    if isinstance(value, datetime):
        return int(value.timestamp())
    raise TypeError(f"Cannot persist Telegram parameter of type {type(value).__name__}")


def _decode_delivery(value: dict) -> Any:
    if value.get("__uruk_type") == "file":
        file = InputFile(base64.b64decode(value["content"]), filename=value["filename"])
        file.mimetype = value["mimetype"]
        file.attach_name = value["attach_name"]
        return file
    return value


def _dump_delivery(value: Any) -> str:
    return json.dumps(value, default=_encode_delivery)


def _load_delivery(value: str) -> Any:
    return json.loads(value, object_hook=_decode_delivery)


def _expire_delivery_controls(data: dict) -> bool:
    markup = data.get("reply_markup") or {}
    if isinstance(markup, TelegramObject):
        markup = markup.to_dict()
    callbacks = [button.get("callback_data", "") for row in markup.get("inline_keyboard", []) for button in row]
    if not any(callback.startswith(("a:", "p:", "q:", "x:")) for callback in callbacks):
        return False
    data.pop("reply_markup", None)
    # Restoring the text must not resurrect buttons whose in-memory waiter is gone.
    data["text"] += "\n\nThis interaction has ended. Start the flow again if needed."
    return True


class _DeliveryError(RuntimeError):
    """A message retained in the outbox needs attention before it can be delivered."""


class _Outbox:
    """Persist complete messages and retry transient failures independently of callers.

    Delivery is at least once: a lost acknowledgement can cause a duplicate.
    Permanent failures remain inspectable and can be retried after correction.
    """

    def __init__(self, path: Path, sender: Callable[[str, dict], Awaitable[Any]], prepare: Callable[[dict], Awaitable[list[tuple[str, dict]]]]) -> None:
        self.path = path
        self._sender = sender
        self._prepare = prepare
        self._worker: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._waiters: dict[int, asyncio.Future] = {}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        with self._connection() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, operation TEXT NOT NULL, payload TEXT NOT NULL, topic TEXT NOT NULL, parts TEXT, part_index INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '', retired INTEGER NOT NULL DEFAULT 0)")
            for row in connection.execute("SELECT * FROM outbox").fetchall():
                self._expire(connection, row)

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous = FULL")
            with connection:
                yield connection

    def _expire(self, connection: sqlite3.Connection, row: sqlite3.Row) -> None:
        payload = _load_delivery(row["payload"])
        changed = _expire_delivery_controls(payload) if row["operation"] == "sendMessage" else False
        parts = _load_delivery(row["parts"]) if row["parts"] else None
        if parts:
            for operation, data in parts[row["part_index"]:]:
                if operation == "sendMessage":
                    changed = _expire_delivery_controls(data) or changed
        if changed:
            connection.execute("UPDATE outbox SET payload = ?, parts = ?, retired = 1 WHERE id = ?", (_dump_delivery(payload), _dump_delivery(parts) if parts else None, row["id"]))

    def start(self) -> None:
        """Resume pending deliveries without waiting for a new message."""
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="uruk-outbox")
        self._wake.set()

    async def stop(self) -> None:
        """Stop the worker, leaving unfinished deliveries on disk."""
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    def enqueue(self, operation: str, data: dict) -> int:
        """Commit content and schedule delivery without waiting for the network."""
        topic = data.get("message_thread_id")
        topic_key = json.dumps([data.get("chat_id"), None if topic == 1 else topic])
        with self._connection() as connection:
            cursor = connection.execute("INSERT INTO outbox (operation, payload, topic) VALUES (?, ?, ?)", (operation, _dump_delivery(data), topic_key))
            job_id = cursor.lastrowid
        assert job_id is not None  # noqa: S101
        self.start()
        return job_id

    async def send(self, operation: str, data: dict) -> Any:
        """Commit a delivery before awaiting it; caller cancellation cannot discard it."""
        job_id = self.enqueue(operation, data)
        future = asyncio.get_running_loop().create_future()
        self._waiters[job_id] = future
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # A timeout or shutdown can abandon an interaction while its send is
            # waiting. Preserve the content, but retire its process-local buttons.
            with self._connection() as connection:
                row = connection.execute("SELECT * FROM outbox WHERE id = ?", (job_id,)).fetchone()
                if row is not None:
                    self._expire(connection, row)
            raise
        finally:
            self._waiters.pop(job_id, None)
            if not future.done():
                future.cancel()

    def pending(self) -> list[dict]:
        """Return delivery IDs, destinations, attempts, and errors without message bodies."""
        with self._connection() as connection:
            return [dict(row) for row in connection.execute("SELECT id, operation, topic, state, attempts, last_error FROM outbox ORDER BY id")]

    def retry(self) -> int:
        """Make retained failures eligible for another delivery attempt."""
        with self._connection() as connection:
            count = connection.execute("UPDATE outbox SET state = 'pending', next_attempt = 0 WHERE state = 'blocked'").rowcount
        self.start()
        return count

    def resend(self, job_id: int, chat_id: int, topic_id: int | None) -> bool:
        """Redirect a blocked message to the current topic without changing its content."""
        topic_id = None if topic_id == 1 else topic_id
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM outbox WHERE id = ? AND state = 'blocked'", (job_id,)).fetchone()
            if row is None or row["operation"] not in {"sendMessage", "sendDocument", "sendPhoto", "renderText"}:
                return False
            self._expire(connection, row)
            row = connection.execute("SELECT * FROM outbox WHERE id = ?", (job_id,)).fetchone()
            payload = _load_delivery(row["payload"])
            parts = _load_delivery(row["parts"]) if row["parts"] else None
            destinations = [payload, *(data for _, data in parts)] if parts else [payload]
            for data in destinations:
                data["chat_id"] = chat_id
                data["message_thread_id"] = topic_id
                # Recovery must not depend on a reply target in the old topic.
                data.pop("reply_parameters", None)
                data.pop("reply_to_message_id", None)
            connection.execute("UPDATE outbox SET payload = ?, parts = ?, topic = ?, state = 'pending', next_attempt = 0 WHERE id = ?", (_dump_delivery(payload), _dump_delivery(parts) if parts else None, json.dumps([chat_id, topic_id]), job_id))
        self.start()
        return True

    async def _run(self) -> None:
        try:
            while True:
                self._wake.clear()
                with self._connection() as connection:
                    # Keep transient retries in order within each topic. A
                    # blocked delivery must not hide subsequent error reports.
                    row = connection.execute("SELECT a.* FROM outbox a WHERE a.state = 'pending' AND NOT EXISTS (SELECT 1 FROM outbox b WHERE b.topic = a.topic AND b.state = 'pending' AND b.id < a.id) ORDER BY a.next_attempt, a.id LIMIT 1").fetchone()
                if row is None:
                    await self._wake.wait()
                    continue
                delay = row["next_attempt"] - time.time()
                if delay > 0:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), min(delay, 60))
                    continue
                await self._deliver(row)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            logger.exception("outbox worker stopped; queued deliveries remain in {}", self.path)
            for future in self._waiters.values():
                if not future.done():
                    future.set_exception(_DeliveryError(f"Outbox worker stopped: {error}. Messages remain in {self.path}"))

    async def _deliver(self, row: sqlite3.Row) -> None:
        job_id = row["id"]
        try:
            if row["parts"]:
                parts = _load_delivery(row["parts"])
            elif row["operation"] == "renderText":
                parts = await self._prepare(_load_delivery(row["payload"]))
            else:
                parts = [(row["operation"], _load_delivery(row["payload"]))]
            with self._connection() as connection:
                connection.execute("UPDATE outbox SET parts = ? WHERE id = ?", (_dump_delivery(parts), job_id))
            result = None
            for index in range(row["part_index"], len(parts)):
                operation, data = parts[index]
                try:
                    result = await self._sender(operation, data)
                except BadRequest:
                    if operation != "sendMessage" or not (data.get("entities") or data.get("parse_mode")):
                        raise
                    # Telegram explicitly rejected the formatted version, so a
                    # plain-text retry cannot duplicate a successful delivery.
                    data.pop("entities", None)
                    data.pop("parse_mode", None)
                    with self._connection() as connection:
                        connection.execute("UPDATE outbox SET parts = ? WHERE id = ?", (_dump_delivery(parts), job_id))
                    result = await self._sender(operation, data)
                with self._connection() as connection:
                    current = connection.execute("SELECT retired FROM outbox WHERE id = ?", (job_id,)).fetchone()
                    if current["retired"] and data.get("reply_markup") and isinstance(result, dict) and "message_id" in result:
                        cleanup = {"chat_id": data["chat_id"], "message_id": result["message_id"]}
                        connection.execute("INSERT INTO outbox (operation, payload, topic) VALUES ('editMessageReplyMarkup', ?, ?)", (_dump_delivery(cleanup), row["topic"]))
                    connection.execute("UPDATE outbox SET part_index = ?, attempts = 0, next_attempt = 0, last_error = '' WHERE id = ?", (index + 1, job_id))
            with self._connection() as connection:
                connection.execute("DELETE FROM outbox WHERE id = ?", (job_id,))
            if (future := self._waiters.get(job_id)) is not None and not future.done():
                future.set_result(result)
        except RetryAfter as error:
            retry_after = error.retry_after
            seconds = retry_after.total_seconds() if isinstance(retry_after, timedelta) else retry_after
            self._failed(row, error, seconds + 0.1)
        except NetworkError as error:
            if isinstance(error, BadRequest):
                self._blocked(row, error)
            else:
                self._failed(row, error, min(60, 2 ** min(row["attempts"], 6)))
        except (ChatMigrated, Forbidden, InvalidToken) as error:
            self._blocked(row, error)
        except TelegramError as error:
            self._failed(row, error, min(60, 2 ** min(row["attempts"], 6)))
        except Exception as error:  # noqa: BLE001
            self._blocked(row, error)

    def _failed(self, row: sqlite3.Row, error: Exception, delay: float) -> None:
        with self._connection() as connection:
            connection.execute("UPDATE outbox SET attempts = attempts + 1, next_attempt = ?, last_error = ? WHERE id = ?", (time.time() + delay, str(error), row["id"]))
        logger.warning("delivery #{} retained; retrying in {} seconds: {}", row["id"], delay, error)

    def _blocked(self, row: sqlite3.Row, error: Exception) -> None:
        with self._connection() as connection:
            current = connection.execute("SELECT * FROM outbox WHERE id = ?", (row["id"],)).fetchone()
            self._expire(connection, current)
            connection.execute("UPDATE outbox SET state = 'blocked', attempts = attempts + 1, last_error = ? WHERE id = ?", (str(error), row["id"]))
        logger.error("delivery #{} blocked and retained in {}: {}", row["id"], self.path, error)
        if (future := self._waiters.get(row["id"])) is not None and not future.done():
            future.set_exception(_DeliveryError(f"Delivery #{row['id']} retained: {error}. Use /outbox to inspect it."))


class _DurableBot(ExtBot[None]):
    """Persist content sends from every API entry point, including message shortcuts."""

    __slots__ = ("_outbox",)

    def __init__(self, token: str, outbox: _Outbox, **kwargs: Any) -> None:
        super().__init__(token=token, **kwargs)
        with self._unfrozen():
            self._outbox = outbox

    async def _do_post(self, endpoint: str, data: dict, **kwargs: Any) -> Any:
        if endpoint in {"sendMessage", "sendDocument", "sendPhoto"}:
            # Delivery uses the configured transport timeouts. The caller's
            # lifetime cannot cancel a persisted network request.
            return await self._outbox.send(endpoint, data)
        return await super()._do_post(endpoint, data, **kwargs)

    async def deliver(self, endpoint: str, data: dict) -> Any:
        """Send one queued part through the normal rate limiter without requeuing it."""
        return await super()._do_post(endpoint, data)

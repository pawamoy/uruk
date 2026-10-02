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

# Persistence of task/session state across bot restarts.

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class TaskInfo:
    """One agent task: a repo, a Telegram forum topic, and a resumable session."""

    topic_id: int
    """Telegram forum topic ID."""
    repo: str
    """Path to the session's repository."""
    title: str
    """Session title shown in Telegram and the terminal session picker."""
    provider: str = "claude"
    """Agent provider: `claude` or `openai`."""
    session_id: str | None = None
    """Provider session ID used to resume the conversation."""
    model: str | None = None
    """Requested model, or `None` to use the default."""
    effort: str | None = None
    """Requested reasoning effort, or `None` to use the default."""
    status_message_id: int | None = None
    """Telegram message ID of the pinned session status."""
    resolved_model: str | None = None  # Actual model reported by the session's init message.
    """Actual model reported by the provider."""
    # ``origin`` says where the session was first created.  ``owner`` says which
    # UI is currently allowed to drive it.  Keeping these separate means a
    # Telegram-created session remains available to ``uruk resume`` after it has
    # been handed to a terminal and back again.
    origin: str = "telegram"  # "telegram" or "terminal"
    """Interface where the session was created: `telegram` or `terminal`."""
    owner: str = "telegram"  # "telegram", "terminal", or "transferring"
    """Current session owner: `telegram`, `terminal`, or `transferring`."""


class SessionStore:
    """JSON-file-backed map of topic ID to task info, plus the closed topics awaiting deletion."""

    def __init__(self, path: Path) -> None:
        """Load session state from `path` if the file exists."""
        self.path = path
        """JSON file used to load and save session state."""
        self._tasks: dict[int, TaskInfo] = {}
        self._closed: list[int] = []
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if "tasks" in data:
                tasks = data["tasks"]
                self._closed = [int(topic_id) for topic_id in data.get("closed_topics", [])]
            else:  # Legacy format: a flat topic-id-to-info map.
                tasks = data
            self._tasks = {int(key): TaskInfo(**value) for key, value in tasks.items()}

    def save(self) -> None:
        """Write all tasks and closed topic IDs to the state file."""
        data = {
            "tasks": {str(topic_id): asdict(info) for topic_id, info in self._tasks.items()},
            "closed_topics": self._closed,
        }
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get(self, topic_id: int) -> TaskInfo | None:
        """Return the task for a topic, or `None` if the topic is not tracked."""
        return self._tasks.get(topic_id)

    def set(self, info: TaskInfo) -> None:
        """Add or replace a task and save the state file."""
        self._tasks[info.topic_id] = info
        self.save()

    def remove(self, topic_id: int) -> None:
        """Remove a tracked task and save the change, if the task exists."""
        if self._tasks.pop(topic_id, None) is not None:
            self.save()

    def all(self) -> list[TaskInfo]:
        """Return a list of all tracked tasks."""
        return list(self._tasks.values())

    def add_closed(self, topic_id: int) -> None:
        """Mark a topic as awaiting deletion and save the change."""
        if topic_id not in self._closed:
            self._closed.append(topic_id)
            self.save()

    def remove_closed(self, topic_id: int) -> None:
        """Stop tracking a closed topic and save the change."""
        if topic_id in self._closed:
            self._closed.remove(topic_id)
            self.save()

    def closed(self) -> list[int]:
        """Return the IDs of closed topics awaiting deletion."""
        return list(self._closed)

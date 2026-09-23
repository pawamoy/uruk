"""Persistence of task/session state across bot restarts."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class TaskInfo:
    """One agent task: a repo, a Telegram forum topic, and a resumable session."""

    topic_id: int
    repo: str
    title: str
    provider: str = "claude"
    session_id: str | None = None
    model: str | None = None
    effort: str | None = None
    status_message_id: int | None = None
    resolved_model: str | None = None  # Actual model reported by the session's init message.
    # ``origin`` says where the session was first created.  ``owner`` says which
    # UI is currently allowed to drive it.  Keeping these separate means a
    # Telegram-created session remains available to ``uruk resume`` after it has
    # been handed to a terminal and back again.
    origin: str = "telegram"  # "telegram" or "terminal"
    owner: str = "telegram"  # "telegram", "terminal", or "transferring"


class SessionStore:
    """JSON-file-backed map of topic ID to task info, plus the closed topics awaiting deletion."""

    def __init__(self, path: Path) -> None:
        self.path = path
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
        data = {
            "tasks": {str(topic_id): asdict(info) for topic_id, info in self._tasks.items()},
            "closed_topics": self._closed,
        }
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get(self, topic_id: int) -> TaskInfo | None:
        return self._tasks.get(topic_id)

    def set(self, info: TaskInfo) -> None:
        self._tasks[info.topic_id] = info
        self.save()

    def remove(self, topic_id: int) -> None:
        if self._tasks.pop(topic_id, None) is not None:
            self.save()

    def all(self) -> list[TaskInfo]:
        return list(self._tasks.values())

    def add_closed(self, topic_id: int) -> None:
        if topic_id not in self._closed:
            self._closed.append(topic_id)
            self.save()

    def remove_closed(self, topic_id: int) -> None:
        if topic_id in self._closed:
            self._closed.remove(topic_id)
            self.save()

    def closed(self) -> list[int]:
        return list(self._closed)

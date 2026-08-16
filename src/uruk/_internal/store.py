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
    session_id: str | None = None
    model: str | None = None
    effort: str | None = None


class SessionStore:
    """JSON-file-backed map of topic ID to task info."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._tasks: dict[int, TaskInfo] = {}
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            self._tasks = {int(key): TaskInfo(**value) for key, value in data.items()}

    def save(self) -> None:
        data = {str(topic_id): asdict(info) for topic_id, info in self._tasks.items()}
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

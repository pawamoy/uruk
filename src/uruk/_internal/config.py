"""Configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """Invalid or missing configuration."""


@dataclass(frozen=True)
class Config:
    token: str
    chat_id: int | None
    owner_id: int | None
    repos_root: Path | None
    permission_mode: str
    model: str | None
    data_dir: Path
    repo_prefixes: dict[str, str]

    @staticmethod
    def data_dir_from_env() -> Path:
        """Return Uruk's state directory without requiring Telegram settings."""
        return Path(os.environ.get("URUK_DATA_DIR", "~/.local/share/uruk")).expanduser()

    @property
    def configured(self) -> bool:
        """Whether the bot is fully configured (vs. setup mode where only /id works)."""
        return self.chat_id is not None and self.owner_id is not None

    @classmethod
    def from_env(cls) -> Config:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is not set (get one from @BotFather)")

        def _int(name: str) -> int | None:
            value = os.environ.get(name)
            if value is None or not value.strip():
                return None
            try:
                return int(value)
            except ValueError as error:
                raise ConfigError(f"{name} must be an integer, got {value!r}") from error

        repos_root_env = os.environ.get("URUK_REPOS_ROOT")
        repos_root = Path(repos_root_env).expanduser() if repos_root_env else None
        if repos_root is not None and not repos_root.is_dir():
            raise ConfigError(f"URUK_REPOS_ROOT is not a directory: {repos_root}")

        data_dir = cls.data_dir_from_env()
        data_dir.mkdir(parents=True, exist_ok=True)

        repo_prefixes = {}
        for entry in os.environ.get("URUK_REPO_PREFIXES", "").split(","):
            entry = entry.strip()
            if not entry:
                continue
            owner, separator, prefix = entry.partition("=")
            if not separator or not owner.strip():
                raise ConfigError(f"URUK_REPO_PREFIXES entries must look like owner=prefix, got {entry!r}")
            repo_prefixes[owner.strip()] = prefix.strip()

        return cls(
            token=token,
            chat_id=_int("TELEGRAM_CHAT_ID"),
            owner_id=_int("TELEGRAM_OWNER_ID"),
            repos_root=repos_root,
            permission_mode=os.environ.get("URUK_PERMISSION_MODE", "acceptEdits"),
            model=os.environ.get("URUK_MODEL") or None,
            data_dir=data_dir,
            repo_prefixes=repo_prefixes,
        )

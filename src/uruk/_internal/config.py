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

# Configuration, read from environment variables.

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """Invalid or missing configuration."""


@dataclass(frozen=True)
class Config:
    """Telegram settings, agent defaults, and paths used by the bot."""

    token: str
    """Telegram bot token."""
    chat_id: int | None
    """Authorized Telegram chat, or `None` during setup."""
    owner_id: int | None
    """Authorized Telegram user, or `None` during setup."""
    repos_root: Path | None
    """Directory containing repositories available to the bot."""
    permission_mode: str
    """Default permission mode for Claude sessions."""
    model: str | None
    """Default Claude model, or `None` to use the provider default."""
    data_dir: Path
    """Directory containing session state and the local control socket."""
    repo_prefixes: dict[str, str]
    """Repository owner names mapped to local directory prefixes."""

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
        """Read settings from environment variables and create the state directory.

        Raises:
            ConfigError: The bot token is missing or a setting is invalid.
        """
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
            entry = entry.strip()  # noqa: PLW2901
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

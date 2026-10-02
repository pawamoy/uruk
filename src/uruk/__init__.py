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

"""Uruk package.

Telegram bot to drive Claude Code and OpenAI Codex sessions from your phone.
"""

from __future__ import annotations

from uruk._internal.agent import UI, AgentTask, ClaudeAgentTask
from uruk._internal.bot import UrukBot
from uruk._internal.cli import get_parser, main, resume_main
from uruk._internal.codex_agent import CodexAgentTask
from uruk._internal.config import Config, ConfigError
from uruk._internal.store import SessionStore, TaskInfo

__all__: list[str] = [
    "UI",
    "AgentTask",
    "ClaudeAgentTask",
    "CodexAgentTask",
    "Config",
    "ConfigError",
    "SessionStore",
    "TaskInfo",
    "UrukBot",
    "get_parser",
    "main",
    "resume_main",
]

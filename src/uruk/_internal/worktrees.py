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

# Git worktrees used by new Telegram tasks.

from __future__ import annotations

import secrets
import subprocess
from pathlib import Path


def _git(repo: Path, *arguments: str) -> str:
    """Run Git in a repository and report failures without starting an agent."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), *arguments],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(error.stderr.strip() or "Git could not prepare the task worktree.") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Git timed out while preparing the task worktree.") from error
    except OSError as error:
        raise RuntimeError(f"Could not run Git: {error}") from error
    return result.stdout.strip()


def _prepare_worktree(repo: Path, issue_number: int | None = None) -> Path:
    """Create a task branch and worktree under `~/dev/.worktrees`.

    Issue tasks reuse their existing worktree or branch without resetting it.
    Other tasks get a unique branch based on the source repository's HEAD.
    Raise RuntimeError if the destination belongs to another worktree or branch.
    """
    repo = Path(_git(repo, "rev-parse", "--show-toplevel"))
    branch = f"issue{issue_number}" if issue_number is not None else f"task-{secrets.token_hex(4)}"
    name = f"{repo.name}#{issue_number}" if issue_number is not None else f"{repo.name}-{branch}"
    destination = (Path.home() / "dev" / ".worktrees" / name).resolve()

    # Only reuse a destination registered to this repository on the expected branch.
    # An unrelated directory must never become an agent's working directory.
    for record in _git(repo, "worktree", "list", "--porcelain", "-z").split("\0\0"):
        fields = dict(field.split(" ", 1) for field in record.split("\0") if " " in field)
        if fields.get("worktree") == str(destination):
            if fields.get("branch") != f"refs/heads/{branch}" or not destination.is_dir():
                raise RuntimeError(f"The existing worktree at {destination} is missing or is not on branch {branch}.")
            return destination
    if destination.exists() or destination.is_symlink():
        raise RuntimeError(f"The worktree destination already exists: {destination}.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if _git(repo, "for-each-ref", "--format=%(refname)", f"refs/heads/{branch}") == f"refs/heads/{branch}":
        _git(repo, "worktree", "add", str(destination), branch)
    else:
        _git(repo, "worktree", "add", "-b", branch, str(destination))
    return destination

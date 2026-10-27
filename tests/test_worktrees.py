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

"""Task worktrees preserve the source checkout and existing issue work."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from uruk._internal.worktrees import _prepare_worktree


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-C", str(repo), *arguments],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Keep all worktrees in the test directory, including the simulated home.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    repository = tmp_path / "project"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    (repository / "file.txt").write_text("original\n", encoding="utf-8")
    _git(repository, "add", "file.txt")
    _git(repository, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "Initial commit")
    return repository


def test_issue_worktree_isolates_work_from_the_source_checkout(repo: Path) -> None:
    # A task starts from the committed HEAD, preserving uncommitted source edits.
    (repo / "file.txt").write_text("source edits\n", encoding="utf-8")

    worktree = _prepare_worktree(repo, 7)

    assert worktree == Path.home() / "dev" / ".worktrees" / "project#7"
    assert _git(worktree, "branch", "--show-current") == "issue7"
    assert (worktree / "file.txt").read_text(encoding="utf-8") == "original\n"

    (worktree / "file.txt").write_text("task edits\n", encoding="utf-8")

    assert (repo / "file.txt").read_text(encoding="utf-8") == "source edits\n"
    assert _git(repo, "branch", "--show-current") == "main"


def test_repeated_issue_preserves_existing_worktree_changes(repo: Path) -> None:
    worktree = _prepare_worktree(repo, 7)
    (worktree / "file.txt").write_text("unfinished work\n", encoding="utf-8")

    repeated = _prepare_worktree(repo, 7)

    assert repeated == worktree
    assert (worktree / "file.txt").read_text(encoding="utf-8") == "unfinished work\n"


def test_existing_issue_branch_is_not_reset_to_source_head(repo: Path) -> None:
    _git(repo, "branch", "issue7")
    issue_head = _git(repo, "rev-parse", "issue7")
    (repo / "file.txt").write_text("later source commit\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "Later commit")

    worktree = _prepare_worktree(repo, 7)

    assert _git(worktree, "rev-parse", "HEAD") == issue_head
    assert _git(worktree, "rev-parse", "HEAD") != _git(repo, "rev-parse", "HEAD")


def test_tasks_without_an_issue_get_separate_worktrees_and_branches(repo: Path) -> None:
    first = _prepare_worktree(repo)
    second = _prepare_worktree(repo)

    assert first != second
    assert first.parent == second.parent == Path.home() / "dev" / ".worktrees"
    assert first.name == f"project-{_git(first, 'branch', '--show-current')}"
    assert second.name == f"project-{_git(second, 'branch', '--show-current')}"
    assert _git(first, "rev-parse", "HEAD") == _git(second, "rev-parse", "HEAD")


def test_unrelated_destination_is_preserved(repo: Path) -> None:
    destination = Path.home() / "dev" / ".worktrees" / "project#7"
    destination.mkdir(parents=True)
    marker = destination / "keep.txt"
    marker.write_text("existing data", encoding="utf-8")

    with pytest.raises(RuntimeError, match="destination already exists"):
        _prepare_worktree(repo, 7)

    assert marker.read_text(encoding="utf-8") == "existing data"
    assert not _git(repo, "branch", "--list", "issue7")


def test_existing_worktree_on_another_branch_is_preserved(repo: Path) -> None:
    worktree = _prepare_worktree(repo, 7)
    _git(worktree, "switch", "-c", "other-work")

    with pytest.raises(RuntimeError, match="not on branch issue7"):
        _prepare_worktree(repo, 7)

    assert _git(worktree, "branch", "--show-current") == "other-work"


def test_issue_branch_checked_out_elsewhere_is_not_taken_over(repo: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "existing-work"
    _git(repo, "worktree", "add", "-b", "issue7", str(elsewhere))

    with pytest.raises(RuntimeError, match=r"already (used by worktree|checked out)"):
        _prepare_worktree(repo, 7)

    assert _git(elsewhere, "branch", "--show-current") == "issue7"


def test_non_git_directory_cannot_start_a_worktree(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not a git repository"):
        _prepare_worktree(tmp_path, 7)

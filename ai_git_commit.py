#!/usr/bin/env python3
"""
AI-assisted Git committer: sync a source directory into a local Git repo and
create one or more commits with LLM-generated messages.

Changes are grouped into functionally coherent commits. Each commit's total
changed lines (insertions + deletions) is kept at or below 200 when possible.
Single files larger than 200 lines are committed alone.

Copyright (C) 2026 Dr. Nikolaus Klepp
office@klepp.biz - https://klepp.biz

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import requests
from tqdm import tqdm

MAX_LINES_PER_COMMIT = 200
API_TIMEOUT = 120


@dataclass
class FileChange:
    path: str
    status: str  # A, M, D, R, etc.
    insertions: int = 0
    deletions: int = 0

    @property
    def total_lines(self) -> int:
        return self.insertions + self.deletions


@dataclass
class CommitPlan:
    message: str
    files: list[str] = field(default_factory=list)


def run_git(repo: Path, *args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(repo), *args]
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
    )


def parse_author(name_email: str) -> tuple[str, str]:
    """Parse 'Name <email@domain>' into (name, email)."""
    m = re.match(r"^\s*(.*?)\s*<([^>]+)>\s*$", name_email)
    if not m:
        raise ValueError(
            f"Invalid --name format. Expected 'Name <email@domain>', got: {name_email!r}"
        )
    return m.group(1).strip(), m.group(2).strip()


def ensure_git_repo(repo: Path) -> None:
    """Ensure *repo* is a Git working tree. Create the directory and run
    ``git init`` if it does not exist or is not yet a repository."""
    if not repo.exists():
        print(f"Git repository does not exist – creating {repo} …")
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
        print(f"  initialized empty Git repository in {repo}")
        return

    if not repo.is_dir():
        raise SystemExit(f"Git repository path exists but is not a directory: {repo}")

    r = run_git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    if r.returncode != 0 or r.stdout.strip() != "true":
        # Directory exists but is not a Git repo → init in place
        print(f"Directory {repo} is not a Git repository – running git init …")
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
        print(f"  initialized empty Git repository in {repo}")


ARCHIVE_SUFFIXES = (
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
)


def is_archive(path: Path) -> bool:
    name = path.name.lower()
    return path.is_file() and any(name.endswith(s) for s in ARCHIVE_SUFFIXES)


def _strip_to_content_root(extracted: Path) -> Path:
    """
    If the extracted tree contains exactly one top-level directory (and no
    loose files), treat that directory as the real content root.  Repeat
    while the pattern holds so nested single-dir wrappers are also removed.
    """
    current = extracted
    while True:
        items = [p for p in current.iterdir() if not p.name.startswith(".")]
        if len(items) == 1 and items[0].is_dir():
            current = items[0]
            continue
        break
    return current


@contextmanager
def resolve_source(src: Path) -> Iterator[Path]:
    """
    Yield a directory that contains the source content.

    * Plain directory → yield as-is.
    * Archive (zip/tar/…) → extract to a temporary directory, strip any
      single leading directory layers, yield the content root, then delete
      the temporary tree on exit.
    """
    src = src.expanduser().resolve()
    if not src.exists():
        raise SystemExit(f"Source path does not exist: {src}")

    if src.is_dir():
        yield src
        return

    if not is_archive(src):
        raise SystemExit(
            f"Source must be a directory or a supported archive "
            f"({', '.join(ARCHIVE_SUFFIXES)}), got: {src}"
        )

    tmp = tempfile.mkdtemp(prefix="ai_git_commit_")
    tmp_path = Path(tmp)
    print(f"Extracting archive {src.name} → temporary directory …")
    try:
        name = src.name.lower()
        if name.endswith(".zip"):
            with zipfile.ZipFile(src, "r") as zf:
                zf.extractall(tmp_path)
        elif name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
            with tarfile.open(src, "r:*") as tf:
                tf.extractall(tmp_path)
        else:
            raise SystemExit(f"Unsupported archive format: {src}")

        content = _strip_to_content_root(tmp_path)
        if content != tmp_path:
            print(f"  stripped leading directory layer(s) → using {content.relative_to(tmp_path)}")
        yield content
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)
        print("  temporary extraction directory removed.")


def detect_extra_directory_layer(src: Path, repo: Path) -> str | None:
    """
    Detect the common case where the source tree has been wrapped in one (or
    more) extra top-level directories compared to the repository root.

    Example:
        repo:   program.py, lib/, …
        src:    somedir/program.py, somedir/lib/, …

    Returns a human-readable warning message, or None if the layouts look
    compatible.
    """
    # Only meaningful when the repository already has content
    repo_top = {
        p.name
        for p in repo.iterdir()
        if p.name != ".git" and not p.name.startswith(".")
    }
    if not repo_top:
        return None  # empty / new repo – nothing to compare against

    src_items = [p for p in src.iterdir() if not p.name.startswith(".")]
    if not src_items:
        return None

    # Walk down while there is exactly one directory and no files
    depth = 0
    current = src
    while True:
        items = [p for p in current.iterdir() if not p.name.startswith(".")]
        files = [p for p in items if p.is_file()]
        dirs = [p for p in items if p.is_dir()]
        if files:
            break
        if len(dirs) != 1:
            break
        current = dirs[0]
        depth += 1

    if depth == 0:
        return None

    # Check whether the inner content overlaps with the repo root
    inner_top = {
        p.name
        for p in current.iterdir()
        if not p.name.startswith(".")
    }
    overlap = repo_top & inner_top
    if not overlap:
        return None

    # Build a short relative path of the extra layers
    rel_layers = current.relative_to(src)
    return (
        f"Source directory appears to contain {depth} extra top-level "
        f"directory layer(s): '{rel_layers}'.\n"
        f"  Repository root contains e.g.: {', '.join(sorted(repo_top)[:5])}"
        f"{'…' if len(repo_top) > 5 else ''}\n"
        f"  Inside the extra layer(s) we find overlapping names: "
        f"{', '.join(sorted(overlap)[:5])}{'…' if len(overlap) > 5 else ''}\n"
        f"\n"
        f"This usually means the content was nested one directory deeper than "
        f"expected.  Please point --src at the correct directory (or unpack "
        f"an archive, which is automatically stripped) and try again."
    )


def sync_source_to_repo(src: Path, repo: Path) -> None:
    """
    Copy all content from src into repo, deleting files that no longer exist
    in src (except .git). Uses rsync if available, otherwise shutil.
    """
    src = src.resolve()
    repo = repo.resolve()
    if not src.is_dir():
        raise SystemExit(f"Source directory does not exist: {src}")

    # Prefer rsync for efficient sync + delete
    if shutil.which("rsync"):
        cmd = [
            "rsync",
            "-a",
            "--delete",
            "--exclude",
            ".git",
            "--info=progress2",
            f"{src}/",
            f"{repo}/",
        ]
        # rsync --info=progress2 writes live progress to stderr
        subprocess.run(cmd, check=True)
    else:
        # Fallback: walk and copy with a progress bar, then remove extras
        file_list: list[tuple[Path, Path]] = []
        for root, dirs, files in os.walk(src):
            rel = Path(root).relative_to(src)
            dest_dir = repo / rel
            for f in files:
                file_list.append((Path(root) / f, dest_dir / f))

        for src_file, dest_file in tqdm(
            file_list, desc="Copying files", unit="file", leave=True
        ):
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest_file)

        # Remove files present in repo but not in src (skip .git)
        to_remove: list[Path] = []
        for root, dirs, files in os.walk(repo, topdown=True):
            if Path(root).name == ".git" or ".git" in Path(root).parts:
                dirs.clear()
                continue
            rel = Path(root).relative_to(repo)
            src_dir = src / rel
            for f in files:
                if not (src_dir / f).exists():
                    to_remove.append(Path(root) / f)
        for p in tqdm(to_remove, desc="Removing obsolete", unit="file", leave=False):
            p.unlink()


def has_commits(repo: Path) -> bool:
    """Return True if the repository has at least one commit (HEAD exists)."""
    r = run_git(repo, "rev-parse", "--verify", "HEAD", check=False)
    return r.returncode == 0


def collect_changes(repo: Path) -> list[FileChange]:
    """Return list of changed files with insertion/deletion counts."""
    changes: dict[str, FileChange] = {}

    # Tracked modifications / deletions / renames (only when HEAD exists)
    if has_commits(repo):
        r = run_git(repo, "diff", "--numstat", "HEAD")
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            ins, dels, path = parts[0], parts[1], parts[2]
            # handle renames: path may be "old => new"
            if " => " in path:
                path = path.split(" => ")[-1].strip()
            ins_n = 0 if ins == "-" else int(ins)
            del_n = 0 if dels == "-" else int(dels)
            changes[path] = FileChange(path=path, status="M", insertions=ins_n, deletions=del_n)

    # Status for untracked / deleted / type
    r = run_git(repo, "status", "--porcelain", "-uall")
    for line in r.stdout.splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        path = line[3:].strip()
        # renames / copies in porcelain: R  old -> new
        if " -> " in path:
            path = path.split(" -> ")[-1].strip()
        if path in changes:
            # refine status
            if "D" in status:
                changes[path].status = "D"
            elif "A" in status or "?" in status:
                changes[path].status = "A"
            continue
        if status.strip() == "??" or status.startswith("A") or status.startswith("?"):
            # untracked / new: count lines
            fpath = repo / path
            lines = 0
            if fpath.is_file():
                try:
                    lines = sum(1 for _ in fpath.open("rb"))
                except OSError:
                    lines = 0
            changes[path] = FileChange(path=path, status="A", insertions=lines, deletions=0)
        elif "D" in status:
            changes[path] = FileChange(path=path, status="D", insertions=0, deletions=0)

    return sorted(changes.values(), key=lambda c: c.path)


def call_llm(
    base_url: str,
    model: str,
    system: str,
    user: str,
    api_key: str | None = None,
) -> str:
    """Call OpenAI-compatible /v1/chat/completions."""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": 4096,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Unexpected LLM response format: {data}") from e


def plan_commits(changes: list[FileChange], base_url: str, model: str, api_key: str | None) -> list[CommitPlan]:
    if not changes:
        return []

    # Build summary for the model
    lines = []
    total = 0
    for c in changes:
        lines.append(
            f"- {c.path}  [{c.status}]  +{c.insertions}/-{c.deletions}  (total {c.total_lines} lines)"
        )
        total += c.total_lines

    summary = "\n".join(lines)

    system = (
        "You are an expert software engineer who writes clear, conventional Git commit messages "
        "and groups related changes into logically coherent commits.\n"
        "Rules:\n"
        f"- Prefer commits whose total changed lines (insertions + deletions) stay <= {MAX_LINES_PER_COMMIT}.\n"
        "- If a single file exceeds that limit, it may form its own commit.\n"
        "- Group files that belong to the same functional change (feature, fix, refactor, docs, etc.).\n"
        "- Order commits so that earlier commits do not depend on later ones when possible.\n"
        "- Commit messages: imperative mood, concise subject (<=72 chars), optional body.\n"
        "- Output ONLY valid JSON: a list of objects, each with keys \"message\" (string) and \"files\" (list of strings).\n"
        "- Every changed file must appear in exactly one commit.\n"
        "- Do not invent file paths."
    )

    user = (
        f"The following files have changed (total ~{total} lines):\n\n"
        f"{summary}\n\n"
        "Produce a list of commits that apply these changes in a sensible order."
    )

    raw = call_llm(base_url, model, system, user, api_key)

    # Extract JSON (tolerate markdown fences)
    text = raw
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # try to find first [ ... ]
        m = re.search(r"\[[\s\S]*\]", text)
        if not m:
            raise RuntimeError(f"LLM did not return valid JSON:\n{raw}")
        data = json.loads(m.group(0))

    if not isinstance(data, list):
        raise RuntimeError(f"Expected JSON list of commits, got: {type(data)}")

    plans: list[CommitPlan] = []
    seen: set[str] = set()
    all_paths = {c.path for c in changes}

    for item in data:
        if not isinstance(item, dict):
            continue
        msg = str(item.get("message", "")).strip()
        files = item.get("files") or []
        if not msg or not isinstance(files, list):
            continue
        clean_files = []
        for f in files:
            f = str(f).strip()
            if f in all_paths and f not in seen:
                clean_files.append(f)
                seen.add(f)
        if clean_files:
            plans.append(CommitPlan(message=msg, files=clean_files))

    # Any leftover files go into a final catch-all commit
    leftover = [c.path for c in changes if c.path not in seen]
    if leftover:
        plans.append(
            CommitPlan(
                message="chore: remaining changes",
                files=leftover,
            )
        )

    return plans


def _max_mtime_for_paths(repo: Path, paths: list[str]) -> float | None:
    """Return the newest modification time among the given paths that still exist."""
    latest: float | None = None
    for p in paths:
        full = repo / p
        if full.exists() or full.is_symlink():
            try:
                m = full.stat().st_mtime
            except OSError:
                continue
            if latest is None or m > latest:
                latest = m
    return latest


def _format_git_date(timestamp: float) -> str:
    """Format a Unix timestamp for GIT_AUTHOR_DATE / GIT_COMMITTER_DATE."""
    # Use ISO-8601 with explicit UTC offset so the chronological order is
    # independent of the local timezone.
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S +0000")


def apply_commits(
    repo: Path,
    plans: list[CommitPlan],
    author_name: str,
    author_email: str,
) -> None:
    base_env = os.environ.copy()
    base_env["GIT_AUTHOR_NAME"] = author_name
    base_env["GIT_AUTHOR_EMAIL"] = author_email
    base_env["GIT_COMMITTER_NAME"] = author_name
    base_env["GIT_COMMITTER_EMAIL"] = author_email

    for i, plan in enumerate(
        tqdm(plans, desc="Creating commits", unit="commit", leave=True), start=1
    ):
        tqdm.write(f"\n=== Commit {i}/{len(plans)} ===")
        tqdm.write(f"Message: {plan.message}")
        tqdm.write(f"Files: {', '.join(plan.files)}")

        # Stage only the listed paths
        # Use git add -- for both new and modified; for deletions use git rm
        to_add = []
        for p in plan.files:
            full = repo / p
            if full.exists() or full.is_symlink():
                to_add.append(p)
            else:
                # deleted
                run_git(repo, "rm", "--ignore-unmatch", "-f", "--", p, check=False)

        if to_add:
            run_git(repo, "add", "--", *to_add)

        # Safety: make sure something is staged
        staged = run_git(repo, "diff", "--cached", "--name-only")
        if not staged.stdout.strip():
            tqdm.write("  (nothing staged – skipping)")
            continue

        # Derive commit date from the source files' modification times so that
        # a series of old archives produces a chronologically ordered history.
        env = base_env.copy()
        mtime = _max_mtime_for_paths(repo, plan.files)
        if mtime is not None:
            date_str = _format_git_date(mtime)
            env["GIT_AUTHOR_DATE"] = date_str
            env["GIT_COMMITTER_DATE"] = date_str
            tqdm.write(f"  date: {date_str} (from source file mtime)")
        else:
            # Pure deletion commit – fall back to current time
            tqdm.write("  date: (current time – no surviving files for mtime)")

        # Commit
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "commit",
                "-m",
                plan.message,
            ],
            check=True,
            env=env,
        )
        tqdm.write("  committed.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync a source directory into a Git repo and create AI-written commits "
        f"(max ~{MAX_LINES_PER_COMMIT} changed lines per commit when possible)."
    )
    parser.add_argument(
        "--url",
        required=True,
        help="OpenAI-compatible base URL, e.g. http://llamaserver:8080/v1",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Model name, e.g. qwen3.5-coder",
    )
    parser.add_argument(
        "--git",
        required=True,
        type=Path,
        help="Path to the local Git repository",
    )
    parser.add_argument(
        "--src",
        required=True,
        type=Path,
        help="Path to the newest source directory, or an archive "
        "(.zip / .tar / .tar.gz / …) that will be unpacked automatically",
    )
    parser.add_argument(
        "--name",
        required=True,
        help='Committer identity, e.g. "Botter <the-great-ai@localhost.at>"',
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENAI_API_KEY"),
        help="Optional API key (or set OPENAI_API_KEY). Many local servers ignore it.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the planned commits without writing anything",
    )
    args = parser.parse_args()

    author_name, author_email = parse_author(args.name)
    ensure_git_repo(args.git)

    # Optional: refuse if there are already staged changes
    staged = run_git(args.git, "diff", "--cached", "--name-only")
    if staged.stdout.strip():
        raise SystemExit(
            "Repository has staged changes. Please commit or unstage them first."
        )

    with resolve_source(args.src) as effective_src:
        # Warn & abort if the (plain) source tree has extra directory layers
        # relative to the repository.  Archives are already stripped by
        # resolve_source, so this mainly protects directory inputs.
        warning = detect_extra_directory_layer(effective_src, args.git)
        if warning:
            print("\n*** Extra directory layer detected ***\n")
            print(warning)
            raise SystemExit(1)

        print(f"Syncing {effective_src} → {args.git} …")
        if not args.dry_run:
            sync_source_to_repo(effective_src, args.git)
        else:
            print("  (dry-run: skip actual file sync)")

        print("Collecting changes …")
        changes = collect_changes(args.git)
        if not changes:
            print("No changes detected. Nothing to commit.")
            return

        print(f"Found {len(changes)} changed path(s):")
        for c in changes:
            print(f"  {c.status} {c.path}  (+{c.insertions}/-{c.deletions})")

        print("\nAsking model for commit plan …")
        plans = plan_commits(changes, args.url, args.model, args.api_key)

        if not plans:
            print("Model returned empty plan.")
            return

        print(f"\nPlanned {len(plans)} commit(s):")
        for i, p in enumerate(plans, 1):
            line_sum = sum(
                next((c.total_lines for c in changes if c.path == f), 0) for f in p.files
            )
            print(f"  {i}. [{line_sum} lines] {p.message}")
            for f in p.files:
                print(f"       - {f}")

        if args.dry_run:
            print("\nDry-run finished. No commits were created.")
            return

        apply_commits(args.git, plans, author_name, author_email)
        print("\nDone.")


if __name__ == "__main__":
    main()

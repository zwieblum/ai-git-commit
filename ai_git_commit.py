#!/usr/bin/env python3
"""
AI-assisted Git committer: sync a source directory into a local Git repo and
create one or more commits with LLM-generated messages.

Changes are grouped into functionally coherent commits. Each commit's total
changed lines (insertions + deletions) is kept at or below 200 when possible.
Single files larger than 200 lines are committed alone.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

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
    if not repo.is_dir():
        raise SystemExit(f"Git repository path does not exist or is not a directory: {repo}")
    r = run_git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    if r.returncode != 0 or r.stdout.strip() != "true":
        raise SystemExit(f"Not a valid Git working tree: {repo}")


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
            f"{src}/",
            f"{repo}/",
        ]
        subprocess.run(cmd, check=True)
    else:
        # Fallback: walk and copy, then remove extras
        for root, dirs, files in os.walk(src):
            rel = Path(root).relative_to(src)
            dest_dir = repo / rel
            dest_dir.mkdir(parents=True, exist_ok=True)
            for f in files:
                shutil.copy2(Path(root) / f, dest_dir / f)
        # Remove files present in repo but not in src (skip .git)
        for root, dirs, files in os.walk(repo, topdown=True):
            if Path(root).name == ".git" or ".git" in Path(root).parts:
                dirs.clear()
                continue
            rel = Path(root).relative_to(repo)
            src_dir = src / rel
            for f in files:
                if not (src_dir / f).exists():
                    (Path(root) / f).unlink()
            # remove empty dirs later if needed; git will handle


def collect_changes(repo: Path) -> list[FileChange]:
    """Return list of changed files with insertion/deletion counts."""
    changes: dict[str, FileChange] = {}

    # Tracked modifications / deletions / renames
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


def apply_commits(
    repo: Path,
    plans: list[CommitPlan],
    author_name: str,
    author_email: str,
) -> None:
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = author_name
    env["GIT_AUTHOR_EMAIL"] = author_email
    env["GIT_COMMITTER_NAME"] = author_name
    env["GIT_COMMITTER_EMAIL"] = author_email

    for i, plan in enumerate(plans, 1):
        print(f"\n=== Commit {i}/{len(plans)} ===")
        print(f"Message: {plan.message}")
        print(f"Files: {', '.join(plan.files)}")

        # Stage only the listed paths
        # Use git add -- for both new and modified; for deletions use git add -u or explicit
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
            print("  (nothing staged – skipping)")
            continue

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
        print("  committed.")


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
        help="Path to the newest (non-git) source directory",
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

    print(f"Syncing {args.src} → {args.git} …")
    if not args.dry_run:
        sync_source_to_repo(args.src, args.git)
    else:
        print("  (dry-run: skip actual file sync)")

    print("Collecting changes …")
    # In dry-run we still need the would-be changes; for simplicity re-sync is required
    # so dry-run after a real sync or assume user already synced.
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

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
MIN_MESSAGE_WORDS = 10
MAX_MESSAGE_WORDS = 100
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
class DiffHunk:
    """One unified-diff hunk belonging to a single file."""
    index: int
    header: str          # the @@ -a,b +c,d @@ line
    body: str            # lines following the header (including the header line)
    change_lines: int    # approximate insertions + deletions in this hunk


@dataclass
class CommitPlan:
    message: str
    files: list[str] = field(default_factory=list)
    # Optional partial-file application: path -> ordered list of hunk indices
    # When set, only those hunks of the file are committed (for large-file splits).
    hunks: dict[str, list[int]] = field(default_factory=dict)


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


def ensure_branch(repo: Path, branch: str) -> None:
    """Checkout *branch*, creating it if it does not exist.

    - Existing branch → ``git checkout NAME``
    - New branch with existing commits → ``git checkout -b NAME``
    - New branch in an empty repo (no HEAD) → ``git checkout -b NAME``
      (Git will create the branch on the first commit)
    """
    branch = branch.strip()
    if not branch:
        raise SystemExit("--branch must not be empty")
    if re.search(r"[\s~^:?*\[\\]", branch) or branch.startswith("-"):
        raise SystemExit(f"Invalid branch name: {branch!r}")

    # Already on the requested branch?
    cur = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    if cur.returncode == 0 and cur.stdout.strip() == branch:
        print(f"Already on branch '{branch}'")
        return

    # Does the branch already exist?
    exists = run_git(
        repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False
    )
    if exists.returncode == 0:
        print(f"Checking out existing branch '{branch}' …")
        run_git(repo, "checkout", branch)
        return

    # Create and switch
    if has_commits(repo):
        print(f"Creating and checking out new branch '{branch}' …")
        run_git(repo, "checkout", "-b", branch)
    else:
        # Empty repo: set the symbolic ref so the first commit lands on this branch
        print(f"Empty repository – first commit will create branch '{branch}' …")
        run_git(repo, "checkout", "-b", branch, check=False)
        # Some Git versions need: git symbolic-ref HEAD refs/heads/NAME
        run_git(repo, "symbolic-ref", "HEAD", f"refs/heads/{branch}", check=False)


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


def branch_name_from_archive(path: Path) -> str:
    """Derive a Git branch name from an archive filename (strip known extensions)."""
    name = path.name
    lower = name.lower()
    # Strip longest matching archive suffix first (e.g. .tar.gz before .gz)
    for suffix in sorted(ARCHIVE_SUFFIXES, key=len, reverse=True):
        if lower.endswith(suffix):
            name = name[: -len(suffix)]
            break
    # Sanitise for Git branch rules: no spaces, no leading '-', etc.
    name = name.strip().replace(" ", "-")
    name = re.sub(r"[~^:?*\[\\]+", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    if not name or name.startswith("-"):
        name = "import-archive"
    return name


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


def resolve_model(base_url: str, model: str | None, api_key: str | None = None) -> str:
    """Return *model*, or the first/default model advertised by the server.

    Uses ``GET {base_url}/models`` (OpenAI-compatible).  Prefers a model marked
    as the default/active one when the server provides such a hint; otherwise
    uses the first entry in ``data``.
    """
    if model:
        return model

    url = base_url.rstrip("/") + "/models"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    def _die(msg: str) -> None:
        print(msg, file=sys.stderr)
        sys.exit(1)

    try:
        resp = requests.get(url, headers=headers, timeout=API_TIMEOUT)
    except requests.exceptions.ConnectionError as e:
        _die(
            f"Error: cannot reach LLM server at {url} to list models.\n"
            f"  Provide --model explicitly, or start the server.\n"
            f"  Detail: {e}"
        )
    except requests.exceptions.Timeout as e:
        _die(
            f"Error: LLM server at {url} timed out while listing models.\n"
            f"  Detail: {e}"
        )
    except requests.exceptions.RequestException as e:
        _die(f"Error: failed to list models from {url}: {e}")

    if resp.status_code != 200:
        _die(
            f"Error: cannot list models (HTTP {resp.status_code}) at {url}.\n"
            f"  Provide --model explicitly.\n"
            f"  Response: {(resp.text or '')[:300]}"
        )

    try:
        data = resp.json()
    except ValueError:
        _die(
            f"Error: non-JSON response from {url}. Provide --model explicitly.\n"
            f"  Body: {(resp.text or '')[:300]}"
        )

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        # Some servers return a bare list
        if isinstance(data, list) and data:
            items = data
        else:
            _die(
                f"Error: no models reported by {url}. Provide --model explicitly.\n"
                f"  Response: {data!r}"
            )

    # Prefer an explicitly marked default/active model when present
    chosen = None
    for it in items:
        if not isinstance(it, dict):
            continue
        mid = it.get("id") or it.get("name")
        if not mid:
            continue
        if it.get("default") or it.get("active") or it.get("is_default"):
            chosen = str(mid)
            break
        if chosen is None:
            chosen = str(mid)

    if not chosen:
        _die(
            f"Error: could not determine a model id from {url}. "
            f"Provide --model explicitly.\n  Response: {data!r}"
        )

    print(f"No --model given – using server model '{chosen}'")
    return chosen


def call_llm(
    base_url: str,
    model: str,
    system: str,
    user: str,
    api_key: str | None = None,
) -> str:
    """Call OpenAI-compatible /v1/chat/completions.

    Raises SystemExit with a clear message on network / HTTP / response errors
    so the program does not crash with an unhandled traceback.
    """
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
    def _die(msg: str) -> None:
        """Print a clear error and exit without a long exception chain."""
        print(msg, file=sys.stderr)
        sys.exit(1)

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=API_TIMEOUT)
    except requests.exceptions.ConnectionError as e:
        _die(
            f"Error: cannot reach LLM server at {url}\n"
            f"  Check that the server is running and --url is correct.\n"
            f"  Detail: {e}"
        )
    except requests.exceptions.Timeout as e:
        _die(
            f"Error: LLM server at {url} did not respond within {API_TIMEOUT}s.\n"
            f"  The model may be overloaded or the URL may be wrong.\n"
            f"  Detail: {e}"
        )
    except requests.exceptions.RequestException as e:
        _die(f"Error: request to LLM server failed: {e}")

    if resp.status_code != 200:
        body = (resp.text or "")[:500]
        lower = body.lower()
        if resp.status_code in (404, 503) or (
            "model" in lower
            and (
                "not found" in lower
                or "unavailable" in lower
                or "does not exist" in lower
            )
        ):
            _die(
                f"Error: LLM model '{model}' appears unavailable "
                f"(HTTP {resp.status_code}).\n"
                f"  URL: {url}\n"
                f"  Response: {body}"
            )
        _die(
            f"Error: LLM server returned HTTP {resp.status_code}.\n"
            f"  URL: {url}\n"
            f"  Response: {body}"
        )

    try:
        data = resp.json()
    except ValueError:
        _die(
            f"Error: LLM server returned non-JSON response.\n"
            f"  URL: {url}\n"
            f"  Body (first 500 chars): {(resp.text or '')[:500]}"
        )

    # Some servers put errors in a 200 body
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        msg = err.get("message", err) if isinstance(err, dict) else err
        _die(f"Error: LLM server error for model '{model}': {msg}")

    try:
        content = data["choices"][0]["message"]["content"]
        if content is None:
            raise KeyError("content is null")
        return content.strip()
    except (KeyError, IndexError, TypeError):
        _die(
            f"Error: unexpected LLM response format "
            f"(model '{model}' may be unavailable or returned an empty completion).\n"
            f"  Response: {data!r}"
        )


def _parse_unified_hunks(diff_text: str) -> list[DiffHunk]:
    """Parse a unified diff (possibly multi-file) into per-hunk objects.

    Only the hunk bodies are kept; file headers are ignored here (caller
    already knows the path).
    """
    hunks: list[DiffHunk] = []
    lines = diff_text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("@@"):
            header = line.rstrip("\n")
            body_lines = [line]
            i += 1
            while i < len(lines) and not lines[i].startswith("@@") and not lines[i].startswith("diff --git"):
                body_lines.append(lines[i])
                i += 1
            body = "".join(body_lines)
            # count +/- lines (ignore @@ and context ' ')
            change = sum(
                1
                for bl in body_lines
                if bl.startswith("+") or bl.startswith("-")
            ) - sum(1 for bl in body_lines if bl.startswith("+++") or bl.startswith("---"))
            # simpler approximate: every + or - that is not a file header
            change = 0
            for bl in body_lines[1:]:
                if bl.startswith("+") or bl.startswith("-"):
                    change += 1
            hunks.append(
                DiffHunk(
                    index=len(hunks),
                    header=header,
                    body=body,
                    change_lines=change,
                )
            )
            continue
        i += 1
    return hunks


def _get_file_diff(repo: Path, path: str) -> str:
    """Return the unified diff of *path* against HEAD (or the whole file if new).

    Untracked files are not shown by ``git diff HEAD -- path``, so we detect
    that case and synthesise a "new file" diff from the working-tree content.
    """
    full = repo / path

    if has_commits(repo):
        in_head = run_git(
            repo, "ls-tree", "HEAD", "--", path, check=False
        ).stdout.strip()
        if in_head:
            r = run_git(repo, "diff", "HEAD", "--", path, check=False)
            return r.stdout or ""
        # File is not in HEAD → treat as new (fall through to synthesise)

    # Brand-new repo or untracked file: synthesise a "new file" diff
    if not full.is_file():
        return ""
    try:
        content = full.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = content.splitlines(keepends=True)
    if not lines:
        return ""
    header = (
        f"diff --git a/{path} b/{path}\n"
        f"new file mode 100644\n"
        f"--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
    )
    body = "".join("+" + (ln if ln.endswith("\n") else ln + "\n") for ln in lines)
    return header + body


def _build_partial_patch(path: str, all_hunks: list[DiffHunk], indices: list[int], is_new: bool) -> str:
    """Build a valid unified diff that contains only the selected hunks."""
    selected = [all_hunks[i] for i in indices if 0 <= i < len(all_hunks)]
    if not selected:
        return ""
    if is_new and indices == list(range(len(all_hunks))):
        # full new file – return complete synthetic diff is handled by caller
        pass
    parts = [
        f"diff --git a/{path} b/{path}\n",
    ]
    if is_new and indices[0] == 0:
        parts.append(f"new file mode 100644\n")
        parts.append(f"--- /dev/null\n")
        parts.append(f"+++ b/{path}\n")
    else:
        parts.append(f"--- a/{path}\n")
        parts.append(f"+++ b/{path}\n")
    for h in selected:
        parts.append(h.body if h.body.endswith("\n") else h.body + "\n")
    return "".join(parts)


def _extract_json_array(raw: str) -> list:
    """Extract a JSON array from an LLM response (tolerates markdown fences)."""
    text = raw
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[[\s\S]*\]", text)
        if not m:
            raise RuntimeError(f"LLM did not return valid JSON:\n{raw}")
        data = json.loads(m.group(0))
    if not isinstance(data, list):
        raise RuntimeError(f"Expected JSON list, got: {type(data)}")
    return data


def _word_count(text: str) -> int:
    return len(text.split())


def normalize_message(msg: str, files: list[str], reserve_words: int = 0) -> str:
    """Force a commit message to MIN_MESSAGE_WORDS..MAX_MESSAGE_WORDS words.

    ``reserve_words`` words are kept free for a suffix appended later (e.g. the
    archive tag).  Too-long messages are cut (preferably at a sentence/line
    end); too-short ones are extended with the affected file names.
    """
    msg = msg.strip()
    limit = max(1, MAX_MESSAGE_WORDS - reserve_words)
    if _word_count(msg) > limit:
        # Keep original line structure while cutting at the word limit.
        out: list[str] = []
        used = 0
        for line in msg.splitlines():
            n = _word_count(line)
            if used + n <= limit:
                out.append(line)
                used += n
            else:
                room = limit - used
                if room > 0 and line.strip():
                    out.append(" ".join(line.split()[:room]))
                break
        msg = "\n".join(out).strip()
    minimum = max(0, MIN_MESSAGE_WORDS - reserve_words)
    if _word_count(msg) < minimum:
        shown = ", ".join(files[:3]) + (" and others" if len(files) > 3 else "")
        msg = f"{msg.rstrip('. ')}: update {shown} to keep the codebase current and consistent"
        if _word_count(msg) < minimum:
            msg += " with the latest source changes"
        if _word_count(msg) > limit:
            msg = " ".join(msg.split()[:limit])
    return msg


def plan_commits(
    changes: list[FileChange],
    repo: Path,
    base_url: str,
    model: str,
    api_key: str | None,
    archive_name: str | None = None,
) -> list[CommitPlan]:
    if not changes:
        return []

    large = [c for c in changes if c.total_lines > MAX_LINES_PER_COMMIT and c.status != "D"]
    small = [c for c in changes if c not in large]

    archive_note = ""
    if archive_name:
        archive_note = (
            f"\n- These changes come from the archive '{archive_name}'. "
            f"Mention the archive name in each commit message "
            f"(e.g. append \" (from {archive_name})\" or weave it into the subject).\n"
        )

    # ---- 1) Plan for small files / deletions (file-level) ----
    plans: list[CommitPlan] = []
    if small:
        lines = []
        total = 0
        for c in small:
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
            "- Group files that belong to the same functional change (feature, fix, refactor, docs, etc.).\n"
            "- Order commits so that earlier commits do not depend on later ones when possible.\n"
            "- Commit messages: imperative mood, concise subject (<=72 chars), optional body; "
            f"the whole message must be concise but between {MIN_MESSAGE_WORDS} and {MAX_MESSAGE_WORDS} words long.\n"
            f"{archive_note}"
            "- Output ONLY valid JSON: a list of objects, each with keys \"message\" (string) and \"files\" (list of strings).\n"
            "- Every listed file must appear in exactly one commit.\n"
            "- Do not invent file paths."
        )
        user = (
            f"The following files have changed (total ~{total} lines)"
            + (f" from archive '{archive_name}'" if archive_name else "")
            + f":\n\n{summary}\n\n"
            "Produce a list of commits that apply these changes in a sensible order."
        )
        raw = call_llm(base_url, model, system, user, api_key)
        data = _extract_json_array(raw)
        seen: set[str] = set()
        all_small = {c.path for c in small}
        for item in data:
            if not isinstance(item, dict):
                continue
            msg = str(item.get("message", "")).strip()
            files = item.get("files") or []
            if not msg or not isinstance(files, list):
                continue
            clean = []
            for f in files:
                f = str(f).strip()
                if f in all_small and f not in seen:
                    clean.append(f)
                    seen.add(f)
            if clean:
                plans.append(CommitPlan(message=msg, files=clean))
        leftover = [c.path for c in small if c.path not in seen]
        if leftover:
            plans.append(CommitPlan(message="chore: remaining small changes", files=leftover))

    # ---- 2) Large files: ask the model to split into functional hunks ----
    for c in large:
        print(f"  Large file {c.path} ({c.total_lines} lines) – asking model to plan functional hunks …")
        diff_text = _get_file_diff(repo, c.path)
        hunks = _parse_unified_hunks(diff_text)
        if not hunks:
            # fallback: treat whole file as one commit
            plans.append(CommitPlan(message=f"update {c.path}", files=[c.path]))
            continue

        # Build a readable hunk summary for the model (truncate very long bodies)
        hunk_summaries = []
        for h in hunks:
            preview = h.body.strip().splitlines()
            preview = preview[:8]  # first few lines
            preview_txt = "\n".join(preview)
            if len(h.body.splitlines()) > 8:
                preview_txt += "\n…"
            hunk_summaries.append(
                f"HUNK {h.index}  ({h.change_lines} changed lines)\n{h.header}\n{preview_txt}"
            )
        hunk_block = "\n\n".join(hunk_summaries)

        system = (
            "You are an expert software engineer. A single large file has many changes. "
            "Split them into functionally coherent commits.\n"
            "Rules:\n"
            f"- Each commit should preferably contain at most {MAX_LINES_PER_COMMIT} changed lines "
            "(sum of the hunk change_lines you assign to it).\n"
            "- Group consecutive or related hunks that implement the same logical change.\n"
            "- Hunks must be applied in ascending index order; do not reorder them across commits "
            "in a way that would break sequential application (each commit's hunk indices must be "
            "increasing, and commits themselves ordered by the smallest hunk index they contain).\n"
            "- Every hunk index from 0 to N-1 must appear in exactly one commit.\n"
            "- Commit messages: imperative mood, concise subject (<=72 chars), optional body; "
            f"the whole message must be concise but between {MIN_MESSAGE_WORDS} and {MAX_MESSAGE_WORDS} words long.\n"
            f"{archive_note}"
            "- Output ONLY valid JSON: a list of objects with keys:\n"
            '  "message" (string), "hunks" (list of integer hunk indices).\n'
            "- Do not invent hunk indices."
        )
        user = (
            f"File: {c.path}  (status={c.status}, +{c.insertions}/-{c.deletions})"
            + (f" from archive '{archive_name}'" if archive_name else "")
            + f"\nTotal hunks: {len(hunks)}\n\n"
            f"{hunk_block}\n\n"
            "Produce the list of commits that split this file into functional hunks."
        )
        raw = call_llm(base_url, model, system, user, api_key)
        data = _extract_json_array(raw)

        assigned: set[int] = set()
        file_plans: list[CommitPlan] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            msg = str(item.get("message", "")).strip()
            idxs = item.get("hunks") or item.get("files") or []
            if not msg or not isinstance(idxs, list):
                continue
            clean_idx = []
            for x in idxs:
                try:
                    xi = int(x)
                except (TypeError, ValueError):
                    continue
                if 0 <= xi < len(hunks) and xi not in assigned:
                    clean_idx.append(xi)
                    assigned.add(xi)
            clean_idx.sort()
            if clean_idx:
                file_plans.append(
                    CommitPlan(
                        message=msg,
                        files=[c.path],
                        hunks={c.path: clean_idx},
                    )
                )

        # any leftover hunks → final catch-all for this file
        missing = sorted(set(range(len(hunks))) - assigned)
        if missing:
            file_plans.append(
                CommitPlan(
                    message=f"chore: remaining changes in {c.path}",
                    files=[c.path],
                    hunks={c.path: missing},
                )
            )

        # keep sequential order by first hunk index
        file_plans.sort(key=lambda p: min(p.hunks.get(c.path, [0])))
        plans.extend(file_plans)

    # Ensure the archive name appears in every commit message when applicable,
    # then enforce the word-count limits on every message.
    tag = f"(from {archive_name})" if archive_name else ""
    for p in plans:
        needs_tag = bool(tag) and tag not in p.message and archive_name not in p.message
        p.message = normalize_message(
            p.message, p.files, reserve_words=_word_count(tag) if needs_tag else 0
        )
        if needs_tag:
            p.message = f"{p.message.rstrip()} {tag}"

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

    # Per-file state for sequential hunk application:
    #   path -> {"final": bytes, "hunks": list[DiffHunk], "applied": set[int], "is_new": bool}
    large_state: dict[str, dict] = {}

    # Snapshot which paths are new relative to the *initial* HEAD (before we
    # create any commits in this run).  Later commits must not flip is_new.
    initially_new: set[str] = set()
    for plan in plans:
        for path in list(plan.files) + list(plan.hunks.keys()):
            if path in initially_new:
                continue
            if not has_commits(repo):
                initially_new.add(path)
            else:
                in_head = run_git(
                    repo, "ls-tree", "HEAD", "--", path, check=False
                ).stdout.strip()
                if not in_head:
                    initially_new.add(path)

    def _ensure_large_state(path: str) -> dict:
        if path in large_state:
            return large_state[path]
        full = repo / path
        final = full.read_bytes() if full.exists() else b""
        # Prefer the already-saved final content for diff synthesis if the
        # worktree file was altered; for planning-time consistency we re-read
        # from worktree when present.
        diff_text = _get_file_diff(repo, path)
        hunks = _parse_unified_hunks(diff_text)
        is_new = path in initially_new
        large_state[path] = {
            "final": final,
            "hunks": hunks,
            "applied": set(),
            "is_new": is_new,
        }
        return large_state[path]

    for i, plan in enumerate(
        tqdm(plans, desc="Creating commits", unit="commit", leave=True), start=1
    ):
        tqdm.write(f"\n=== Commit {i}/{len(plans)} ===")
        tqdm.write(f"Message: {plan.message}")
        tqdm.write(f"Files: {', '.join(plan.files)}")
        if plan.hunks:
            for p, idxs in plan.hunks.items():
                tqdm.write(f"  hunks[{p}]: {idxs}")

        if plan.hunks:
            # Partial / sequential hunk application for large files
            for path, idxs in plan.hunks.items():
                st = _ensure_large_state(path)
                st["applied"].update(idxs)
                cumulative = sorted(st["applied"])
                all_idx = set(range(len(st["hunks"])))
                full = repo / path

                # New files: always stage the full final content.  Splitting a
                # brand-new file into intermediate patch steps is unreliable
                # (no base in HEAD); the model still produces a useful message.
                if st["is_new"]:
                    full.parent.mkdir(parents=True, exist_ok=True)
                    full.write_bytes(st["final"])
                    run_git(repo, "add", "--", path, check=False)
                    st["applied"] = set(all_idx)  # mark complete
                    continue

                # Existing file: restore base then apply cumulative hunks
                run_git(repo, "checkout", "HEAD", "--", path, check=False)

                patch = _build_partial_patch(
                    path, st["hunks"], cumulative, is_new=st["is_new"]
                )
                applied_ok = False
                if patch:
                    with tempfile.NamedTemporaryFile(
                        mode="w", suffix=".patch", delete=False, encoding="utf-8"
                    ) as tf:
                        tf.write(patch)
                        patch_path = tf.name
                    try:
                        r = subprocess.run(
                            [
                                "git",
                                "-C",
                                str(repo),
                                "apply",
                                "--recount",
                                "--whitespace=nowarn",
                                patch_path,
                            ],
                            capture_output=True,
                            text=True,
                        )
                        applied_ok = r.returncode == 0
                        if not applied_ok:
                            tqdm.write(
                                f"  warning: git apply failed for {path}:\n{r.stderr.strip()}"
                            )
                    finally:
                        os.unlink(patch_path)

                if not applied_ok:
                    # Fallback: write final content when this is the complete set
                    if st["applied"] >= all_idx or st["is_new"]:
                        full.parent.mkdir(parents=True, exist_ok=True)
                        full.write_bytes(st["final"])
                        tqdm.write(f"  (wrote final content for {path})")
                    else:
                        tqdm.write(
                            f"  warning: could not apply intermediate hunks for {path}; "
                            "skipping stage for this commit"
                        )
                        continue

                if full.exists() or full.is_symlink():
                    run_git(repo, "add", "--", path, check=False)
                else:
                    tqdm.write(f"  warning: {path} missing after apply – not staged")
        else:
            # Full-file staging (small files / deletions)
            to_add = []
            for p in plan.files:
                full = repo / p
                if full.exists() or full.is_symlink():
                    to_add.append(p)
                else:
                    run_git(repo, "rm", "--ignore-unmatch", "-f", "--", p, check=False)
            if to_add:
                run_git(repo, "add", "--", *to_add)

        # Safety: make sure something is staged
        staged = run_git(repo, "diff", "--cached", "--name-only")
        if not staged.stdout.strip():
            tqdm.write("  (nothing staged – skipping)")
            continue

        # Derive commit date from the source files' modification times
        env = base_env.copy()
        # For hunk commits prefer the final-file mtime (stored in large_state)
        mtime = None
        for p in plan.files:
            if p in large_state and large_state[p]["final"]:
                # use a synthetic mtime from the final bytes via the live file if present
                full = repo / p
                if full.exists():
                    try:
                        mtime = full.stat().st_mtime
                    except OSError:
                        pass
        if mtime is None:
            mtime = _max_mtime_for_paths(repo, plan.files)
        if mtime is not None:
            date_str = _format_git_date(mtime)
            env["GIT_AUTHOR_DATE"] = date_str
            env["GIT_COMMITTER_DATE"] = date_str
            tqdm.write(f"  date: {date_str} (from source file mtime)")
        else:
            tqdm.write("  date: (current time – no surviving files for mtime)")

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

    # After all commits, ensure large files match the final synced content
    for path, st in large_state.items():
        full = repo / path
        if st["final"] and (not full.exists() or full.read_bytes() != st["final"]):
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(st["final"])
            run_git(repo, "add", "--", path)
            # if there is a remaining diff, it will show as dirty – user can fix;
            # normally the last hunk commit should already match


def list_local_branches(repo: Path) -> list[str]:
    """Return local branch names (without refs/heads/)."""
    r = run_git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def resolve_export_branch(repo: Path, name: str | None) -> str:
    """Resolve which branch to export.

    * ``None`` / ``__LAST__`` → branch that contains the most recent commit
      (by committer date) among local branches; falls back to current HEAD branch.
    * Otherwise treat *name* as a branch name, or as an archive basename
      (with or without extension) matching ``branch_name_from_archive``.
    """
    branches = list_local_branches(repo)
    if not branches:
        raise SystemExit("No local branches found – nothing to export.")

    if name is None or name == "__LAST__":
        # Branch whose tip has the newest committer date
        r = run_git(
            repo,
            "for-each-ref",
            "--sort=-committerdate",
            "--format=%(refname:short)",
            "refs/heads/",
            check=False,
        )
        ordered = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        if ordered:
            return ordered[0]
        cur = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False)
        if cur.returncode == 0 and cur.stdout.strip() not in ("", "HEAD"):
            return cur.stdout.strip()
        raise SystemExit("Could not determine the last branch to export.")

    # Exact branch match
    if name in branches:
        return name

    # Archive-style name → strip extension and match
    candidate = branch_name_from_archive(Path(name))
    if candidate in branches:
        return candidate

    # Case-insensitive match
    lower_map = {b.lower(): b for b in branches}
    if name.lower() in lower_map:
        return lower_map[name.lower()]
    if candidate.lower() in lower_map:
        return lower_map[candidate.lower()]

    raise SystemExit(
        f"No branch matching '{name}' (tried '{candidate}').\n"
        f"  Available branches: {', '.join(branches)}"
    )


def export_archive(
    repo: Path,
    branch: str,
    output: Path | None = None,
    fmt: str = "zip",
    prefix: str | None = None,
) -> Path:
    """Recreate an archive from the tree at *branch* using ``git archive``.

    Returns the path of the written archive.
    """
    fmt = fmt.lower().strip()
    if fmt in ("tar.gz", "tgz"):
        git_format = "tar.gz"
        ext = ".tar.gz"
    elif fmt in ("tar",):
        git_format = "tar"
        ext = ".tar"
    else:
        git_format = "zip"
        ext = ".zip"

    if output is None:
        output = Path(f"{branch}{ext}")
    else:
        output = output.expanduser().resolve()
        if output.is_dir():
            output = output / f"{branch}{ext}"

    # Prefix so unpacking creates a single top-level directory (matches import strip)
    if prefix is None:
        prefix = f"{branch}/"

    output.parent.mkdir(parents=True, exist_ok=True)
    print(f"Exporting branch '{branch}' → {output} ({git_format}) …")
    cmd = [
        "git",
        "-C",
        str(repo),
        "archive",
        f"--format={git_format}",
        f"--prefix={prefix}",
        f"--output={output}",
        branch,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(
            f"git archive failed:\n{r.stderr or r.stdout}"
        )
    size = output.stat().st_size if output.exists() else 0
    print(f"  wrote {output} ({size} bytes)")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "AI Git Committer – import snapshots into Git with LLM commit messages,\n"
            "or export/recreate archives from existing branches.\n"
            "\n"
            "Two modes (only --git is always required):\n"
            "\n"
            "  IMPORT  – sync --src into --git and create commits (needs LLM)\n"
            "            requires: --git --url --src --name\n"
            "            optional: --model (default: first/active model from GET /v1/models)\n"
            "\n"
            "  EXPORT  – recreate a zip/tar from a branch\n"
            "            requires: --git --export [NAME]\n"
            "            does NOT need --url, --model, --src, or --name"
        ),
        epilog=(
            "examples:\n"
            "  # Import an archive (creates branch from archive name)\n"
            "  %(prog)s --git ./repo --url http://localhost:8080/v1 \\\n"
            "      --src snapshot-2020.zip --name 'Botter <ai@localhost>'\n"
            "\n"
            "  # Import with an explicit model\n"
            "  %(prog)s --git ./repo --url http://localhost:8080/v1 \\\n"
            "      --model qwen2.5-coder --src snapshot-2020.zip \\\n"
            "      --name 'Botter <ai@localhost>'\n"
            "\n"
            "  # Export the last imported archive (newest branch tip)\n"
            "  %(prog)s --git ./repo --export\n"
            "\n"
            "  # Export a specific branch / archive name\n"
            "  %(prog)s --git ./repo --export 013-llm-errors\n"
            "  %(prog)s --git ./repo --export 013-llm-errors.zip \\\n"
            "      --export-format tar.gz --export-output /tmp/out.tar.gz\n"
        ),
    )
    # --- always ---
    parser.add_argument(
        "--git",
        required=True,
        type=Path,
        help="Path to the local Git repository (created on import if missing)",
    )
    # --- import mode ---
    import_opts = parser.add_argument_group(
        "import mode (required together unless --export is used)"
    )
    import_opts.add_argument(
        "--url",
        default=None,
        help="OpenAI-compatible base URL, e.g. http://localhost:8080/v1",
    )
    import_opts.add_argument(
        "--model",
        default=None,
        help="Model name (default: first/active model from GET {url}/models)",
    )
    import_opts.add_argument(
        "--src",
        default=None,
        type=Path,
        help="Source directory or archive (.zip / .tar / .tar.gz / …)",
    )
    import_opts.add_argument(
        "--name",
        default=None,
        help='Committer identity, e.g. "Botter <ai@localhost>"',
    )
    import_opts.add_argument(
        "--api-key",
        default=os.environ.get("OPENAI_API_KEY"),
        help="API key (or env OPENAI_API_KEY); often unused by local servers",
    )
    import_opts.add_argument(
        "--branch",
        default=None,
        metavar="NAME",
        help="Branch to commit on (default: archive basename if --src is an archive)",
    )
    import_opts.add_argument(
        "--mention-archive",
        action="store_true",
        help="Append the archive filename to each commit message (default: off)",
    )
    import_opts.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan commits only; do not sync or write commits",
    )
    # --- export mode ---
    export_opts = parser.add_argument_group(
        "export mode (does not need --url / --model / --src / --name)"
    )
    export_opts.add_argument(
        "--export",
        nargs="?",
        const="__LAST__",
        default=None,
        metavar="NAME",
        help="Export mode: recreate archive from branch. "
        "Omit NAME for the branch with the newest commit; "
        "NAME may be a branch or archive basename (e.g. 013-llm-errors[.zip])",
    )
    export_opts.add_argument(
        "--export-format",
        default="zip",
        choices=["zip", "tar", "tar.gz", "tgz"],
        help="Archive format for --export (default: zip)",
    )
    export_opts.add_argument(
        "--export-output",
        type=Path,
        default=None,
        metavar="PATH",
        help="Output file or directory for --export (default: <branch>.zip in cwd)",
    )
    args = parser.parse_args()

    # ----- export mode -----
    if args.export is not None:
        if not args.git.is_dir() or not (
            run_git(args.git, "rev-parse", "--is-inside-work-tree", check=False).stdout.strip()
            == "true"
            if args.git.is_dir()
            else False
        ):
            # validate without auto-creating
            if not args.git.exists():
                raise SystemExit(f"Git repository does not exist: {args.git}")
            r = run_git(args.git, "rev-parse", "--is-inside-work-tree", check=False)
            if r.returncode != 0 or r.stdout.strip() != "true":
                raise SystemExit(f"Not a valid Git working tree: {args.git}")
        branch = resolve_export_branch(
            args.git, None if args.export == "__LAST__" else args.export
        )
        export_archive(
            args.git,
            branch,
            output=args.export_output,
            fmt=args.export_format,
        )
        return

    ensure_git_repo(args.git)

    # ----- import mode -----
    missing = [
        flag
        for flag, val in (
            ("--url", args.url),
            ("--src", args.src),
            ("--name", args.name),
        )
        if not val
    ]
    if missing:
        raise SystemExit(
            "Import mode requires: " + ", ".join(missing) + "\n"
            "(--model is optional; the active server model is used when omitted.)\n"
            "(Or use --export [NAME] to recreate an archive from the repo.)"
        )

    args.model = resolve_model(args.url, args.model, args.api_key)
    author_name, author_email = parse_author(args.name)

    # Optional: refuse if there are already staged changes
    staged = run_git(args.git, "diff", "--cached", "--name-only")
    if staged.stdout.strip():
        raise SystemExit(
            "Repository has staged changes. Please commit or unstage them first."
        )

    # Branch selection: explicit --branch wins; otherwise derive from archive name
    branch = args.branch
    if branch is None and is_archive(args.src.expanduser().resolve()):
        branch = branch_name_from_archive(args.src)
        print(f"Archive detected – using branch name '{branch}' (override with --branch)")
    if branch:
        ensure_branch(args.git, branch)

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
        archive_label = None
        src_path = args.src.expanduser().resolve()
        if args.mention_archive and is_archive(src_path):
            archive_label = src_path.name  # keep full name including extension
        plans = plan_commits(
            changes, args.git, args.url, args.model, args.api_key,
            archive_name=archive_label,
        )

        if not plans:
            print("Model returned empty plan.")
            return

        print(f"\nPlanned {len(plans)} commit(s):")
        for i, p in enumerate(plans, 1):
            if p.hunks:
                print(f"  {i}. {p.message}")
                for f, idxs in p.hunks.items():
                    print(f"       - {f}  (hunks {idxs})")
            else:
                line_sum = sum(
                    next((c.total_lines for c in changes if c.path == f), 0)
                    for f in p.files
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

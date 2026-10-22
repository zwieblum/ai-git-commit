#!/usr/bin/env python3
"""
AI-assisted Git committer: sync a source directory into a local Git repo and
create one or more commits with LLM-generated messages.

Changes are grouped into functionally coherent commits (the model plans; large files are split hunk by hunk by the program). Each commit's total
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
# Never sync or commit Python bytecode caches.
IGNORED_DIR_NAMES = ("__pycache__",)
IGNORED_FILE_SUFFIXES = (".pyc", ".pyo")


def is_ignored_path(path: str) -> bool:
    """True for bytecode-cache paths that must never be added to the repo."""
    parts = Path(path).parts
    return any(d in parts for d in IGNORED_DIR_NAMES) or path.endswith(IGNORED_FILE_SUFFIXES)


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
    """One unified-diff hunk belonging to a single file.

    ``old_lines`` / ``new_lines`` hold the exact bytes (with line endings) the
    hunk removes/context-matches and inserts/context-keeps, so the program can
    apply the hunk itself without ``git apply``.
    """
    index: int
    header: str          # the @@ -a,b +c,d @@ line
    body: str            # the hunk text (including the header line), for display
    change_lines: int    # insertions + deletions in this hunk
    old_start: int = 0
    old_count: int = 0
    old_lines: list[bytes] = field(default_factory=list)
    new_lines: list[bytes] = field(default_factory=list)

    @property
    def pos(self) -> int:
        """0-based index in the ORIGINAL file where old_lines start."""
        return self.old_start if self.old_count == 0 else self.old_start - 1


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


_EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+$")


def _slug(text: str) -> str:
    """Make a safe e-mail local part from free text ('Bob Smith' -> 'bob.smith')."""
    out = re.sub(r"[^a-z0-9._+-]+", ".", text.lower()).strip(".")
    return re.sub(r"\.{2,}", ".", out) or "unknown"


def parse_author(name_email: str) -> tuple[str, str]:
    """Parse the --name value into ``(name, email)``, leniently.

    Accepted forms (first match wins):

    * ``Name <user@host>``   – the canonical form
    * ``<user@host>``        – name is derived from the local part (``user``)
    * ``user@host``          – dito (bare e-mail)
    * ``Name user@host``     – e-mail without angle brackets
    * ``Name <garbage>``     – bracket content that is not an e-mail is
      turned into ``garbage@localhost``
    * ``Name``               – e-mail becomes ``<name-slug>@localhost``

    Anything inferred is announced with a short note; only a completely empty
    value is an error.  Characters Git refuses in identities (``<``, ``>``,
    line breaks) are removed.
    """
    raw = " ".join((name_email or "").split())  # collapse whitespace/newlines
    if not raw:
        raise SystemExit("--name must not be empty (expected 'Name <email@domain>')")

    name = email = ""
    m = re.match(r"^(.*?)\s*<([^<>]*)>?\s*$", raw)  # tolerate a missing '>'
    if m:
        name, inner = m.group(1).strip(), m.group(2).strip()
        if _EMAIL_RE.match(inner):
            email = inner
        elif inner:
            email = f"{_slug(inner)}@localhost"
    else:
        tokens = [t for t in re.split(r"[\s,;]+", raw) if t]
        # last token that looks like an e-mail address
        for k in range(len(tokens) - 1, -1, -1):
            cand = tokens[k].strip("<>")
            if _EMAIL_RE.match(cand):
                email = cand
                name = " ".join(tokens[:k] + tokens[k + 1:])
                break
        else:
            name = raw

    name = name.replace("<", "").replace(">", "").strip().strip("\"'").strip()
    if not name and email:
        name = email.split("@", 1)[0]
    if not email:
        email = f"{_slug(name)}@localhost"
    if not name:
        name = "unknown"

    canonical = f"{name} <{email}>"
    if canonical != raw:
        print(f"Note: --name {name_email!r} interpreted as {canonical!r}")
    return name, email


def require_git_repo(repo: Path) -> None:
    """Exit with an error unless *repo* is an existing Git working tree
    (unlike ``ensure_git_repo`` this never creates anything)."""
    if not repo.exists():
        raise SystemExit(f"Git repository does not exist: {repo}")
    r = run_git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    if r.returncode != 0 or r.stdout.strip() != "true":
        raise SystemExit(f"Not a valid Git working tree: {repo}")


def list_branches(repo: Path) -> None:
    """Print all branches exactly like ``git branch --all``."""
    r = run_git(repo, "branch", "--all", "--no-color", check=False)
    if r.returncode != 0:
        raise SystemExit(f"Could not list branches: {r.stderr.strip()}")
    if r.stdout.strip():
        print(r.stdout.rstrip("\n"))
    else:
        print("No branches found.")


def current_branch(repo: Path) -> str:
    r = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def revert_last_branch(repo: Path) -> None:
    """Remove the last branch (see ``latest_release_branch``) from *repo*.

    * Refuses to remove the only remaining branch.
    * Refuses when the working tree has uncommitted/untracked changes.
    * If the branch is checked out, the previous branch is checked out first.
    * Prints the tip commit so the branch can be restored with
      ``git branch NAME SHA``.
    """
    require_git_repo(repo)
    branches = list_local_branches(repo)
    if not branches:
        raise SystemExit("No local branches found – nothing to revert.")
    if len(branches) == 1:
        raise SystemExit(
            f"Only one branch ('{branches[0]}') exists – refusing to remove it."
        )
    last = latest_release_branch(repo)
    if not last:
        raise SystemExit("Could not determine the last branch.")

    if run_git(repo, "status", "--porcelain", check=False).stdout.strip():
        raise SystemExit(
            f"Working tree of {repo} has uncommitted or untracked changes "
            f"(e.g. from a failed import) – refusing to switch branches.\n"
            f"Discard them first if you are sure:\n"
            f"  git -C {repo} reset --hard && git -C {repo} clean -fd"
        )

    sha = run_git(repo, "rev-parse", f"refs/heads/{last}").stdout.strip()
    now_on = current_branch(repo)
    if now_on == last:
        fallback = latest_release_branch(repo, exclude=last)
        r = run_git(repo, "checkout", "--quiet", fallback, check=False)
        if r.returncode != 0:
            raise SystemExit(
                f"Could not switch to '{fallback}' before removing '{last}':\n"
                f"{r.stderr.strip()}"
            )
        now_on = fallback
    r = run_git(repo, "branch", "-D", last, check=False)
    if r.returncode != 0:
        raise SystemExit(f"Could not remove branch '{last}':\n{r.stderr.strip()}")

    print(f"Removed branch '{last}' (was {sha[:12]}); now on '{now_on}'.")
    print(f"Restore with: git -C {repo} branch {last} {sha}")


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


def latest_release_branch(repo: Path, exclude: str | None = None) -> str | None:
    """Return the branch holding the most recent release.

    Release branches are named like ``017-export-model`` (derived from archive
    names).  The highest leading number wins; if no branch has a numeric
    prefix, the branch whose tip has the newest committer date is used.
    *exclude* leaves one branch out of the choice.
    """
    r = run_git(
        repo, "for-each-ref", "--sort=-committerdate",
        "--format=%(refname:short)", "refs/heads/", check=False,
    )
    ordered = [
        ln.strip() for ln in r.stdout.splitlines()
        if ln.strip() and ln.strip() != exclude
    ]
    if not ordered:
        return None
    numbered = []
    for b in ordered:
        m = re.match(r"^(\d+)", b)
        if m:
            numbered.append((int(m.group(1)), b))
    if numbered:
        return max(numbered, key=lambda t: t[0])[1]
    return ordered[0]


def branch_exists(repo: Path, branch: str) -> bool:
    """True if a *local* branch named *branch* exists (an unborn HEAD does not count)."""
    return run_git(
        repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False
    ).returncode == 0


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
    if branch_exists(repo, branch):
        print(f"Checking out existing branch '{branch}' …")
        run_git(repo, "checkout", branch)
        return

    # Create and switch.  Base the new branch on the *latest release* branch
    # (not on whatever HEAD happens to be, which may be an old release).
    if has_commits(repo):
        base = latest_release_branch(repo)
        if base:
            print(f"Creating and checking out new branch '{branch}' from latest release '{base}' …")
            run_git(repo, "checkout", "-b", branch, base)
        else:
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
        f"expected.  Please point --add at the correct directory (or unpack "
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

    # Remove bytecode caches already present in the repo (tracked or not) so
    # they show up as deletions and are never committed again.
    for pc in list(repo.rglob("__pycache__")) + list(repo.rglob("*.py[co]")):
        if ".git" in pc.relative_to(repo).parts:
            continue
        if pc.is_dir():
            shutil.rmtree(pc, ignore_errors=True)
        elif pc.exists():
            pc.unlink()

    # Prefer rsync for efficient sync + delete
    if shutil.which("rsync"):
        cmd = [
            "rsync",
            "-a",
            "--delete",
            "--exclude",
            ".git",
            # never copy bytecode caches (tracked ones are removed below)
            "--exclude",
            "__pycache__/",
            "--exclude",
            "*.pyc",
            "--exclude",
            "*.pyo",
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
            dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES]
            for f in files:
                if f.endswith(IGNORED_FILE_SUFFIXES):
                    continue
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
        # (bytecode caches are never in the file list, so they are removed above)
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
        if (status.strip() == "??" or status.startswith("A") or status.startswith("?")) \
                and is_ignored_path(path):
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
    max_tokens: int = 4096,
    allow_empty: bool = False,
) -> str:
    """Call OpenAI-compatible /v1/chat/completions.

    ``allow_empty`` accepts a null/empty completion (used by the availability
    check, where reasoning models may spend a small token budget on thinking).

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
        "max_tokens": max_tokens,
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
            if allow_empty:
                return ""
            raise KeyError("content is null")
        return content.strip()
    except (KeyError, IndexError, TypeError):
        _die(
            f"Error: unexpected LLM response format "
            f"(model '{model}' may be unavailable or returned an empty completion).\n"
            f"  Response: {data!r}"
        )


def check_model(base_url: str, model: str, api_key: str | None = None) -> None:
    """Verify that *model* answers, by sending a tiny "hi" test prompt.

    Called before anything in the repository is touched, so an unreachable
    server or an unavailable model aborts the run cleanly (call_llm exits
    with a descriptive error).
    """
    print(f"Checking model '{model}' with a test prompt …")
    reply = call_llm(
        base_url,
        model,
        "You are a helpful assistant.",
        "hi",
        api_key,
        max_tokens=32,
        allow_empty=True,
    )
    shown = " ".join(reply.split())[:60]
    print(f"  model OK" + (f" (reply: {shown!r})" if shown else " (empty reply)"))


class HunkError(RuntimeError):
    """A hunk could not be applied to the file taken from the repository."""


_HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _split_lines(data: bytes) -> list[bytes]:
    """Split on b'\\n' only (like git does), keeping the line endings."""
    return re.findall(rb"[^\n]*\n|[^\n]+", data)


def _parse_unified_hunks(diff: bytes) -> list[DiffHunk]:
    """Parse the unified diff of ONE file into per-hunk objects.

    Hunk extents are taken from the ``@@ -a,b +c,d @@`` counts, so lines in a
    hunk body that merely look like headers cannot confuse the parser.
    """
    lines = _split_lines(diff)
    hunks: list[DiffHunk] = []
    i, n = 0, len(lines)
    while i < n:
        m = _HUNK_RE.match(lines[i])
        if not m:
            i += 1
            continue
        old_start = int(m.group(1))
        old_count = int(m.group(2)) if m.group(2) is not None else 1
        new_count = int(m.group(4)) if m.group(4) is not None else 1
        header = lines[i].rstrip(b"\n").decode("utf-8", "replace")
        body = [lines[i]]
        old_l: list[bytes] = []
        new_l: list[bytes] = []
        change = 0
        old_seen = new_seen = 0
        prev = b""
        i += 1
        while i < n:
            ln = lines[i]
            tag = ln[:1]
            if tag == b"\\":  # "\ No newline at end of file" → previous line has none
                if prev == b" ":
                    old_l[-1] = old_l[-1].rstrip(b"\n")
                    new_l[-1] = new_l[-1].rstrip(b"\n")
                elif prev == b"-":
                    old_l[-1] = old_l[-1].rstrip(b"\n")
                elif prev == b"+":
                    new_l[-1] = new_l[-1].rstrip(b"\n")
                body.append(ln)
                i += 1
                continue
            if old_seen >= old_count and new_seen >= new_count:
                break
            if tag == b" ":
                old_l.append(ln[1:]); new_l.append(ln[1:])
                old_seen += 1; new_seen += 1
            elif tag == b"-":
                old_l.append(ln[1:]); old_seen += 1; change += 1
            elif tag == b"+":
                new_l.append(ln[1:]); new_seen += 1; change += 1
            else:
                break
            prev = tag
            body.append(ln)
            i += 1
        hunks.append(
            DiffHunk(
                index=len(hunks),
                header=header,
                body=b"".join(body).decode("utf-8", "replace"),
                change_lines=change,
                old_start=old_start,
                old_count=old_count,
                old_lines=old_l,
                new_lines=new_l,
            )
        )
    return hunks


def _git_bytes(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True)


def read_head_file(repo: Path, path: str) -> bytes:
    """Return *path* as stored in HEAD (b'' if there is no HEAD or no such file)."""
    if not has_commits(repo):
        return b""
    r = _git_bytes(repo, "show", f"HEAD:{path}")
    return r.stdout if r.returncode == 0 else b""


def _get_file_diff(repo: Path, path: str) -> bytes:
    """Return the unified diff of *path* against HEAD (or the whole file if new).

    Untracked files are not shown by ``git diff HEAD -- path``, so we detect
    that case and synthesise a "new file" diff from the working-tree content.
    """
    full = repo / path

    if has_commits(repo):
        in_head = run_git(repo, "ls-tree", "HEAD", "--", path, check=False).stdout.strip()
        if in_head:
            r = _git_bytes(repo, "diff", "--no-color", "--no-ext-diff", "-U3", "HEAD", "--", path)
            return r.stdout or b""
        # File is not in HEAD → treat as new (fall through to synthesise)

    if not full.is_file():
        return b""
    try:
        lines = _split_lines(full.read_bytes())
    except OSError:
        return b""
    if not lines:
        return b""
    out = [
        f"diff --git a/{path} b/{path}\n".encode(),
        b"new file mode 100644\n",
        b"--- /dev/null\n",
        f"+++ b/{path}\n".encode(),
        f"@@ -0,0 +1,{len(lines)} @@\n".encode(),
    ]
    for ln in lines:
        if ln.endswith(b"\n"):
            out.append(b"+" + ln)
        else:
            out.append(b"+" + ln + b"\n\\ No newline at end of file\n")
    return b"".join(out)


def apply_hunk(current: bytes, hunk: DiffHunk, applied: list[DiffHunk]) -> bytes:
    """Apply ONE hunk to *current* (the file as it is now in the repository).

    *applied* are the hunks of the same diff that are already part of
    *current*; those located above this hunk shifted its line numbers.  The
    lines the hunk expects are verified before anything is changed.
    """
    lines = _split_lines(current)
    start = hunk.pos + sum(
        len(a.new_lines) - len(a.old_lines) for a in applied if a.pos < hunk.pos
    )
    end = start + len(hunk.old_lines)
    if start < 0 or lines[start:end] != hunk.old_lines:
        raise HunkError(
            f"hunk {hunk.index} ({hunk.header}) does not match the file at line {start + 1}"
        )
    return b"".join(lines[:start] + hunk.new_lines + lines[end:])


def verify_hunks(repo: Path, path: str, hunks: list[DiffHunk]) -> bool:
    """Dry run: do all hunks together turn the HEAD version into the working file?"""
    try:
        cur = read_head_file(repo, path)
        done: list[DiffHunk] = []
        for h in hunks:
            cur = apply_hunk(cur, h, done)
            done.append(h)
        return cur == (repo / path).read_bytes()
    except (HunkError, OSError):
        return False


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

    # ---- 2) Large files: the model only PLANS the split, the program does it ----
    # The model returns the order of the hunks and one commit message per hunk.
    # apply_commits() then takes the file from the repository, applies exactly
    # one hunk, commits, and repeats until every hunk is done.
    for c in large:
        print(f"  Large file {c.path} ({c.total_lines} lines) – asking model to plan the hunk split …")
        hunks = _parse_unified_hunks(_get_file_diff(repo, c.path))
        if len(hunks) < 2 or not verify_hunks(repo, c.path, hunks):
            if len(hunks) >= 2:
                print(f"  warning: hunks of {c.path} do not reproduce the file – committing it as a whole")
            plans.append(CommitPlan(message=f"update {c.path}", files=[c.path]))
            continue

        hunk_summaries = []
        for h in hunks:
            body_lines = h.body.rstrip("\n").splitlines()
            preview_txt = "\n".join(body_lines[:40])
            if len(body_lines) > 40:
                preview_txt += "\n…"
            hunk_summaries.append(f"HUNK {h.index}  ({h.change_lines} changed lines)\n{preview_txt}")
        hunk_block = "\n\n".join(hunk_summaries)

        system = (
            "You are an expert software engineer. A single large file has many changes, "
            "shown as numbered diff hunks. A program (not you) will split the file: it takes "
            "the file from the repository, applies exactly ONE hunk, commits it, and repeats. "
            "You only write the plan: the order in which the hunks are applied and a commit "
            "message for each hunk.\n"
            "Rules:\n"
            "- Every hunk index from 0 to N-1 must appear exactly once.\n"
            "- Order the hunks so that each commit makes sense on its own and earlier commits "
            "do not depend on later ones; hunks of the same logical change should be adjacent.\n"
            "- Hunks may be applied in any order; the program handles shifted line numbers.\n"
            "- Commit messages: imperative mood, concise subject (<=72 chars), optional body; "
            f"the whole message must be concise but between {MIN_MESSAGE_WORDS} and {MAX_MESSAGE_WORDS} words long.\n"
            f"{archive_note}"
            "- Output ONLY valid JSON: a list of objects, one per hunk, with keys:\n"
            '  "hunk" (integer hunk index), "message" (string).\n'
            "- Do not invent hunk indices."
        )
        user = (
            f"File: {c.path}  (status={c.status}, +{c.insertions}/-{c.deletions})"
            + (f" from archive '{archive_name}'" if archive_name else "")
            + f"\nTotal hunks: {len(hunks)}\n\n"
            f"{hunk_block}\n\n"
            "Produce the plan: one commit per hunk, in the order they should be applied."
        )
        try:
            data = _extract_json_array(call_llm(base_url, model, system, user, api_key))
        except RuntimeError as exc:
            print(f"  warning: unusable plan for {c.path} ({exc}); using hunk order with generic messages")
            data = []

        assigned: set[int] = set()
        file_plans: list[CommitPlan] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            msg = str(item.get("message", "")).strip()
            raw_idx = item.get("hunk", item.get("hunks"))
            idxs = raw_idx if isinstance(raw_idx, list) else [raw_idx]
            if not msg:
                continue
            clean_idx: list[int] = []
            for x in idxs:
                try:
                    xi = int(x)
                except (TypeError, ValueError):
                    continue
                if 0 <= xi < len(hunks) and xi not in assigned:
                    clean_idx.append(xi)
                    assigned.add(xi)
            if clean_idx:  # normally exactly one hunk per commit
                file_plans.append(
                    CommitPlan(message=msg, files=[c.path], hunks={c.path: clean_idx})
                )

        # hunks the model forgot: one commit each, in file order, after the planned ones
        for hi in sorted(set(range(len(hunks))) - assigned):
            file_plans.append(
                CommitPlan(
                    message=f"update {c.path}: apply hunk {hi + 1} of {len(hunks)} ({hunks[hi].header})",
                    files=[c.path],
                    hunks={c.path: [hi]},
                )
            )
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


def _snapshot_worktree(repo: Path, paths: list[str]) -> dict[str, tuple[bytes, float] | None]:
    """Remember the synced (final) content and mtime of *paths*; None = absent."""
    snap: dict[str, tuple[bytes, float] | None] = {}
    for p in paths:
        full = repo / p
        if full.is_file():
            snap[p] = (full.read_bytes(), full.stat().st_mtime)
        else:
            snap[p] = None
    return snap


def _write_file(repo: Path, path: str, data: bytes, mtime: float | None) -> None:
    full = repo / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(data)
    if mtime is not None:
        os.utime(full, (mtime, mtime))


def _rollback(
    repo: Path,
    start_head: str | None,
    snapshot: dict[str, tuple[bytes, float] | None],
) -> None:
    """Undo every commit made by this run and restore the synced working tree.

    HEAD goes back to where it was before the first commit; the index is reset
    and each snapshotted file gets its final (synced) content back, so the
    repository is in exactly the state it had before apply_commits() started.
    """
    if start_head:
        run_git(repo, "reset", "--mixed", "-q", start_head, check=False)
    else:  # repository had no commit before this run
        run_git(repo, "update-ref", "-d", "HEAD", check=False)
        run_git(repo, "read-tree", "--empty", check=False)
    for path, st in snapshot.items():
        full = repo / path
        if st is None:
            if full.is_file():
                full.unlink()
        else:
            _write_file(repo, path, st[0], st[1])


def apply_commits(
    repo: Path,
    plans: list[CommitPlan],
    author_name: str,
    author_email: str,
) -> None:
    """Create the planned commits; on any failure undo all of them again.

    Hunk plans are executed by the program, one hunk at a time:
      1. take the file as it is in the repository (HEAD),
      2. apply ONE hunk to it,
      3. stage and commit it,
      4. repeat until all hunks of the file are committed.
    If any commit fails, every commit created by this run is reverted
    (HEAD is reset to its starting point, the synced files are restored) and
    the program exits with an error.
    """
    base_env = os.environ.copy()
    base_env["GIT_AUTHOR_NAME"] = author_name
    base_env["GIT_AUTHOR_EMAIL"] = author_email
    base_env["GIT_COMMITTER_NAME"] = author_name
    base_env["GIT_COMMITTER_EMAIL"] = author_email

    start_head = (
        run_git(repo, "rev-parse", "HEAD").stdout.strip() if has_commits(repo) else None
    )
    all_paths = sorted({p for pl in plans for p in list(pl.files) + list(pl.hunks)})
    snapshot = _snapshot_worktree(repo, all_paths)

    # Per hunk-file state: the hunks of the diff HEAD→worktree and which ones are committed.
    hunk_state: dict[str, dict] = {}
    for pl in plans:
        for path in pl.hunks:
            if path not in hunk_state:
                hunk_state[path] = {
                    "hunks": _parse_unified_hunks(_get_file_diff(repo, path)),
                    "applied": [],
                }

    made = 0
    try:
        for i, plan in enumerate(
            tqdm(plans, desc="Creating commits", unit="commit", leave=True), start=1
        ):
            tqdm.write(f"\n=== Commit {i}/{len(plans)} ===")
            tqdm.write(f"Message: {plan.message}")
            tqdm.write(f"Files: {', '.join(plan.files)}")

            if plan.hunks:
                for path, idxs in plan.hunks.items():
                    st = hunk_state[path]
                    final = snapshot[path][0] if snapshot[path] else b""
                    mtime = snapshot[path][1] if snapshot[path] else None
                    # 1) the file as it is in the repository right now
                    current = read_head_file(repo, path)
                    for idx in idxs:
                        h = st["hunks"][idx]
                        # 2) change exactly one hunk
                        current = apply_hunk(current, h, st["applied"])
                        st["applied"].append(h)
                        tqdm.write(f"  hunk {idx} of {path}: {h.header} (+/-{h.change_lines})")
                    if len(st["applied"]) == len(st["hunks"]) and current != final:
                        raise HunkError(
                            f"{path}: after the last hunk the file differs from the synced source"
                        )
                    _write_file(repo, path, current, mtime)
                    run_git(repo, "add", "--", path)
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

            staged = run_git(repo, "diff", "--cached", "--name-only")
            if not staged.stdout.strip():
                tqdm.write("  (nothing staged – skipping)")
                continue

            # Commit date = mtime of the source files (kept from before any rewrite)
            env = base_env.copy()
            times = [snapshot[p][1] for p in plan.files if snapshot.get(p)]
            mtime_c = max(times) if times else _max_mtime_for_paths(repo, plan.files)
            if mtime_c is not None:
                date_str = _format_git_date(mtime_c)
                env["GIT_AUTHOR_DATE"] = date_str
                env["GIT_COMMITTER_DATE"] = date_str
                tqdm.write(f"  date: {date_str} (from source file mtime)")
            else:
                tqdm.write("  date: (current time – no surviving files for mtime)")

            # 3) commit
            r = subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", plan.message],
                env=env, capture_output=True, text=True,
            )
            if r.returncode != 0:
                raise RuntimeError(
                    f"git commit failed (exit {r.returncode}): "
                    f"{(r.stderr or r.stdout).strip()}"
                )
            made += 1
            tqdm.write("  committed.")
        # 4) the loop repeats until every planned hunk is committed
    except (Exception, KeyboardInterrupt) as exc:
        tqdm.write(f"\nERROR in commit {made + 1}/{len(plans)}: {exc}")
        tqdm.write(f"Reverting the {made} commit(s) created by this run …")
        _rollback(repo, start_head, snapshot)
        where = f"HEAD is back at {start_head[:12]}" if start_head else "the repository is empty again"
        raise SystemExit(
            f"Aborted: {exc}\nAll {made} commit(s) of this run were reverted; {where}, "
            "the synced files are in the working tree (uncommitted)."
        )


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
    strip: bool = True,
) -> Path:
    """Recreate an archive from the tree at *branch* using ``git archive``.

    By default (``strip=True``) the files are stored directly at the root of
    the archive.  With ``strip=False`` they are placed below a single top-level
    directory named after the branch.

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

    # Always resolve against the *current working directory*.  A relative
    # path would otherwise be interpreted by ``git -C <repo> archive`` relative
    # to the repository, i.e. the archive would end up inside the repo.
    if output is None:
        output = Path.cwd() / f"{branch}{ext}"
    else:
        output = output.expanduser().resolve()
        if output.is_dir():
            output = output / f"{branch}{ext}"

    # Prefix so unpacking creates a single top-level directory (matches import
    # strip).  strip (the default) drops it: files are stored at the archive root.
    if strip:
        prefix = ""
    elif prefix is None:
        prefix = f"{branch}/"

    output.parent.mkdir(parents=True, exist_ok=True)
    print(f"Exporting branch '{branch}' → {output} ({git_format}) …")
    cmd = [
        "git",
        "-C",
        str(repo),
        "archive",
        f"--format={git_format}",
        *([f"--prefix={prefix}"] if prefix else []),
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
            "Four modes (only --git is always required):\n"
            "\n"
            "  LIST    – print all branches like 'git branch --all' (the default\n"
            "            when neither --add nor --export is given)\n"
            "            requires: --git\n"
            "\n"
            "  REVERT  – remove the last branch from the repository\n"
            "            requires: --git --revert (not with --add / --export / --list)\n"
            "\n"
            "  IMPORT  – sync --add into --git and create commits (needs LLM)\n"
            "            requires: --git --url --add --name\n"
            "            optional: --model (default: first/active model from GET /v1/models)\n"
            "\n"
            "  EXPORT  – recreate a zip/tar from a branch\n"
            "            requires: --git --export [NAME]\n"
            "            does NOT need --url, --model, --add, or --name"
        ),
        epilog=(
            "examples:\n"
            "  # Import an archive (creates branch from archive name)\n"
            "  %(prog)s --git ./repo --url http://localhost:8080/v1 \\\n"
            "      --add snapshot-2020.zip --name 'Botter <ai@localhost>'\n"
            "\n"
            "  # Import with an explicit model\n"
            "  %(prog)s --git ./repo --url http://localhost:8080/v1 \\\n"
            "      --model qwen2.5-coder --add snapshot-2020.zip \\\n"
            "      --name 'Botter <ai@localhost>'\n"
            "\n"
            "  # Export the last imported archive (newest branch tip)\n"
            "  %(prog)s --git ./repo --export\n"
            "\n"
            "  # Export a specific branch / archive name\n"
            "  %(prog)s --git ./repo --export 013-llm-errors\n"
            "  %(prog)s --git ./repo --export 013-llm-errors.zip \\\n"
            "      --export-format tar.gz --export-output /tmp/out.tar.gz\n"
            "\n"
            "  # List all branches (like 'git branch --all'); needs only --git\n"
            "  %(prog)s --git ./repo --list\n"
            "  %(prog)s --git ./repo          # same: --list is the default\n"
            "                                 # when neither --add nor --export is given\n"
            "\n"
            "  # Remove the last branch again (e.g. after a bad import)\n"
            "  %(prog)s --git ./repo --revert\n"
            "\n"
            "  # Export keeping the top-level <branch>/ directory\n"
            "  %(prog)s --git ./repo --export 013-llm-errors --no-strip\n"
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
        "--add",
        default=None,
        type=Path,
        metavar="PATH",
        help="Source directory or archive (.zip / .tar / .tar.gz / …)",
    )
    import_opts.add_argument(
        "--name",
        default=None,
        help='Committer identity, e.g. "Botter <ai@localhost>". Also accepted: '
        '"ai@localhost" (name = part before the @), "Botter ai@localhost" and '
        'a bare "Botter" (email becomes botter@localhost)',
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
        help="Branch to commit on (default: archive basename if --add is an archive)",
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
    list_opts = parser.add_argument_group(
        "list mode (needs only --git; the default when neither --add nor --export is given)"
    )
    list_opts.add_argument(
        "--list",
        action="store_true",
        help="Print all branches of the repository like 'git branch --all' "
        "and exit.  This is the default when neither --add nor --export is "
        "given.  (No branch listing is shown after an import or export.)",
    )
    revert_opts = parser.add_argument_group(
        "revert mode (needs only --git; cannot be combined with --add / --export)"
    )
    revert_opts.add_argument(
        "--revert",
        action="store_true",
        help="Remove the last branch from the repository (the one holding the "
        "most recent import: highest numeric prefix, else newest tip).  If it "
        "is checked out, the previous branch is checked out first.  Refuses "
        "to remove the only branch or to run with uncommitted changes.",
    )
    export_opts = parser.add_argument_group(
        "export mode (does not need --url / --model / --add / --name)"
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
    export_opts.add_argument(
        "--strip",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="With --export: strip all directories from the archive, i.e. do "
        "not wrap the files in a top-level <branch>/ directory (default: on). "
        "Use --no-strip to keep the <branch>/ directory",
    )
    args = parser.parse_args()

    if args.revert:
        if args.export is not None or args.add is not None or args.list:
            parser.error("--revert cannot be combined with --add, --export or --list")
    elif args.export is None and args.add is None:
        # Neither --export nor --add given → list mode is the default
        args.list = True

    def note_ignored(action: str) -> None:
        ignored = [
            flag
            for flag, dest in (
                ("--url", "url"), ("--model", "model"), ("--name", "name"),
                ("--branch", "branch"), ("--mention-archive", "mention_archive"),
                ("--dry-run", "dry_run"),
            )
            if getattr(args, dest) != parser.get_default(dest)
        ]
        if ignored:
            print(
                f"Note: {', '.join(ignored)} ignored – {action} "
                "(use --add ARCHIVE_OR_DIR to import).",
                file=sys.stderr,
            )

    if args.strip is not None and args.export is None:
        parser.error("--strip / --no-strip can only be used together with --export")
    if args.strip is None:
        args.strip = True  # default: files at the archive root

    # ----- revert mode -----
    if args.revert:
        note_ignored("reverting the last branch")
        revert_last_branch(args.git)
        return

    # ----- list mode -----
    if args.list:
        if args.export is not None or args.add is not None:
            parser.error("--list cannot be combined with --export or --add")
        note_ignored("listing branches")
        require_git_repo(args.git)
        list_branches(args.git)
        return

    # ----- export mode -----
    if args.export is not None:
        require_git_repo(args.git)  # validate without auto-creating
        branch = resolve_export_branch(
            args.git, None if args.export == "__LAST__" else args.export
        )
        export_archive(
            args.git,
            branch,
            output=args.export_output,
            fmt=args.export_format,
            strip=args.strip,
        )
        return

    ensure_git_repo(args.git)

    # ----- import mode -----
    missing = [
        flag
        for flag, val in (
            ("--url", args.url),
            ("--add", args.add),
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

    # An archive whose name (without extension) matches an existing branch has
    # already been imported (or clashes with another release): refuse before
    # doing anything else.  An explicit --branch still lets you add to a branch.
    _src_path = args.add.expanduser().resolve()
    if args.branch is None and is_archive(_src_path):
        _derived = branch_name_from_archive(_src_path)
        if branch_exists(args.git, _derived):
            raise SystemExit(
                f"Error: branch '{_derived}' already exists in {args.git} – "
                f"refusing to import archive '{_src_path.name}' again.\n"
                "Rename the archive, or pass --branch NAME to add it to an "
                "existing branch on purpose.\n"
                f"(If an earlier import of this archive failed before anything "
                f"was committed, remove the leftover branch with "
                f"'git branch -D {_derived}' and try again.)"
            )

    args.model = resolve_model(args.url, args.model, args.api_key)
    author_name, author_email = parse_author(args.name)

    # Optional: refuse if there are already staged changes
    staged = run_git(args.git, "diff", "--cached", "--name-only")
    if staged.stdout.strip():
        raise SystemExit(
            "Repository has staged changes. Please commit or unstage them first."
        )

    # Make sure the model really answers before the repository is touched
    # (branch creation, sync and commits all come after this point).
    check_model(args.url, args.model, args.api_key)

    # Branch selection: explicit --branch wins; otherwise derive from archive name
    branch = args.branch
    if branch is None and is_archive(args.add.expanduser().resolve()):
        branch = branch_name_from_archive(args.add)
        print(f"Archive detected – using branch name '{branch}' (override with --branch)")
    if branch:
        ensure_branch(args.git, branch)

    # Safety: the sync step runs "rsync --delete" into the repo, so a source
    # (archive or directory) located inside the repo would be deleted by it.
    _src_abs = args.add.expanduser().resolve()
    _repo_abs = args.git.expanduser().resolve()
    if _src_abs == _repo_abs or _repo_abs in _src_abs.parents:
        raise SystemExit(
            f"Source {_src_abs} is inside the target repository {_repo_abs}; "
            "syncing would delete it. Move the source outside the repository."
        )

    committed = False
    with resolve_source(args.add) as effective_src:
        if not effective_src.is_dir():
            raise SystemExit(
                f"Internal error: resolved source is not a directory: {effective_src} "
                f"(from --add {args.add}). Is this the current ai_git_commit.py?"
            )
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
        src_path = args.add.expanduser().resolve()
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
        committed = True

    # Only the name of the branch the commits went to; printed after the
    # temporary-directory cleanup so it really is the last line of output.
    if committed:
        last = current_branch(args.git)
        if last:
            print(last)


if __name__ == "__main__":
    main()

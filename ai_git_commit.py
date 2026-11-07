#!/usr/bin/env python3
"""
AI-assisted Git committer: sync a source directory into a local Git repo and
create one or more commits with LLM-generated messages.

All commits go on ONE branch named "main" (GitHub compatible).  Each import is
marked with a release TAG (the archive name); the tag message holds the
description of that version.  Repositories of the old format (one branch per
release, ``desc/<name>`` tags) are converted automatically.

Changes are grouped into functionally coherent commits (the model plans; large files are split hunk by hunk by the program). Each commit's total
changed lines (insertions + deletions) is kept at or below 200 when possible.
Single files larger than 200 lines are committed alone.

© Dr. Nikolaus Klepp - office@klepp.biz - https://klepp.biz

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

MAX_LINES_PER_COMMIT = 200
# Never sync or commit Python bytecode caches.
IGNORED_DIR_NAMES = ("__pycache__",)
# Bytecode caches and common editor/backup files – never synced or committed.
IGNORED_FILE_SUFFIXES = (
    ".pyc", ".pyo",
    ".bak", ".BAK", ".orig", ".swp", ".swo", ".tmp",
)


def is_ignored_path(path: str) -> bool:
    """True for paths that must never be added to the repo (bytecode, backups)."""
    p = Path(path)
    parts = p.parts
    name = p.name
    if any(d in parts for d in IGNORED_DIR_NAMES):
        return True
    if name.endswith(IGNORED_FILE_SUFFIXES) or name.endswith("~"):
        return True
    # Emacs-style auto-save / lock files: #file#
    if len(name) >= 2 and name.startswith("#") and name.endswith("#"):
        return True
    return False


MAX_MESSAGE_WORDS = 40

# Prompt rules shared by every prompt that makes the model write commit text.
NO_FILLER_RULE = (
    "- State clearly and concisely WHAT changed, naming the concrete change (what was "
    "added, removed, fixed or renamed, and where). Never describe a purpose, benefit or "
    "justification: no clauses like \"to keep the codebase current and consistent\", "
    "\"to improve maintainability\", \"for better readability\" or \"to ensure everything "
    "works as expected\". Never write a message that only says \"update <file>\".\n"
)
MESSAGE_RULE = (
    "- Commit messages: imperative mood, ONE short subject line (<=72 chars, normally "
    f"under 15 words); add a body only when needed, at most {MAX_MESSAGE_WORDS} words in total.\n"
)
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


MAIN_BRANCH = "main"   # the only branch; GitHub compatible


def tag_descriptions(repo: Path) -> dict[str, str]:
    """Map release tag name → short description (first paragraph, as one line)
    of its annotated tag.  Lightweight tags have no description."""
    r = run_git(
        repo, "for-each-ref", "refs/tags/",
        "--format=%(refname)%09%(objecttype)%09%(contents:subject)", check=False,
    )
    out: dict[str, str] = {}
    prefix = "refs/tags/"
    for line in r.stdout.splitlines():
        ref, _, rest = line.partition("\t")
        kind, _, subject = rest.partition("\t")
        if kind != "tag" or not ref.startswith(prefix):
            continue
        subject = clean_subject(strip_markup(subject))
        if subject:
            out[ref[len(prefix):]] = subject
    return out


ANSI_RESET = "\033[0m"
ANSI_GRAY = "\033[90m"          # bright black = gray
ANSI_LIGHT_GREEN = "\033[92m"   # bright green
ANSI_BRIGHT_RED = "\033[91m"    # bright red (error messages)


def use_color(stream: Any = None) -> bool:
    """Colorize only when *stream* (default: stdout) is a terminal and
    NO_COLOR is not set."""
    stream = sys.stdout if stream is None else stream
    return stream.isatty() and "NO_COLOR" not in os.environ


def red(text: str, stream: Any = None) -> str:
    """Wrap *text* in bright red when *stream* (default: stderr) is a terminal."""
    stream = sys.stderr if stream is None else stream
    if not use_color(stream):
        return text
    return f"{ANSI_BRIGHT_RED}{text}{ANSI_RESET}"


def print_error(msg: str) -> None:
    """Print an error message to stderr in bright red."""
    print(red(str(msg)), file=sys.stderr)


def _release_sort_key(name: str) -> tuple[int, int, str]:
    """Sort key: tags without a number first, then by release number (oldest
    first, newest last).  An old ``desc/013-x`` tag counts as number 13."""
    m = re.match(r"^(?:desc/)?(\d+)", name)
    return (1, int(m.group(1)), name) if m else (0, 0, name)


def list_release_tags(repo: Path) -> list[str]:
    """Return all tag names that can be releases, oldest release first.

    A tag named ``main`` is never a release (it would clash with the branch).
    """
    r = run_git(repo, "for-each-ref", "--format=%(refname)", "refs/tags/", check=False)
    prefix = "refs/tags/"
    names = [ln.strip()[len(prefix):] for ln in r.stdout.splitlines()
             if ln.strip().startswith(prefix)]
    return sorted((n for n in names if n != MAIN_BRANCH), key=_release_sort_key)


def tag_commits(repo: Path) -> dict[str, str]:
    """Map tag name → sha of the commit the tag points to."""
    r = run_git(
        repo, "for-each-ref", "refs/tags/",
        "--format=%(refname)%09%(objectname)%09%(*objectname)", check=False,
    )
    out: dict[str, str] = {}
    prefix = "refs/tags/"
    for line in r.stdout.splitlines():
        ref, _, rest = line.partition("\t")
        obj, _, peeled = rest.partition("\t")
        if ref.startswith(prefix):
            out[ref[len(prefix):]] = peeled or obj
    return out


def list_tags(repo: Path) -> None:
    """Print the release tags, plus short descriptions.

    The tags that point at the checked-out commit are marked with ``*``.
    """
    tags = list_release_tags(repo)
    if not tags:
        print("No release tags found.")
        return
    desc = tag_descriptions(repo)
    head = run_git(repo, "rev-parse", "-q", "--verify", "HEAD^{commit}",
                   check=False).stdout.strip()
    commits = tag_commits(repo)
    lines = [("* " if head and commits.get(t) == head else "  ") + t for t in tags]
    width = max((len(ln) for ln in lines if ln[2:] in desc), default=0)
    color = use_color()
    for ln in lines:
        text = desc.get(ln[2:])
        name = ln.ljust(width) if text else ln  # pad BEFORE adding escape codes
        if color and ln.startswith("* "):  # the checked-out release
            name = f"{ANSI_LIGHT_GREEN}{name}{ANSI_RESET}"
        if text:
            text = f"{ANSI_GRAY}{text}{ANSI_RESET}" if color else text
            print(f"{name}  {text}")
        else:
            print(name)


def current_branch(repo: Path) -> str:
    """Name of the checked-out branch; ``HEAD`` when detached.

    Uses the full ref name, because ``rev-parse --abbrev-ref`` returns
    ``heads/NAME`` when a tag has the same name as the branch.
    """
    r = run_git(repo, "symbolic-ref", "-q", "HEAD", check=False)
    if r.returncode != 0:
        return "HEAD" if has_commits(repo) else ""
    ref = r.stdout.strip()
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref


# Documents that describe a release, in priority order (file stem, any extension).
# README first so release descriptions summarize product changes; conversation is fallback.
DESCRIPTION_DOC_STEMS = ("readme", "conversation", "changelog", "changes", "history", "notes")
SHORT_DESCRIPTION_WORDS = 50   # target length of the first line shown by --list
MAX_SHORT_DESCRIPTION_WORDS = 70
MAX_DESCRIPTION_WORDS = 170
MAX_PROMPT_CHARS = 12000


def find_description_doc(repo: Path, ref: str = "HEAD") -> str | None:
    """Return the best description document in the root of *ref*, or None.

    Priority: README* first, then conversation.*, CHANGELOG*, CHANGES*,
    HISTORY*, NOTES* (case-insensitive, any extension).
    """
    r = run_git(repo, "ls-tree", "--name-only", ref, check=False)
    if r.returncode != 0:
        return None
    names = sorted(n for n in r.stdout.splitlines() if "/" not in n)
    for stem in DESCRIPTION_DOC_STEMS:
        for n in names:
            if Path(n).stem.lower() == stem and (repo / n).is_file():
                return n
    return None


def _clip(text: str, limit: int = MAX_PROMPT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[… truncated …]"


def summarize_progress(
    repo: Path, doc: str, start_head: str | None,
    base_url: str, model: str, api_key: str | None,
) -> str:
    """Ask the model for a short description of what changed since *start_head*.

    The ONLY source is the diff of the description document *doc* between the
    previous version (*start_head*; the empty tree for a first import) and the
    current tip – no other files, commit messages or document text.  Returns
    "" when the document did not change.
    """
    base = start_head or run_git(repo, "hash-object", "-t", "tree", "/dev/null").stdout.strip()
    d = _git_bytes(repo, "diff", "--no-color", "-U0", base, "HEAD", "--", doc)
    diff = d.stdout.decode("utf-8", "replace").strip()
    if not diff:
        return ""
    parts = [f"CHANGES OF {doc} SINCE THE PREVIOUS VERSION (unified diff; '+' = added, '-' = removed):\n"
             + _clip(diff)]
    system = (
        "You write the description of a new version of a software project, to be "
        "stored as the message of an annotated Git tag. Summarize the differences / "
        "progression from the previous version to the current version, based ONLY "
        "on the document changes provided (use nothing else). The document is only your "
        "source of information about WHAT CHANGED IN THE PROJECT: describe those project "
        "changes themselves and NEVER mention any document name (README, conversation, "
        "changelog, …), a conversation, requests, users, assistants, or the fact that a "
        "document or log was updated.\n"
        "Output PLAIN TEXT only, in this layout:\n"
        f"1. a SHORT DESCRIPTION: one single line of about {SHORT_DESCRIPTION_WORDS} words "
        "(between 40 and 60) naming the concrete changes and features of this version; "
        "no 'Subject:' or other label, no markdown;\n"
        "2. a blank line;\n"
        f"3. optionally a body with further details (at most {MAX_DESCRIPTION_WORDS - SHORT_DESCRIPTION_WORDS * 2} words).\n"
        "The whole answer must be PLAIN TEXT: no markup of any kind (no markdown, no "
        "asterisks, backticks, underscores for emphasis, headings, bullet points, numbered "
        "lists, links or code fences) – just sentences.\n"
        + NO_FILLER_RULE.rstrip("\n")
    )
    user = "\n\n".join(parts)
    text = normalize_description(call_llm(base_url, model, system, user, api_key, max_tokens=1024))
    if _mentions_doc(text, doc):  # one retry with a stricter reminder, then filter
        retry = call_llm(
            base_url, model, system,
            user + "\n\nREMINDER: do not mention README, conversation, any document name, "
                   "or that a document was updated – only the project changes themselves.",
            api_key, max_tokens=1024,
        )
        text = normalize_description(retry)
    return _drop_doc_mentions(text, doc)


def _doc_pattern(doc: str) -> re.Pattern:
    """Matches the source document name and common doc names (readme, conversation)."""
    alts = [
        re.escape(doc),
        r"\breadme(?:\.\w+)?\b",
        r"\bconversation(?:\.\w+)?\b",
        r"\bchangelog(?:\.\w+)?\b",
    ]
    return re.compile("|".join(alts), re.IGNORECASE)


def _mentions_doc(text: str, doc: str) -> bool:
    return bool(_doc_pattern(doc).search(text))


def _drop_doc_mentions(text: str, doc: str) -> str:
    """Remove sentences that still talk about the source document itself."""
    pat = _doc_pattern(doc)
    paras = []
    for para in text.split("\n\n"):
        sentences = re.split(r"(?<=[.!?])\s+", para)
        kept = [x for x in sentences if not pat.search(x)]
        if kept:
            paras.append(" ".join(kept) if len(para.splitlines()) <= 1 else "\n".join(
                ln for ln in para.splitlines() if not pat.search(ln)))
    return "\n\n".join(p for p in paras if p.strip())


def read_head_file_at(repo: Path, ref: str, path: str) -> bytes:
    r = _git_bytes(repo, "show", f"{ref}:{path}")
    return r.stdout if r.returncode == 0 else b""


def strip_markup(text: str) -> str:
    """Convert Markdown to plain text: no emphasis, code, headings, list
    markers, quotes, rules, links or images (their text is kept)."""
    out: list[str] = []
    for line in text.splitlines():
        if re.match(r"^\s*(```|~~~)", line) or re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line):
            continue  # code fences, horizontal rules
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)                 # headings
        line = re.sub(r"^\s*>+\s?", "", line)                         # quotes
        line = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", line)          # list markers
        line = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", line)        # links / images
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)                   # bold
        line = re.sub(r"(?<!\w)__(?!\s)(.+?)(?<!\s)__(?!\w)", r"\1", line)  # __bold__
        line = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", line)  # *italic*
        line = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"\1", line)           # _italic_
        line = re.sub(r"~~(.+?)~~", r"\1", line)                       # strikethrough
        line = re.sub(r"`+([^`]*)`+", r"\1", line)                     # inline code
        line = re.sub(r"^\s*(?:subject|title)\s*:\s*", "", line, flags=re.IGNORECASE)
        out.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def clean_subject(line: str) -> str:
    """Drop markdown emphasis and a leading 'Subject:' / 'Title:' label.

    ``**Subject:** Foo`` and ``**Subject: Foo**`` both become ``Foo``.
    """
    line = line.strip().strip("*`# ")
    line = re.sub(r"^(?:subject|title)\s*:\s*", "", line, flags=re.IGNORECASE)
    return line.strip("*`# ")


def normalize_description(text: str) -> str:
    """Strip code fences; return ``short description`` [+ blank line + body].

    The short description (shown by ``--list``) is the first paragraph joined
    into ONE line without label/markdown, cut to MAX_SHORT_DESCRIPTION_WORDS;
    the whole text is cut to MAX_DESCRIPTION_WORDS words.
    """
    text = strip_markup(text)
    head, sep, body = text.partition("\n\n")
    if not sep:  # no blank line: first line is the short description
        head, _, body = text.partition("\n")
    short = clean_subject(" ".join(head.split()))
    short = " ".join(short.split()[:MAX_SHORT_DESCRIPTION_WORDS])
    body = body.strip()
    if body:
        room = MAX_DESCRIPTION_WORDS - len(short.split())
        out: list[str] = []
        used = 0
        for line in body.splitlines():
            n = len(line.split())
            if used + n <= room:
                out.append(line.rstrip())
                used += n
            else:
                if room - used > 0 and line.strip():
                    out.append(" ".join(line.split()[: room - used]))
                break
        body = "\n".join(out).strip()
    return f"{short}\n\n{body}" if body else short


def tag_release(
    repo: Path, tag: str, description: str, author_name: str, author_email: str,
) -> None:
    """Create the release tag *tag* on the tip of ``main``.

    With a description the tag is an annotated tag (the description is its
    message); without a description it is a lightweight tag.
    """
    env = os.environ.copy()
    env.update(GIT_COMMITTER_NAME=author_name, GIT_COMMITTER_EMAIL=author_email,
               GIT_AUTHOR_NAME=author_name, GIT_AUTHOR_EMAIL=author_email)
    if description:
        cmd = ["git", "-C", str(repo), "tag", "-a", "-F", "-", tag, "HEAD"]
        stdin = description + "\n"
    else:
        cmd = ["git", "-C", str(repo), "tag", tag, "HEAD"]
        stdin = None
    r = subprocess.run(cmd, input=stdin, text=True, capture_output=True, env=env)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())
    first = description.splitlines()[0] if description else "(no description)"
    print(f"Created release tag '{tag}': {first}")


def _repo_size_kib(repo: Path) -> int:
    """Size of the object database (loose + packed) in KiB."""
    info = {}
    for line in run_git(repo, "count-objects", "-v").stdout.splitlines():
        k, _, v = line.partition(":")
        info[k.strip()] = v.strip()
    return sum(int(info.get(k, "0") or 0) for k in ("size", "size-pack", "size-garbage"))


def cleanup_repository(repo: Path) -> None:
    """Permanently delete everything no branch or tag can reach any more.

    Expires the whole reflog and prunes unreachable objects immediately
    (``git reflog expire --expire=now --all`` + ``git gc --prune=now``), so
    commits of reverted releases are gone for good.  No confirmation.
    """
    require_git_repo(repo)
    before = _repo_size_kib(repo)
    fsck = run_git(repo, "fsck", "--unreachable", "--no-reflogs", check=False)
    commits = sum(1 for ln in fsck.stdout.splitlines() if ln.startswith("unreachable commit"))
    objects = sum(1 for ln in fsck.stdout.splitlines() if ln.startswith("unreachable "))
    print(f"Cleaning up {repo} …")
    run_git(repo, "reflog", "expire", "--expire=now", "--all")
    run_git(repo, "gc", "--prune=now", "-q")
    after = _repo_size_kib(repo)
    print(f"Removed {commits} unreachable commit(s) ({objects} unreachable object(s) in total).")
    print(f"Repository size: {before} KiB → {after} KiB.")


def revert_last_release(repo: Path) -> None:
    """Remove the last release (see ``latest_release_tag``) from *repo*.

    * Refuses to remove the only remaining release tag.
    * Refuses when the working tree has uncommitted/untracked changes.
    * Refuses when ``main`` is not checked out or not at the tip of that tag.
    * Deletes the tag and moves ``main`` back to the previous release tag.
    * Prints the commands that restore the removed release.
    """
    require_git_repo(repo)
    tags = list_release_tags(repo)
    if not tags:
        raise SystemExit("No release tags found – nothing to revert.")
    if len(tags) == 1:
        raise SystemExit(
            f"Only one release tag ('{tags[0]}') exists – refusing to remove it."
        )
    last = latest_release_tag(repo)
    previous = latest_release_tag(repo, exclude=last)
    if not last or not previous:
        raise SystemExit("Could not determine the last release tag.")

    now_on = current_branch(repo)
    if now_on != MAIN_BRANCH:
        raise SystemExit(
            f"The checked-out branch is '{now_on}', not '{MAIN_BRANCH}'.\n"
            f"Check out '{MAIN_BRANCH}' first:  git -C {repo} checkout {MAIN_BRANCH}"
        )
    if run_git(repo, "status", "--porcelain", check=False).stdout.strip():
        raise SystemExit(
            f"Working tree of {repo} has uncommitted or untracked changes "
            f"(e.g. from a failed import) – refusing to move '{MAIN_BRANCH}'.\n"
            f"Discard them first if you are sure:\n"
            f"  git -C {repo} reset --hard && git -C {repo} clean -fd"
        )

    def rev(ref: str) -> str:
        return run_git(repo, "rev-parse", ref).stdout.strip()

    last_commit = rev(f"refs/tags/{last}^{{commit}}")
    prev_commit = rev(f"refs/tags/{previous}^{{commit}}")
    tag_object = rev(f"refs/tags/{last}")
    if rev("HEAD") != last_commit:
        raise SystemExit(
            f"'{MAIN_BRANCH}' is not at the tip of release '{last}' "
            f"({rev('HEAD')[:12]} instead of {last_commit[:12]}) – refusing to move it."
        )
    if run_git(repo, "merge-base", "--is-ancestor", prev_commit, last_commit,
               check=False).returncode != 0:
        raise SystemExit(
            f"Release '{previous}' is not an ancestor of '{last}' – refusing to move "
            f"'{MAIN_BRANCH}'."
        )

    r = run_git(repo, "reset", "--hard", "-q", prev_commit, check=False)
    if r.returncode != 0:
        raise SystemExit(f"Could not move '{MAIN_BRANCH}' back to '{previous}':\n{r.stderr.strip()}")
    r = run_git(repo, "tag", "-d", last, check=False)
    if r.returncode != 0:
        raise SystemExit(f"Could not remove release tag '{last}':\n{r.stderr.strip()}")

    print(f"Removed release tag '{last}' (was {last_commit[:12]}); "
          f"'{MAIN_BRANCH}' is back at '{previous}' ({prev_commit[:12]}).")
    print(f"Restore with: git -C {repo} reset --hard {last_commit} "
          f"&& git -C {repo} tag {last} {tag_object}")


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


def _tags_newest_first(repo: Path) -> list[str]:
    """Tag names ordered best-first.

    Release tags are named like ``017-export-model`` (derived from archive
    names).  Tags with a leading number come first (highest number first); the
    other tags follow, newest first.
    """
    r = run_git(
        repo, "for-each-ref", "--sort=-creatordate", "--format=%(refname)",
        "refs/tags/", check=False,
    )
    prefix = "refs/tags/"
    names = [ln.strip()[len(prefix):] for ln in r.stdout.splitlines()
             if ln.strip().startswith(prefix)
             and ln.strip()[len(prefix):] != MAIN_BRANCH
             and not ln.strip()[len(prefix):].startswith(OLD_DESC_PREFIX)]
    numbered: list[tuple[int, str]] = []
    others: list[str] = []
    for n in names:
        m = re.match(r"^(\d+)", n)
        if m:
            numbered.append((int(m.group(1)), n))
        else:
            others.append(n)
    numbered.sort(key=lambda t: -t[0])  # stable: same number keeps date order
    return [n for _, n in numbered] + others


def latest_release_tag(
    repo: Path, exclude: str | None = None, among: set[str] | None = None,
) -> str | None:
    """Return the tag of the most recent release (highest leading number, else
    newest).  *exclude* leaves one tag out; *among* limits the choice."""
    for n in _tags_newest_first(repo):
        if n != exclude and (among is None or n in among):
            return n
    return None


def branch_exists(repo: Path, branch: str) -> bool:
    """True if a *local* branch named *branch* exists (an unborn HEAD does not count)."""
    return run_git(
        repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False
    ).returncode == 0


def tag_exists(repo: Path, tag: str) -> bool:
    """True if a tag named *tag* exists."""
    return run_git(
        repo, "show-ref", "--verify", "--quiet", f"refs/tags/{tag}", check=False
    ).returncode == 0


def validate_tag_name(tag: str) -> str:
    """Return the stripped tag name, or exit when Git would refuse it."""
    tag = tag.strip()
    if not tag:
        raise SystemExit("--tag must not be empty")
    if re.search(r"[\s~^:?*\[\\]", tag) or tag.startswith("-"):
        raise SystemExit(f"Invalid tag name: {tag!r}")
    if tag == MAIN_BRANCH:
        raise SystemExit(f"'{MAIN_BRANCH}' is the branch name and cannot be a release tag.")
    r = subprocess.run(["git", "check-ref-format", f"refs/tags/{tag}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"Invalid tag name: {tag!r}")
    return tag


OLD_DESC_PREFIX = "desc/"   # description tags of the old (branch) format


def _ref_sha(repo: Path, ref: str) -> str:
    """Object name *ref* resolves to, or "" when it does not exist."""
    r = run_git(repo, "rev-parse", "-q", "--verify", ref, check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return run_git(repo, "merge-base", "--is-ancestor", ancestor, descendant,
                   check=False).returncode == 0


def _make_tag(
    repo: Path, name: str, commit: str, message: str | None,
    tagger: tuple[str, str, str] | None = None, force: bool = False,
) -> None:
    """Create tag *name* on *commit*: annotated when *message* is given (with
    the old tagger name, e-mail and date when known), else lightweight."""
    env = os.environ.copy()
    if message is not None and tagger:
        tname, temail, tdate = tagger
        env.update(GIT_COMMITTER_NAME=tname or "unknown",
                   GIT_COMMITTER_EMAIL=temail or "unknown@localhost")
        if tdate:
            env["GIT_COMMITTER_DATE"] = tdate
    cmd = ["git", "-C", str(repo), "tag"]
    if force:
        cmd.append("-f")
    if message is not None:
        cmd += ["-a", "-F", "-", name, commit]
    else:
        cmd += [name, commit]
    r = subprocess.run(cmd, input=(message + "\n") if message is not None else None,
                       text=True, capture_output=True, env=env)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())


def migrate_old_repository(repo: Path, dry_run: bool = False) -> bool:
    """Convert a repository of the old format to the tag format.

    Old format: one release branch per import (``013-name``), a description tag
    ``desc/013-name`` for each, and a pointer branch ``main``.  New format: the
    single branch ``main`` plus one release tag per release.

    1. Each ``desc/X`` tag becomes the release tag ``X`` (same commit, same
       message and tagger) and is deleted.  A tag ``X`` that points to another
       commit is kept untouched, with a warning.
    2. Each old release branch ``X`` whose tip is part of ``main`` gets a tag
       ``X`` (if missing) and is deleted, so that no commit is lost.
    3. A tag named ``main`` is deleted when its commit is part of ``main``.

    Prints every change with the command that restores it.  With *dry_run*
    nothing is changed.  Returns True when something had to be converted.
    """
    prefix = f"refs/tags/{OLD_DESC_PREFIX}"
    r = run_git(repo, "for-each-ref", "--format=%(refname)", prefix, check=False)
    old_tags = sorted((ln.strip()[len(prefix):] for ln in r.stdout.splitlines() if ln.strip()),
                      key=_release_sort_key)
    r = run_git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/", check=False)
    branches = [ln.strip()[len("refs/heads/"):] for ln in r.stdout.splitlines() if ln.strip()]
    old_branches = [b for b in branches
                    if b != MAIN_BRANCH and (re.match(r"^\d+", b) or b in old_tags)]
    has_main_tag = tag_exists(repo, MAIN_BRANCH)
    if not (old_tags or old_branches or has_main_tag):
        return False

    pre = "(dry-run) would " if dry_run else ""
    changed = False
    started = False

    def start() -> None:
        """Print the header once, before the first message."""
        nonlocal started
        if not started:
            started = True
            print(f"Old repository format found – converting to one branch '{MAIN_BRANCH}' "
                  f"+ release tags" + (" (dry-run: nothing is changed)" if dry_run else "") + " …")

    def say(msg: str) -> None:
        start()
        print(msg)

    def warn(msg: str) -> None:
        start()
        print(f"  warning: {msg}", file=sys.stderr)

    # 1) desc/X tags → release tags X
    planned: dict[str, str] = {}   # tags made (or planned in a dry-run) here
    for x in old_tags:
        if x == MAIN_BRANCH:
            warn(f"tag '{OLD_DESC_PREFIX}{x}' skipped: '{MAIN_BRANCH}' cannot be a release tag")
            continue
        old_ref, new_ref = f"refs/tags/{OLD_DESC_PREFIX}{x}", f"refs/tags/{x}"
        commit = _ref_sha(repo, f"{old_ref}^{{commit}}")
        if not commit:
            warn(f"tag '{OLD_DESC_PREFIX}{x}' does not point to a commit – kept")
            continue
        existing = _ref_sha(repo, f"{new_ref}^{{commit}}")
        if existing and existing != commit:
            warn(f"tag '{x}' points to another commit than '{OLD_DESC_PREFIX}{x}' – both kept")
            continue
        info = run_git(
            repo, "for-each-ref",
            "--format=%(objecttype)%09%(taggername)%09%(taggeremail)%09%(taggerdate:raw)",
            old_ref).stdout.rstrip("\n").split("\t")
        kind, tname, temail, tdate = (info + [""] * 4)[:4]
        old_obj = _ref_sha(repo, old_ref)
        existing_kind = run_git(repo, "for-each-ref", "--format=%(objecttype)", new_ref
                                ).stdout.strip() if existing else ""
        try:
            if kind == "tag" and existing_kind != "tag":
                message = run_git(repo, "for-each-ref", "--format=%(contents)", old_ref
                                  ).stdout.rstrip("\n")
                say(f"  {pre}convert tag '{OLD_DESC_PREFIX}{x}' → '{x}'")
                if not dry_run:
                    _make_tag(repo, x, commit, message,
                              (tname, temail.strip("<>"), tdate), force=bool(existing))
            elif kind != "tag" and not existing:
                say(f"  {pre}convert tag '{OLD_DESC_PREFIX}{x}' → '{x}'")
                if not dry_run:
                    _make_tag(repo, x, commit, None)
            else:
                say(f"  {pre}remove duplicate tag '{OLD_DESC_PREFIX}{x}' (tag '{x}' exists)")
            if not dry_run:
                run_git(repo, "tag", "-d", f"{OLD_DESC_PREFIX}{x}")
            planned[x] = commit
            say(f"    restore: git -C {repo} tag {OLD_DESC_PREFIX}{x} {old_obj}")
            changed = True
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            warn(f"could not convert tag '{OLD_DESC_PREFIX}{x}': {detail.strip()}")

    # 2) old release branches → tags; branches are removed only when main has them
    main_sha = _ref_sha(repo, f"refs/heads/{MAIN_BRANCH}")
    if old_branches and not main_sha:
        warn(f"no branch '{MAIN_BRANCH}': the old release branches are kept")
    for b in (old_branches if main_sha else []):
        tip = _ref_sha(repo, f"refs/heads/{b}")
        if not tip or not _is_ancestor(repo, tip, main_sha):
            warn(f"branch '{b}' is not part of '{MAIN_BRANCH}' – kept")
            continue
        existing = _ref_sha(repo, f"refs/tags/{b}^{{commit}}") or planned.get(b, "")
        if existing and existing != tip:
            warn(f"tag '{b}' points to another commit than branch '{b}' – branch kept")
            continue
        try:
            if not existing:
                say(f"  {pre}create tag '{b}' on the tip of branch '{b}'")
                if not dry_run:
                    _make_tag(repo, b, tip, None)
            if current_branch(repo) == b:
                say(f"  {pre}check out '{MAIN_BRANCH}' (was '{b}')")
                if not dry_run:
                    c = run_git(repo, "checkout", "-q", MAIN_BRANCH, check=False)
                    if c.returncode != 0:
                        warn(f"could not check out '{MAIN_BRANCH}': {c.stderr.strip()} – "
                             f"branch '{b}' kept")
                        continue
            say(f"  {pre}remove old release branch '{b}'")
            if not dry_run:
                run_git(repo, "update-ref", "-d", f"refs/heads/{b}", tip)
            say(f"    restore: git -C {repo} branch {b} {tip}")
            changed = True
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            warn(f"could not convert branch '{b}': {detail.strip()}")

    # 3) a tag named "main" clashes with the branch "main"
    if has_main_tag:
        tag_sha = _ref_sha(repo, f"refs/tags/{MAIN_BRANCH}^{{commit}}")
        tag_obj = _ref_sha(repo, f"refs/tags/{MAIN_BRANCH}")
        if main_sha and tag_sha and _is_ancestor(repo, tag_sha, main_sha):
            say(f"  {pre}remove tag '{MAIN_BRANCH}' (same name as the branch)")
            if not dry_run:
                run_git(repo, "tag", "-d", MAIN_BRANCH)
            say(f"    restore: git -C {repo} tag {MAIN_BRANCH} {tag_obj}")
            changed = True
        else:
            warn(f"tag '{MAIN_BRANCH}' is not part of branch '{MAIN_BRANCH}' – kept; "
                 f"remove it with 'git -C {repo} tag -d {MAIN_BRANCH}' when it is not needed")

    if changed:
        say("Conversion " + ("planned." if dry_run else "finished."))
    return changed


def ensure_main_branch(repo: Path) -> None:
    """Make sure the checked-out branch is ``main``.

    * Empty repository → HEAD is set to the (unborn) branch ``main``.
    * ``main`` already checked out → nothing to do.
    * Another branch or a detached HEAD → ``main`` is checked out.
    * No branch ``main`` in a repository with commits → error.
    """
    if not has_commits(repo):
        run_git(repo, "symbolic-ref", "HEAD", f"refs/heads/{MAIN_BRANCH}")
        return
    cur = current_branch(repo)
    if cur == MAIN_BRANCH:
        return
    if not branch_exists(repo, MAIN_BRANCH):
        raise SystemExit(
            f"The repository {repo} has no branch '{MAIN_BRANCH}' (checked out: '{cur}').\n"
            f"All commits must go on '{MAIN_BRANCH}'.  Rename your branch first, e.g.:\n"
            f"  git -C {repo} branch -m {cur if cur != 'HEAD' else '<branch>'} {MAIN_BRANCH}"
        )
    print(f"Checking out branch '{MAIN_BRANCH}' (was '{cur}') …")
    r = run_git(repo, "checkout", "-q", MAIN_BRANCH, check=False)
    if r.returncode != 0:
        raise SystemExit(f"Could not check out '{MAIN_BRANCH}':\n{r.stderr.strip()}")


def discard_failed_sync(repo: Path) -> None:
    """Undo the file sync of an import that created no commit.

    Resets the working tree to the last commit and removes the untracked files
    that the sync wrote.  ``main`` and the tags are never touched.
    """
    try:
        if has_commits(repo):
            run_git(repo, "reset", "--hard", "-q", "HEAD")
        run_git(repo, "clean", "-fdq", check=False)
        print("No commit was created; the working tree is back at the last commit.")
    except subprocess.CalledProcessError as exc:
        print(f"warning: could not restore the working tree: "
              f"{(exc.stderr or '').strip()} – use 'git reset --hard && git clean -fd'",
              file=sys.stderr)


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


def tag_name_from_archive(path: Path) -> str:
    """Derive a release tag name from an archive filename (strip known extensions)."""
    name = path.name
    lower = name.lower()
    # Strip longest matching archive suffix first (e.g. .tar.gz before .gz)
    for suffix in sorted(ARCHIVE_SUFFIXES, key=len, reverse=True):
        if lower.endswith(suffix):
            name = name[: -len(suffix)]
            break
    # Sanitise for Git ref rules: no spaces, no leading '-', etc.
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

    # Remove bytecode caches and backup files already present in the repo
    # (tracked or not) so they show up as deletions and are never committed again.
    stale: list[Path] = list(repo.rglob("__pycache__")) + list(repo.rglob("*.py[co]"))
    for pattern in ("*~", "*.bak", "*.BAK", "*.orig", "*.swp", "*.swo", "*.tmp", "#*#"):
        stale.extend(repo.rglob(pattern))
    for pc in stale:
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
            # never copy bytecode caches or backup files
            "--exclude",
            "__pycache__/",
            "--exclude",
            "*.pyc",
            "--exclude",
            "*.pyo",
            "--exclude",
            "*~",
            "--exclude",
            "*.bak",
            "--exclude",
            "*.BAK",
            "--exclude",
            "*.orig",
            "--exclude",
            "*.swp",
            "--exclude",
            "*.swo",
            "--exclude",
            "*.tmp",
            "--exclude",
            "#*#",
            f"{src}/",
            f"{repo}/",
        ]
        subprocess.run(cmd, check=True)
    else:
        # Fallback: walk and copy, then remove extras
        file_list: list[tuple[Path, Path]] = []
        for root, dirs, files in os.walk(src):
            rel = Path(root).relative_to(src)
            dest_dir = repo / rel
            dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES]
            for f in files:
                if is_ignored_path(str(Path(rel) / f) if str(rel) != "." else f):
                    continue
                file_list.append((Path(root) / f, dest_dir / f))

        for src_file, dest_file in file_list:
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
        for p in to_remove:
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
        print_error(msg)
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
        print_error(msg)
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


_FILLER_TRIGGER = re.compile(
    r"[,;:]?\s*\b(?:in order to|so that|so as to|with the latest|which (?:keeps|ensures|improves|helps|maintains)"
    r"|to (?:keep|ensure|maintain|improve|enhance|make sure|stay|help|promote|support"
    r"|facilitate|preserve|guarantee|reflect)"
    r"|for (?:better|improved|enhanced|increased|greater|clearer|consistency|clarity"
    r"|maintainability|readability))\b[^.;\n]*",
    re.IGNORECASE,
)
_FILLER_WORDS = re.compile(
    r"current|consistent|consistency|up[- ]to[- ]date|in sync|maintainab|readab|clarity|"
    r"best practice|code ?quality|overall|as expected|properly|smoothly|latest",
    re.IGNORECASE,
)
_FILLER_SENTENCE = re.compile(
    r"^\s*(?:this|these|that|it|which)\s+(?:change|update|commit|ensures?|keeps?|improves?|helps?"
    r"|maintains?|makes?)\b.*(?:" + _FILLER_WORDS.pattern + r").*$",
    re.IGNORECASE,
)


def strip_filler(msg: str) -> str:
    """Remove purpose / justification filler from a commit message.

    Drops trailing clauses such as "to keep the codebase current and consistent"
    (a clause is only removed when it contains a filler word, so a legitimate
    "add check to ensure the port is free" stays) and whole sentences such as
    "This keeps everything consistent."
    """
    lines: list[str] = []
    for line in msg.splitlines():
        line = _FILLER_TRIGGER.sub(
            lambda m: "" if _FILLER_WORDS.search(m.group(0)) else m.group(0), line
        )
        parts = re.split(r"(?<=[.!?])\s+", line)
        parts = [x for x in parts if not _FILLER_SENTENCE.match(x)]
        line = " ".join(parts).rstrip(" ,;:")
        lines.append(line)
    out = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", out)


def normalize_message(msg: str, files: list[str], reserve_words: int = 0) -> str:
    """Return a concise, filler-free commit message of at most MAX_MESSAGE_WORDS words.

    Filler clauses are removed first.  ``reserve_words`` words are kept free for
    a suffix appended later (e.g. the archive tag).  Too-long messages are cut
    at a line end, or at the word limit.  Messages are never padded: when
    nothing is left after cleaning, the message just names the changed files.
    """
    msg = strip_filler(msg.strip())
    if not msg:
        shown = ", ".join(files[:3]) + (" and others" if len(files) > 3 else "")
        msg = f"Update {shown}"
    limit = max(1, MAX_MESSAGE_WORDS - reserve_words)
    if _word_count(msg) > limit:
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
    return msg


def _diff_for_prompt(repo: Path, c: FileChange, limit: int) -> str:
    """Clipped diff of one changed file for the planning prompt ('' when unavailable)."""
    if limit < 200:
        return ""
    if c.status == "D":
        return "  (file deleted)"
    raw = _get_file_diff(repo, c.path)
    if not raw or b"\0" in raw[:8000]:
        return ""
    text = raw.decode("utf-8", "replace")
    body = text[text.find("@@"):] if "@@" in text else text
    return "  diff:\n" + "\n".join("    " + ln for ln in _clip(body, limit).splitlines())


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
        budget = MAX_PROMPT_CHARS
        for c in small:
            lines.append(
                f"- {c.path}  [{c.status}]  +{c.insertions}/-{c.deletions}  (total {c.total_lines} lines)"
            )
            total += c.total_lines
            shown_diff = _diff_for_prompt(repo, c, min(1500, budget))
            if shown_diff:
                budget -= len(shown_diff)
                lines.append(shown_diff)
        summary = "\n".join(lines)

        system = (
            "You are an expert software engineer who writes clear, conventional Git commit messages "
            "and groups related changes into logically coherent commits.\n"
            "Rules:\n"
            f"- Prefer commits whose total changed lines (insertions + deletions) stay <= {MAX_LINES_PER_COMMIT}.\n"
            "- Group files that belong to the same functional change (feature, fix, refactor, docs, etc.).\n"
            "- Order commits so that earlier commits do not depend on later ones when possible.\n"
            f"{MESSAGE_RULE}"
            f"{NO_FILLER_RULE}"
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
            plans.append(CommitPlan(message="", files=leftover))  # named after its files by normalize_message()

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
            f"{MESSAGE_RULE}"
            f"{NO_FILLER_RULE}"
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


def _stage_bytes(repo: Path, path: str, data: bytes) -> None:
    """Stage *data* as the new content of *path* without relying on stat data.

    ``git add`` skips a file whose size and mtime match its index entry.  Hunk
    commits rewrite the file with the same (source) mtime each time, so an
    edit that keeps the file size (e.g. a typo fix) was silently not staged
    ("nothing staged") on file systems with weak ctime/inode data.  Writing the
    blob and updating the index directly always stages the exact content.
    """
    r = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=data, capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git hash-object failed for {path}: {r.stderr.decode(errors='replace').strip()}")
    sha = r.stdout.decode().strip()

    mode = ""
    ls = run_git(repo, "ls-files", "-s", "--", path, check=False).stdout.split()
    if ls:
        mode = ls[0]
    elif has_commits(repo):
        lt = run_git(repo, "ls-tree", "HEAD", "--", path, check=False).stdout.split()
        if lt:
            mode = lt[0]
    if mode not in ("100644", "100755"):
        full = repo / path
        mode = "100755" if full.exists() and os.access(full, os.X_OK) else "100644"
    run_git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{sha},{path}")


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
        for i, plan in enumerate(plans, start=1):
            print(f"\n=== Commit {i}/{len(plans)} ===")
            print(f"Message: {plan.message}")
            print(f"Files: {', '.join(plan.files)}")

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
                        print(f"  hunk {idx} of {path}: {h.header} (+/-{h.change_lines})")
                    if len(st["applied"]) == len(st["hunks"]) and current != final:
                        raise HunkError(
                            f"{path}: after the last hunk the file differs from the synced source"
                        )
                    _write_file(repo, path, current, mtime)
                    _stage_bytes(repo, path, current)
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
                print("  (nothing staged – skipping)")
                continue

            # Commit date = mtime of the source files (kept from before any rewrite)
            env = base_env.copy()
            times = [snapshot[p][1] for p in plan.files if snapshot.get(p)]
            mtime_c = max(times) if times else _max_mtime_for_paths(repo, plan.files)
            if mtime_c is not None:
                date_str = _format_git_date(mtime_c)
                env["GIT_AUTHOR_DATE"] = date_str
                env["GIT_COMMITTER_DATE"] = date_str
                print(f"  date: {date_str} (from source file mtime)")
            else:
                print("  date: (current time – no surviving files for mtime)")

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
            print("  committed.")
        # 4) the loop repeats until every planned hunk is committed
    except (Exception, KeyboardInterrupt) as exc:
        print("\n" + red(f"ERROR in commit {made + 1}/{len(plans)}: {exc}", sys.stdout))
        print(f"Reverting the {made} commit(s) created by this run …")
        _rollback(repo, start_head, snapshot)
        where = f"HEAD is back at {start_head[:12]}" if start_head else "the repository is empty again"
        raise SystemExit(
            f"Aborted: {exc}\nAll {made} commit(s) of this run were reverted; {where}."
        )


def resolve_export_ref(repo: Path, name: str | None) -> tuple[str, str]:
    """Resolve what to export; return ``(git_ref, label)``.

    * ``None`` / ``__LAST__`` / ``main`` → the tip of ``main``.  The label is
      the newest release tag at that commit (else ``main``).
    * Otherwise *name* is a release tag, or an archive basename (with or
      without extension) matching ``tag_name_from_archive``.
    """
    if not has_commits(repo):
        raise SystemExit("The repository has no commits – nothing to export.")
    tags = list_release_tags(repo)

    if name is None or name == "__LAST__" or name == MAIN_BRANCH:
        ref = f"refs/heads/{MAIN_BRANCH}" if branch_exists(repo, MAIN_BRANCH) else "HEAD"
        tip = run_git(repo, "rev-parse", ref).stdout.strip()
        commits = tag_commits(repo)
        here = {t for t, c in commits.items() if c == tip}
        return ref, (latest_release_tag(repo, among=here) or MAIN_BRANCH)

    if not tags:
        raise SystemExit("No release tags found – nothing to export by name.")

    # Exact tag match
    if name in tags:
        return f"refs/tags/{name}", name

    # Archive-style name → strip extension and match
    candidate = tag_name_from_archive(Path(name))
    if candidate in tags:
        return f"refs/tags/{candidate}", candidate

    # Case-insensitive match
    lower_map = {t.lower(): t for t in tags}
    for key in (name.lower(), candidate.lower()):
        if key in lower_map:
            return f"refs/tags/{lower_map[key]}", lower_map[key]

    raise SystemExit(
        f"No release tag matching '{name}' (tried '{candidate}').\n"
        f"  Available release tags: {', '.join(tags)}"
    )


def export_archive(
    repo: Path,
    ref: str,
    label: str,
    output: Path | None = None,
    fmt: str = "zip",
    prefix: str | None = None,
    strip: bool = True,
) -> Path:
    """Recreate an archive from the tree at *ref* using ``git archive``.

    *label* (a release tag name, or ``main``) names the archive file and the
    optional top-level directory.  By default (``strip=True``) the files are
    stored directly at the root of the archive.  With ``strip=False`` they are
    placed below a single top-level directory named *label*.

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
        output = Path.cwd() / f"{label}{ext}"
    else:
        output = output.expanduser().resolve()
        if output.is_dir():
            output = output / f"{label}{ext}"

    # Prefix so unpacking creates a single top-level directory (matches import
    # strip).  strip (the default) drops it: files are stored at the archive root.
    if strip:
        prefix = ""
    elif prefix is None:
        prefix = f"{label}/"

    output.parent.mkdir(parents=True, exist_ok=True)
    print(f"Exporting '{label}' → {output} ({git_format}) …")
    cmd = [
        "git",
        "-C",
        str(repo),
        "archive",
        f"--format={git_format}",
        *([f"--prefix={prefix}"] if prefix else []),
        f"--output={output}",
        ref,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(
            f"git archive failed:\n{r.stderr or r.stdout}"
        )
    size = output.stat().st_size if output.exists() else 0
    print(f"  wrote {output} ({size} bytes)")
    return output


class _RedArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that prints usage errors in bright red."""

    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(2, red(f"{self.prog}: error: {message}") + "\n")


def _main() -> None:
    parser = _RedArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "AI Git Committer – import snapshots into Git with LLM commit messages,\n"
            "or export/recreate archives from existing release tags.\n"
            "\n"
            "All commits go on ONE branch named 'main'.  Every import is marked\n"
            "with a release tag (the archive name); its message is the description.\n"
            "\n"
            "Five modes (--repo defaults to ./repository):\n"
            "\n"
            "  LIST    – print the release tags with descriptions (the default\n"
            "            when neither --add nor --export is given)\n"
            "            requires: nothing (--repo defaults to ./repository)\n"
            "\n"
            "  REVERT  – remove the last release (tag) and move main back\n"
            "            requires: --revert (not with --add / --export / --list)\n"
            "\n"
            "  CLEANUP – permanently delete commits no branch or tag reaches any more\n"
            "            (reflog expire + gc --prune=now), no confirmation\n"
            "            requires: --cleanup (not with --add / --export / --revert / --list)\n"
            "\n"
            "  IMPORT  – sync --add into --repo and create commits (needs LLM)\n"
            "            requires: --url --add --name\n"
            "            optional: --model (default: first/active model from GET /v1/models)\n"
            "\n"
            "  EXPORT  – recreate a zip/tar from a release tag (default: tip of main)\n"
            "            requires: --export [NAME]\n"
            "            optional: --tag NAME (overrides NAME)\n"
            "            does NOT need --url, --model, --add, or --name"
        ),
        epilog=(
            "examples:\n"
            "  # Import an archive (commits on main, tag = archive name)\n"
            "  %(prog)s --repo ./repo --url http://localhost:8080/v1 \\\n"
            "      --add snapshot-2020.zip --name 'Botter <ai@localhost>'\n"
            "\n"
            "  # '--add' may be omitted: a stray path is taken as the source\n"
            "  %(prog)s --url http://localhost:8080/v1 --name 'Botter <ai@localhost>' \\\n"
            "      snapshot-2020.zip\n"
            "\n"
            "  # Import with an explicit model\n"
            "  %(prog)s --repo ./repo --url http://localhost:8080/v1 \\\n"
            "      --model qwen2.5-coder --add snapshot-2020.zip \\\n"
            "      --name 'Botter <ai@localhost>'\n"
            "\n"
            "  # Export the tip of main\n"
            "  %(prog)s --repo ./repo --export\n"
            "\n"
            "  # Export a specific release tag / archive name\n"
            "  %(prog)s --repo ./repo --export 013-llm-errors\n"
            "  %(prog)s --repo ./repo --export 013-llm-errors.zip \\\n"
            "      --export-format tar.gz --export-output /tmp/out.tar.gz\n"
            "\n"
            "  # Export via --tag (overrides --export NAME)\n"
            "  %(prog)s --repo ./repo --export --tag 013-llm-errors\n"
            "\n"
            "  # List the release tags; needs no other option\n"
            "  %(prog)s --repo ./repo --list\n"
            "  %(prog)s --repo ./repo          # same: --list is the default\n"
            "                                 # when neither --add nor --export is given\n"
            "\n"
            "  # Remove the last release again (e.g. after a bad import)\n"
            "  %(prog)s --repo ./repo --revert\n"
            "\n"
            "  # Permanently delete the commits of removed releases (no confirmation)\n"
            "  %(prog)s --repo ./repo --cleanup\n"
            "\n"
            "  # Export keeping the top-level <tag>/ directory\n"
            "  %(prog)s --repo ./repo --export 013-llm-errors --no-strip\n"
        ),
    )
    # --- always ---
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path("repository"),
        help="Path to the local Git repository (created on import if missing; "
        "default: ./repository)",
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
        "source",
        nargs="?",
        default=None,
        type=Path,
        metavar="PATH",
        help="A stray source directory/archive without --add is taken as --add PATH",
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
        "--tag",
        default=None,
        metavar="NAME",
        help="Release tag (import: created on the new tip of main; default: archive "
        "basename if --add is an archive; the tag must not exist yet) or release "
        "to export (with --export; overrides any NAME given to --export)",
    )
    import_opts.add_argument(
        "--branch",
        default=None,
        help=argparse.SUPPRESS,  # removed option: gives a clear error
    )
    import_opts.add_argument(
        "--mention-archive",
        action="store_true",
        help="Append the archive filename to each commit message (default: off)",
    )
    import_opts.add_argument(
        "--describe",
        default=None,
        metavar="TEXT",
        help="Description stored as the message of the release tag after an "
        "import.  Overrides the automatic summary, which is made "
        "by the model from conversation.md (or README…, CHANGELOG…) when such "
        "a file is present in the repository",
    )
    import_opts.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan commits only; do not sync or write commits",
    )
    # --- export mode ---
    list_opts = parser.add_argument_group(
        "list mode (needs no other option; the default when neither --add nor --export is given)"
    )
    list_opts.add_argument(
        "--list",
        action="store_true",
        help="Print the release tags of the repository with their "
        "descriptions and exit.  This is the default when neither --add nor "
        "--export is given.  (No listing is shown after an import or export.)",
    )
    revert_opts = parser.add_argument_group(
        "revert mode (needs no other option; cannot be combined with --add / --export)"
    )
    revert_opts.add_argument(
        "--revert",
        action="store_true",
        help="Remove the last release: delete its tag (the most recent "
        "import: highest numeric prefix, else newest) and move main back to "
        "the previous release tag.  Refuses to remove the only release tag, "
        "to run with uncommitted changes, or when main is not at that tag.",
    )
    cleanup_opts = parser.add_argument_group(
        "cleanup mode (needs no other option; cannot be combined with --add / --export / --revert / --list)"
    )
    cleanup_opts.add_argument(
        "--cleanup",
        action="store_true",
        help="Permanently delete all commits and objects that no branch or "
        "tag reaches any more (e.g. those of reverted releases): expires the "
        "reflog and runs 'git gc --prune=now'.  Runs WITHOUT confirmation and "
        "cannot be undone.",
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
        help="Export mode: recreate archive from a release tag. "
        "Omit NAME to export the tip of main; "
        "NAME may be a release tag or archive basename (e.g. 013-llm-errors[.zip]). "
        "With --tag NAME, that release is exported instead",
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
        help="Output file or directory for --export (default: <tag>.zip in cwd)",
    )
    export_opts.add_argument(
        "--strip",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="With --export: strip all directories from the archive, i.e. do "
        "not wrap the files in a top-level <tag>/ directory (default: on). "
        "Use --no-strip to keep the <tag>/ directory",
    )
    args = parser.parse_intermixed_args()

    if args.branch is not None:
        parser.error(
            "--branch was removed: all commits go on the branch 'main' and each "
            "release is a tag.  Use --tag NAME instead."
        )

    # A stray path without --add means "--add PATH".
    if args.source is not None:
        if args.add is not None:
            parser.error(
                f"two sources given: --add {args.add} and '{args.source}' – use only one"
            )
        if args.export is not None or args.revert or args.cleanup or args.list:
            parser.error(
                f"unexpected argument '{args.source}': a source path is only valid for an "
                "import (not with --export / --revert / --cleanup / --list)"
            )
        args.add = args.source
        print(f"Note: no --add given – using '{args.source}' as --add.", file=sys.stderr)

    if args.cleanup:
        if args.export is not None or args.add is not None or args.list or args.revert:
            parser.error(
                "--cleanup cannot be combined with --add, --export, --revert or --list"
            )
    elif args.revert:
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
                ("--tag", "tag"), ("--mention-archive", "mention_archive"),
                ("--describe", "describe"), ("--dry-run", "dry_run"),
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

    # ----- cleanup mode -----
    if args.cleanup:
        note_ignored("cleaning up the repository")
        require_git_repo(args.repo)
        migrate_old_repository(args.repo)
        cleanup_repository(args.repo)
        return

    # ----- revert mode -----
    if args.revert:
        note_ignored("reverting the last release")
        require_git_repo(args.repo)
        migrate_old_repository(args.repo)
        revert_last_release(args.repo)
        return

    # ----- list mode -----
    if args.list:
        if args.export is not None or args.add is not None:
            parser.error("--list cannot be combined with --export or --add")
        note_ignored("listing release tags")
        require_git_repo(args.repo)
        list_tags(args.repo)
        return

    # ----- export mode -----
    if args.export is not None:
        require_git_repo(args.repo)  # validate without auto-creating
        # --tag NAME wins; else --export NAME; else the tip of main
        if args.tag is not None:
            ref, label = resolve_export_ref(args.repo, args.tag)
        else:
            ref, label = resolve_export_ref(
                args.repo, None if args.export == "__LAST__" else args.export
            )
        export_archive(
            args.repo,
            ref,
            label,
            output=args.export_output,
            fmt=args.export_format,
            strip=args.strip,
        )
        return

    ensure_git_repo(args.repo)

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

    # Release tag: an explicit --tag wins; otherwise it is derived from the
    # archive name.  A release tag that already exists means the release was
    # imported before (or clashes with another one): refuse before doing
    # anything else.
    _src_path = args.add.expanduser().resolve()
    tag = args.tag
    tag_derived = False
    if tag is None and is_archive(_src_path):
        tag = tag_name_from_archive(_src_path)
        tag_derived = True
    if tag:
        tag = validate_tag_name(tag)
        if tag_exists(args.repo, tag):
            raise SystemExit(
                f"Error: release tag '{tag}' already exists in {args.repo} – "
                f"refusing to import '{_src_path.name}' again.\n"
                "Rename the archive, or pass another name with --tag NAME.\n"
                f"(If an earlier import of this archive failed after the tag was made, "
                f"remove the leftover tag with 'git tag -d {tag}' and try again.)"
            )

    args.model = resolve_model(args.url, args.model, args.api_key)
    author_name, author_email = parse_author(args.name)

    # Optional: refuse if there are already staged changes
    staged = run_git(args.repo, "diff", "--cached", "--name-only")
    if staged.stdout.strip():
        raise SystemExit(
            "Repository has staged changes. Please commit or unstage them first."
        )

    # Make sure the model really answers before the repository is touched
    # (checkout, sync and commits all come after this point).
    check_model(args.url, args.model, args.api_key)

    if tag and tag_derived:
        print(f"Archive detected – using release tag '{tag}' (override with --tag)")
    elif not tag:
        print("Note: no release tag will be created (use --tag NAME for a directory source).")
    ensure_main_branch(args.repo)

    # Safety: the sync step runs "rsync --delete" into the repo, so a source
    # (archive or directory) located inside the repo would be deleted by it.
    _src_abs = args.add.expanduser().resolve()
    _repo_abs = args.repo.expanduser().resolve()
    if _src_abs == _repo_abs or _repo_abs in _src_abs.parents:
        raise SystemExit(
            f"Source {_src_abs} is inside the target repository {_repo_abs}; "
            "syncing would delete it. Move the source outside the repository."
        )

    committed = False
    synced = False
    release_tag: str | None = None
    try:
        with resolve_source(args.add) as effective_src:
            if not effective_src.is_dir():
                raise SystemExit(
                    f"Internal error: resolved source is not a directory: {effective_src} "
                    f"(from --add {args.add}). Is this the current ai_git_commit.py?"
                )
            # Warn & abort if the (plain) source tree has extra directory layers
            # relative to the repository.  Archives are already stripped by
            # resolve_source, so this mainly protects directory inputs.
            warning = detect_extra_directory_layer(effective_src, args.repo)
            if warning:
                print("\n*** Extra directory layer detected ***\n")
                print(warning)
                raise SystemExit(1)

            print(f"Syncing {effective_src} → {args.repo} …")
            if not args.dry_run:
                sync_source_to_repo(effective_src, args.repo)
                synced = True
            else:
                print("  (dry-run: skip actual file sync)")

            print("Collecting changes …")
            changes = collect_changes(args.repo)
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
                changes, args.repo, args.url, args.model, args.api_key,
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

            start_head = (
                run_git(args.repo, "rev-parse", "HEAD").stdout.strip()
                if has_commits(args.repo) else None
            )
            apply_commits(args.repo, plans, author_name, author_email)
            print("\nDone.")
            committed = True

            # Release tag with the description of this version on the tip of
            # main.  Failures here never undo the (already finished) commits.
            if tag:
                try:
                    description = (args.describe or "").strip()
                    if not description:
                        doc = find_description_doc(args.repo)
                        if doc:
                            print(f"Summarizing the progress from '{doc}' for the release description …")
                            description = summarize_progress(
                                args.repo, doc, start_head, args.url, args.model, args.api_key
                            )
                            if not description:
                                print(f"No description derivable from the changes of '{doc}' (unchanged, "
                                      "or only about the document itself) – the release tag gets "
                                      "no description (use --describe TEXT to set one).")
                        else:
                            print("No conversation.md / README found – the release tag gets "
                                  "no description (use --describe TEXT to set one).")
                    tag_release(args.repo, tag, description, author_name, author_email)
                    release_tag = tag
                except Exception as exc:  # noqa: BLE001
                    print(f"warning: release tag '{tag}' not created: {exc}\n"
                          f"  create it with: git -C {args.repo} tag {tag} HEAD",
                          file=sys.stderr)
    finally:
        # An import that ended without a commit (failure and rollback,
        # dry-run, "no changes", empty plan) must not leave synced files behind.
        if synced and not committed:
            discard_failed_sync(args.repo)

    # Only the name of the release tag; printed after the temporary-directory
    # cleanup so it really is the last line of output.
    if release_tag:
        print(release_tag)


def main() -> None:
    """Run the program; every error message (SystemExit with a text) is shown
    in bright red on stderr."""
    try:
        _main()
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print_error(exc.code)
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()

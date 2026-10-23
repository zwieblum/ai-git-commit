# AI Git Committer

Sync a non-Git source directory (or archive) into a local Git repository and
create one or more commits whose messages are written by an LLM via an
OpenAI-compatible API.

The tool is designed for reconstructing project history from a series of
snapshots or old archives while keeping commits small, functionally coherent,
and chronologically ordered by the original file modification times.

---

## Features

| Feature | Description |
|--------|-------------|
| **Source flexibility** | `--add` can be a plain directory **or** an archive (`.zip`, `.tar`, `.tar.gz`, `.tgz`, `.tar.bz2`, `.tar.xz`, …). |
| **Archive handling** | Archives are extracted to a temporary directory; single leading directory layers are stripped automatically; the temp tree is removed afterwards. Optionally (`--mention-archive`) the archive filename can be included in every commit message (off by default). |
| **Extra-layer guard** | If a plain directory nests content one or more levels deeper than the repository root (and names overlap), the program **warns and stops**. |
| **Auto-init** | If `--repo` does not exist or is not yet a repository, the directory is created and `git init` is run. |
| **LLM commit messages** | An OpenAI-compatible `/v1/chat/completions` endpoint produces a JSON plan of functionally coherent commits. |
| **Size limit** | Tries to keep each commit ≤ **200 changed lines** (insertions + deletions). A single oversized file is committed alone. |
| **Chronological dates** | Author and committer dates are taken from the newest modification time of the files in that commit (preserved from `--add` / the archive). |
| **Output** | Plain text output, no progress bars. |
| **Branch** | `--branch NAME` checks out or creates the branch before committing. When `--add` is an archive and `--branch` is omitted, the archive basename (without extension) is used automatically. After a successful import the tip of the release branch is also pointed to by a local `main` branch (any previous `main` is removed first) for GitHub compatibility; commits still land on the numbered release branch. |
| **Ignored paths** | Bytecode caches (`__pycache__`, `*.pyc`, `*.pyo`) and common backup/editor files (`*~`, `*.bak`, `*.BAK`, `*.orig`, `*.swp`, `*.swo`, `*.tmp`, `#*#`) are never synced into the repository and never appear in commits. Existing copies in the repo are deleted so they show up as deletions. |
| **Export** | `--export [NAME]` recreates a zip/tar from a branch (default: branch with the newest commit). |
| **Dry-run** | `--dry-run` prints the planned commits without writing anything. |

---


## Requirements

### Runtime

- **Python** 3.9 or newer
- **git** on `$PATH`
- **rsync** (recommended; a pure-Python fallback is used if missing)

### Python packages

```bash
pip install -r requirements.txt
```

| Package   | Purpose                          |
|-----------|----------------------------------|
| `requests`| HTTP client for the LLM API      |

No other third-party libraries are required (standard library covers `zipfile`,
`tarfile`, `argparse`, `pathlib`, etc.).

### LLM endpoint

Any server that implements the OpenAI Chat Completions API shape:

```
POST {base_url}/chat/completions
```

Examples: local llama.cpp / ollama / vLLM / OpenAI / any compatible proxy.
Many local servers ignore the API key; set `--api-key` or `OPENAI_API_KEY` only
when required.

---

## Installation

```bash
# from the extracted archive
cd ai-git-committer
pip install -r requirements.txt
chmod +x ai_git_commit.py   # optional
```

---

## Usage

### Import mode

Requires `--url`, `--add`, and `--name` (`--repo` defaults to `./repository`). `--model` is optional (defaults to the first/active model from `GET {url}/models`).

```bash
python ai_git_commit.py \
  --repo  /path/to/the/repository \
  --url  http://localhost:8080/v1 \
  --model qwen2.5-coder \
  --add  /path/to/the/new/content_or_archive.zip \
  --name "Botter <ai@localhost>"
```

### Export mode

Requires only `--export` (and `--repo`, default `./repository`). **Does not** need `--url`, `--model`, `--add`, or `--name`.

```bash
# Last archive (branch with the newest commit)
python ai_git_commit.py --repo /path/to/repo --export

# Specific branch or archive name
python ai_git_commit.py --repo /path/to/repo --export 013-llm-errors
python ai_git_commit.py --repo /path/to/repo --export 013-llm-errors.zip

# Format and output path
python ai_git_commit.py --repo /path/to/repo --export 014-clean-llm-err \
  --export-format tar.gz --export-output /tmp/out.tar.gz

# Keep the top-level <branch>/ directory inside the archive
python ai_git_commit.py --repo /path/to/repo --export 014-clean-llm-err --no-strip
```

The archive is written to the **current working directory** (not into the
repository) unless `--export-output` is given. By default (`--strip`) the files
are stored directly at the archive root; `--no-strip` wraps them in a single
`<branch>/` directory.

### List mode

Requires no other option (`--repo` defaults to `./repository`). Prints **local release branches** only (same idea as `git branch`, not `git branch --all`). Remote-tracking refs (`remotes/origin/…`) are omitted, and so are the non-release branches `main` (GitHub-compatibility pointer at the latest tip) and `all`. The current branch is marked with `*`. Each listed branch that has a description tag (`desc/<branch>`, see "Branch description") is followed by its **short description** (the first paragraph of the tag message, ~50 words, on one line), e.g. `  013-llm-errors  Adds retry handling for LLM errors, …`. When writing to a terminal, the description is printed in **gray** and the current branch (`*` line) in **light green** (ANSI escape sequences; disabled when the output is piped/redirected or the `NO_COLOR` environment variable is set). **Error messages** (including command-line usage errors) are printed to stderr in **bright red** under the same terminal / `NO_COLOR` rules. List mode is the **default**: whenever neither `--add` nor `--export` is given, `--list` is assumed:

```bash
python ai_git_commit.py --repo /path/to/repo --list
python ai_git_commit.py --repo /path/to/repo          # same thing
```

Import-only options given without `--add` (e.g. `--name`, `--dry-run`) are ignored in list mode; a note on stderr says so.

`--list` cannot be combined with `--export` or `--add`, and it never creates the repository.

### Revert mode

Requires no other option (`--repo` defaults to `./repository`). Removes the **last branch** – the one holding the most recent import – from the repository, e.g. to undo a bad import:

```bash
python ai_git_commit.py --repo /path/to/repo --revert
```

* "Last" is the branch with the highest leading number (`013-llm-errors` beats `012-…`); without numbered branches the branch whose tip is newest is used.
* If that branch is checked out, the previous branch (the next one by the same rule) is checked out first, so the working tree returns to the previous release.
* The branch is deleted with `git branch -D`. The output shows the tip commit, e.g. `Restore with: git -C repo branch 013-llm-errors <sha>`, so the removal can be undone.
* Safety: the program refuses to remove the **only** branch, and refuses to run when the working tree has uncommitted or untracked changes (the message shows how to discard them, e.g. after a failed import).
* `--revert` cannot be combined with `--add`, `--export` or `--list` (error). Import-only options such as `--name` are ignored with a note on stderr. It never creates the repository.

### Branch description (annotated tag)

After an import that created commits, the tip of the branch gets an **annotated tag** `desc/<branch>` (e.g. `desc/013-llm-errors`) whose message is the description of that version:

* If the repository contains a description document in its root – `README*` first, then `conversation.*`, `CHANGELOG*`, `CHANGES*`, `HISTORY*`, `NOTES*` (case-insensitive) – the model writes a description: a **short description** of ~50 words on a single line (cut at 70), then optionally a body with details (≤ 170 words in total). The **only source is the diff of that document** between the previous tip and the new tip (for a first import: the whole document); commit messages and other files are not used. The description covers the **project changes**; it never mentions README, conversation, any document name, or the fact that a log was updated (prompt rule, one retry, and mentioning sentences are dropped). If the document did not change, or nothing is left after that, no tag is created (a note is printed).
* The description is **plain text**: any markup the model produces (emphasis, backticks, headings, list markers, quotes, links, code fences) is removed; `--list` strips it from older tags as well.
* `--describe TEXT` overrides this: TEXT is used as the tag message (no model call).
* Without a description document and without `--describe` no tag is created (a note is printed).
* Re-importing into an existing branch (`--branch NAME`) replaces its tag. `--dry-run` and imports without commits create nothing. A failure while creating the tag only prints a warning; the commits stay.
* Read the descriptions with `git tag -n99 -l 'desc/*'`; `git show desc/<branch>`.
* `--revert` removes the tag of the removed branch as well (otherwise it would keep the commits alive). The tag is local and not part of `--export`.

### Omitting `--add`

`--add` may be left out: a single stray path (directory or archive) is taken as the source, in any position on the command line:

```bash
python ai_git_commit.py --url http://localhost:8080/v1 --name 'Botter <ai@localhost>' snapshot-2020.zip
```

A note is printed. Giving both `--add X` and a stray path is an error, as is a stray path together with `--export`, `--revert`, `--cleanup` or `--list` (for `--export`, a word directly after it is the branch name).

### Cleanup mode

Requires no other option (`--repo` defaults to `./repository`). Permanently deletes everything that no branch reaches any more, e.g. the commits of branches removed with `--revert`:

```bash
python ai_git_commit.py --repo /path/to/repo --cleanup
```

* Runs `git reflog expire --expire=now --all` and `git gc --prune=now` **without any confirmation**; it cannot be undone (the `Restore with:` hint printed by `--revert` stops working).
* Output: number of unreachable commits/objects removed and the repository size before/after.
* Affects only the local repository, and only unreachable data; all branches stay untouched (their reflog history is dropped, too).
* `--cleanup` cannot be combined with `--add`, `--export`, `--revert` or `--list` (error). Import-only options are ignored with a note. It never creates the repository.

### Arguments

| Flag | Import | Export | Description |
|------|:------:|:------:|-------------|
| `--repo` | no | no | Path to the local Git repository (created on import if missing). Default: `./repository` (the former name `--git` is no longer accepted) |
| `--url` | **yes** | no | OpenAI-compatible base URL, e.g. `http://localhost:8080/v1` |
| `--model` | no | no | Model name (default: first/active model from `GET {url}/models`) |
| `--add` | **yes** | no | Source directory **or** archive (`.zip` / `.tar*` …) |
| `--name` | **yes** | no | Committer identity: `"Name <email@domain>"`; other forms are accepted too, see [Identity formats](#identity-formats) |
| `--api-key` | no | no | API key (default: env `OPENAI_API_KEY`) |
| `--branch` | no | no | Branch to commit on (default: archive basename when `--add` is an archive) |
| `--mention-archive` | no | no | Include archive filename in commit messages (default: off) |
| `--describe` | no | no | Text for the branch description tag `desc/<branch>`; overrides the automatic summary from README / conversation… |
| `--dry-run` | no | no | Plan commits only; do not write |
| `--export [NAME]` | no | **yes** | Export mode: recreate archive from branch (`NAME` optional = last) |
| `--export-format` | no | no | `zip` (default), `tar`, `tar.gz`, or `tgz` |
| `--export-output` | no | no | Output path (default: `<branch>.zip` in the current directory, i.e. not inside the repository) |
| `--revert` | no | no | Revert mode: remove the last branch from the repository (needs no other option; not combinable with `--add` / `--export` / `--list`) |
| `--cleanup` | no | no | Cleanup mode: permanently delete all commits/objects no branch reaches (reflog expire + `gc --prune=now`), no confirmation; not combinable with `--add` / `--export` / `--revert` / `--list` |
| `--list` | no | no | List mode: print local release branches only (hides `main`, `all`, and remote-tracking refs); needs no other option; this is the default when neither `--add` nor `--export` is given |
| `--strip` / `--no-strip` | no | no | With `--export`: `--strip` (default) stores the files at the archive root without a top-level `<branch>/` wrapper; `--no-strip` keeps that wrapper directory |

### Example – rebuild history from old archives

```bash
# oldest snapshot first
python ai_git_commit.py --url … --model … --repo ./proj --add snapshot-2020.zip \
  --name "Botter <bot@localhost>"
python ai_git_commit.py --url … --model … --repo ./proj --add snapshot-2021.tar.gz \
  --name "Botter <bot@localhost>"
python ai_git_commit.py --url … --model … --repo ./proj --add snapshot-2022/ \
  --name "Botter <bot@localhost>"
```

Because each commit inherits the file modification times from the source, the
resulting Git history is chronologically ordered.

---

## Functional description

### High-level flow

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  --add      │────▶│ resolve_source() │────▶│ effective_src   │
│ (dir/arch)  │     │ extract + strip  │     │ (plain dir)     │
└─────────────┘     └──────────────────┘     └────────┬────────┘
                                                      │
                                                      ▼
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  --repo      │────▶│ ensure_git_repo  │────▶│ working tree    │
│ (path)      │     │ (init if needed) │     │                 │
└─────────────┘     └──────────────────┘     └────────┬────────┘
                                                      │
                      detect_extra_directory_layer()  │
                      (abort if nested wrapper)       │
                                                      ▼
                                             sync_source_to_repo()
                                             (rsync or copy)
                                                      │
                                                      ▼
                                             collect_changes()
                                             (numstat + porcelain)
                                                      │
                                                      ▼
                                             plan_commits() ──▶ LLM
                                             (JSON list of commits)
                                                      │
                                                      ▼
                                             apply_commits()
                                             (stage, date from mtime,
                                              commit)
```

### 1. Source resolution (`resolve_source`)

- **Directory** → used as-is.
- **Archive** → extracted into a temporary directory; any chain of single
  top-level directories is stripped (common for GitHub/GitLab zip downloads);
  the content root is yielded; the temporary tree is deleted on exit.

### 2. Repository preparation (`ensure_git_repo`)

- Path missing → `mkdir -p` + `git init`.
- Path exists but is not a Git work tree → `git init` in place.
- Otherwise verified with `git rev-parse --is-inside-work-tree`.
- Refuses to continue if anything is already staged.

### 3. Extra-directory-layer guard

Compares the top-level names of the repository with the content found after
walking down single-directory wrappers in the source. If there is meaningful
overlap, the program prints a diagnostic and exits with status 1. This
prevents accidentally nesting the whole project under an extra folder.

### 4. Sync

- Prefer `rsync -a --delete --exclude .git`.
- Fallback: pure-Python walk + `shutil.copy2` (preserves mtimes), then
  removal of obsolete files.

### 5. Change collection

- `git diff --numstat HEAD` (skipped when the repo has no commits yet).
- `git status --porcelain -uall` for untracked / deleted / renames.
- Line counts for new files are obtained by counting lines in the file.

### 6. Commit planning (LLM)

A compact summary of every changed path (status + insertions/deletions) is
sent to the model. The model must return a JSON array:

```json
[
  {"message": "feat: add X", "files": ["src/a.py", "src/b.py"]},
  {"message": "fix: handle edge case", "files": ["src/c.py"]}
]
```

Rules enforced in the prompt (and cleaned up in code):

- Prefer commits whose total changed lines ≤ 200.
- Group by functional coherence.
- Commit messages state clearly and concisely what changed: one short subject line, an optional body, at most 40 words in total. The model receives the (clipped) diff of every small changed file, so it can name the concrete change.
- Filler is never allowed. The prompts forbid purpose and justification text, and `strip_filler()` removes it in code from every message: clauses such as "to keep the codebase current and consistent", "to improve readability", "with the latest source changes", and sentences such as "This keeps everything consistent." A clause is removed only if it contains a filler word, so "add check to ensure the port is free" is kept.
- Messages are never padded. If nothing is left after cleaning (or the model omits a message), the message is just `Update <files>`.
- Every changed path appears in exactly one commit.
- Leftover paths (if the model omits any) are placed in a final catch-all commit.

**Large files (> 200 changed lines): the model plans, the program splits.**
The program parses the diff `HEAD → working file` into hunks and shows them to
the model, which only returns a *plan*: the order of the hunks and one commit
message per hunk.

```json
[
  {"hunk": 2, "message": "Add retry loop to the download helper ..."},
  {"hunk": 0, "message": "Rename config keys to match the new schema ..."}
]
```

Unknown/duplicate indices are dropped; hunks the model forgot get a generic
message and are appended in file order. Before planning, the program checks
that applying all hunks to the HEAD version reproduces the working file; if not,
the file is committed as a whole. The splitting itself is done in step 7.

### 7. Applying commits

Large files are split **by the program, one hunk at a time** (no `git apply`,
no model involvement):

1. take the file as it is in the repository (`git show HEAD:<file>`),
2. apply exactly **one** hunk (line shifts caused by already committed hunks
   are tracked; the lines the hunk expects are verified first),
3. stage and commit it,
4. repeat until all hunks are committed; the result must equal the synced file.

**If any commit fails, all commits made by this run are reverted:** HEAD is
reset to where it was before the first commit and the synced files are put
back in the working tree (uncommitted); the program then exits with an error.
A branch that this run created for the import is **deleted** again (the
previously checked-out branch is checked out and the synced files are removed
from the working tree). The same happens when an import ends without any commit
(error before committing, `--dry-run`, "no changes"). Branches that already
existed (`--branch NAME`) are never deleted.

For every planned commit (small files / deletions are staged whole):

For each planned commit:

1. Stage only the listed paths (`git add` / `git rm`).
2. Compute the newest `st_mtime` among the surviving files.
3. Set `GIT_AUTHOR_DATE` and `GIT_COMMITTER_DATE` to that timestamp (UTC).
4. Set author/committer name and email from `--name`.
5. `git commit -m …`.


---

## Architecture

```
ai_git_commit.py
├── CLI (argparse)
├── Identity parsing          parse_author()
├── Repository bootstrap      ensure_git_repo()
├── Source resolution         is_archive(), resolve_source(),
│                             _strip_to_content_root()
├── Layout safety             detect_extra_directory_layer()
├── File sync                 sync_source_to_repo()
├── Diff inspection           has_commits(), collect_changes()
├── LLM client                call_llm()
├── Planning                  plan_commits()
├── Commit application        _max_mtime_for_paths(),
│                             _format_git_date(), apply_commits()
└── Orchestration             main()
```

All Git interaction goes through the `git` CLI (`subprocess`); no GitPython
dependency. HTTP is handled solely by `requests`.

---

## Limitations

- Small files are grouped at **file level**. A single file whose diff exceeds
  200 lines is split into hunks: the model plans order and messages, the
  program applies and commits one hunk at a time. A new file consists of a
  single hunk and is therefore committed as a whole.
- The model must return valid JSON; the script extracts the first JSON array it
  finds (tolerates markdown fences).
- Pure-deletion commits have no surviving files for mtime and therefore use the
  current time.
- Complex renames rely on Git’s own status/diff output.
- The extra-layer detector only triggers when the repository already has
  content to compare against.

---

## License

Copyright © Dr. Nikolaus Klepp - office@klepp.biz - https://klepp.biz

This program is free software under the **GNU General Public License v3.0**
(or, at your option, any later version).  See the file `LICENSE` for the
full text.

SPDX-License-Identifier: GPL-3.0-or-later


### Branch base

A new branch (derived from the archive name or `--branch`) is created from the **latest release branch** (highest leading number, e.g. `017-…`; otherwise the branch with the newest commit), not from the currently checked-out branch.

### Bytecode caches

`__pycache__/` and `*.pyc` / `*.pyo` are never synced or committed. Any that already exist in the repository are removed during sync (so they appear as a normal deletion in the next import). The project also ships a `.gitignore` for them.

### Model availability check

Before the repository is touched (no branch created, no files synced, no commits) the program sends the test prompt `hi` to the model (a tiny completion, 32 tokens max). If the server is unreachable, the model is unavailable or the answer is malformed, the run aborts with a clear error and the repository content stays as it was (a repository that did not exist yet is still initialised empty first, as before). The check also runs with `--dry-run`, because that mode still asks the model for a commit plan. On success a line such as `model OK (reply: 'Hello! How can I help?')` is shown.

### Existing branch check

If `--add` is an archive and no `--branch` is given, the branch name is derived from the archive name without its extension (`013-llm-errors.zip` → `013-llm-errors`). When a local branch with that name already exists, the import is refused with an error **before** anything is changed – no LLM call, no checkout, no sync, no commits (also with `--dry-run`). Rename the archive, or pass `--branch NAME` explicitly if you really want to add the contents to an existing branch. If an earlier import of the archive failed before committing anything, delete the leftover branch (`git branch -D NAME`) and run again. Directory sources are not affected.

### Identity formats

`--name` is parsed leniently; the program never fails because of the format and prints a short note when it had to infer something:

| Input | Author name | Author email |
|---|---|---|
| `Botter <ai@localhost>` | `Botter` | `ai@localhost` |
| `xx@yyy.zz` or `<xx@yyy.zz>` | `xx` (part before the `@`) | `xx@yyy.zz` |
| `Botter ai@localhost` (no brackets) | `Botter` | `ai@localhost` |
| `Botter <ai@localhost` (missing `>`) | `Botter` | `ai@localhost` |
| `Botter <not an email>` | `Botter` | `not.an.email@localhost` |
| `Botter` | `Botter` | `botter@localhost` |

Only an empty value is rejected. Characters Git does not allow in identities (`<`, `>`, line breaks) are removed.

### Branch output

Nothing about branches is printed by default, with two exceptions:

* **After an import that created commits** the program prints just the name of the branch the commits went to (the last line of output, e.g. `013-llm-errors`). Nothing is printed for dry-runs and "no changes" runs.
* **After an export** nothing branch-related is printed (only the `wrote …` line of the export itself).

To see all branches use `--list` (see *List mode*).

### Export wrapper directory

By default `--export` omits the `<branch>/` prefix so the archive contains the files directly at its root (`--strip`, on by default). `--no-strip` restores the previous layout with a single `<branch>/` top-level directory. Both options are only valid together with `--export`. Re-importing either layout works (the importer only strips wrappers if there are any).

**Hunk staging.** Hunk commits are staged by writing the blob and updating the index directly (not with `git add`), so a hunk that keeps the file size (e.g. a typo fix) is staged even on file systems with unreliable ctime/inode data.

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
| **Source flexibility** | `--src` can be a plain directory **or** an archive (`.zip`, `.tar`, `.tar.gz`, `.tgz`, `.tar.bz2`, `.tar.xz`, …). |
| **Archive handling** | Archives are extracted to a temporary directory; single leading directory layers are stripped automatically; the temp tree is removed afterwards. Optionally (`--mention-archive`) the archive filename can be included in every commit message (off by default). |
| **Extra-layer guard** | If a plain directory nests content one or more levels deeper than the repository root (and names overlap), the program **warns and stops**. |
| **Auto-init** | If `--git` does not exist or is not yet a repository, the directory is created and `git init` is run. |
| **LLM commit messages** | An OpenAI-compatible `/v1/chat/completions` endpoint produces a JSON plan of functionally coherent commits. |
| **Size limit** | Tries to keep each commit ≤ **200 changed lines** (insertions + deletions). A single oversized file is committed alone. |
| **Chronological dates** | Author and committer dates are taken from the newest modification time of the files in that commit (preserved from `--src` / the archive). |
| **Progress feedback** | Progress bars (via `tqdm`) for file copy (fallback path) and for the commit-creation loop; `rsync --info=progress2` when rsync is used. |
| **Branch** | `--branch NAME` checks out or creates the branch before committing. When `--src` is an archive and `--branch` is omitted, the archive basename (without extension) is used automatically. |
| **Export** | `--export [NAME]` recreates a zip/tar from a branch (default: branch with the newest commit). |
| **Dry-run** | `--dry-run` prints the planned commits without writing anything. |

---


## Requirements

### Runtime

- **Python** 3.9 or newer
- **git** on `$PATH`
- **rsync** (recommended; a pure-Python fallback with progress bar is used if missing)

### Python packages

```bash
pip install -r requirements.txt
```

| Package   | Purpose                          |
|-----------|----------------------------------|
| `requests`| HTTP client for the LLM API      |
| `tqdm`    | Progress bars                    |

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

Requires `--git`, `--url`, `--src`, and `--name`. `--model` is optional (defaults to the first/active model from `GET {url}/models`).

```bash
python ai_git_commit.py \
  --git  /path/to/the/repository \
  --url  http://localhost:8080/v1 \
  --model qwen2.5-coder \
  --src  /path/to/the/new/content_or_archive.zip \
  --name "Botter <ai@localhost>"
```

### Export mode

Requires only `--git` and `--export`. **Does not** need `--url`, `--model`, `--src`, or `--name`.

```bash
# Last archive (branch with the newest commit)
python ai_git_commit.py --git /path/to/repo --export

# Specific branch or archive name
python ai_git_commit.py --git /path/to/repo --export 013-llm-errors
python ai_git_commit.py --git /path/to/repo --export 013-llm-errors.zip

# Format and output path
python ai_git_commit.py --git /path/to/repo --export 014-clean-llm-err \
  --export-format tar.gz --export-output /tmp/out.tar.gz

# Without the top-level <branch>/ directory (files at the archive root)
python ai_git_commit.py --git /path/to/repo --export 014-clean-llm-err --strip
```

The archive is written to the **current working directory** (not into the
repository) unless `--export-output` is given. By default its content is
wrapped in a single `<branch>/` directory; `--strip` removes that directory.

### Arguments

| Flag | Import | Export | Description |
|------|:------:|:------:|-------------|
| `--git` | **yes** | **yes** | Path to the local Git repository (created on import if missing) |
| `--url` | **yes** | no | OpenAI-compatible base URL, e.g. `http://localhost:8080/v1` |
| `--model` | no | no | Model name (default: first/active model from `GET {url}/models`) |
| `--src` | **yes** | no | Source directory **or** archive (`.zip` / `.tar*` …) |
| `--name` | **yes** | no | Committer identity: `"Name <email@domain>"` |
| `--api-key` | no | no | API key (default: env `OPENAI_API_KEY`) |
| `--branch` | no | no | Branch to commit on (default: archive basename when `--src` is an archive) |
| `--mention-archive` | no | no | Include archive filename in commit messages (default: off) |
| `--dry-run` | no | no | Plan commits only; do not write |
| `--export [NAME]` | no | **yes** | Export mode: recreate archive from branch (`NAME` optional = last) |
| `--export-format` | no | no | `zip` (default), `tar`, `tar.gz`, or `tgz` |
| `--export-output` | no | no | Output path (default: `<branch>.zip` in the current directory, i.e. not inside the repository) |
| `--strip` | no | no | With `--export`: strip all directories from the archive – no top-level `<branch>/` wrapper, files are stored at the archive root |

### Example – rebuild history from old archives

```bash
# oldest snapshot first
python ai_git_commit.py --url … --model … --git ./proj --src snapshot-2020.zip \
  --name "Botter <bot@localhost>"
python ai_git_commit.py --url … --model … --git ./proj --src snapshot-2021.tar.gz \
  --name "Botter <bot@localhost>"
python ai_git_commit.py --url … --model … --git ./proj --src snapshot-2022/ \
  --name "Botter <bot@localhost>"
```

Because each commit inherits the file modification times from the source, the
resulting Git history is chronologically ordered.

---

## Functional description

### High-level flow

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  --src      │────▶│ resolve_source() │────▶│ effective_src   │
│ (dir/arch)  │     │ extract + strip  │     │ (plain dir)     │
└─────────────┘     └──────────────────┘     └────────┬────────┘
                                                      │
                                                      ▼
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  --git      │────▶│ ensure_git_repo  │────▶│ working tree    │
│ (path)      │     │ (init if needed) │     │                 │
└─────────────┘     └──────────────────┘     └────────┬────────┘
                                                      │
                      detect_extra_directory_layer()  │
                      (abort if nested wrapper)       │
                                                      ▼
                                             sync_source_to_repo()
                                             (rsync or copy+progress)
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
                                              commit with progress)
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

- Prefer `rsync -a --delete --exclude .git --info=progress2`.
- Fallback: pure-Python walk + `shutil.copy2` (preserves mtimes) with a
  `tqdm` progress bar, then removal of obsolete files (also with progress).

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
- Commit messages are concise but between 10 and 100 words long (enforced in code: overlong messages are truncated, too-short ones are extended with the affected file names).
- Every changed path appears in exactly one commit.
- Leftover paths (if the model omits any) are placed in a final catch-all commit.

### 7. Applying commits

For each planned commit:

1. Stage only the listed paths (`git add` / `git rm`).
2. Compute the newest `st_mtime` among the surviving files.
3. Set `GIT_AUTHOR_DATE` and `GIT_COMMITTER_DATE` to that timestamp (UTC).
4. Set author/committer name and email from `--name`.
5. `git commit -m …`.

A `tqdm` progress bar tracks overall commit creation.

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
dependency. HTTP is handled solely by `requests`. Progress UI is provided by
`tqdm` (and by rsync itself when available).

---

## Limitations

- Small files are grouped at **file level**. A single file whose diff exceeds
  200 lines is split into **functional hunks** by the model; those hunks are
  applied sequentially as separate commits.
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

### Export without wrapper directory

`--export … --strip` omits the `<branch>/` prefix so the archive contains the files directly at its root. It is only valid together with `--export`. Re-importing such an archive works as before (the importer only strips wrappers if there are any).

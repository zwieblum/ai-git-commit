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
| **Archive handling** | Archives are extracted to a temporary directory; single leading directory layers are stripped automatically; the temp tree is removed afterwards. |
| **Extra-layer guard** | If a plain directory nests content one or more levels deeper than the repository root (and names overlap), the program **warns and stops**. |
| **Auto-init** | If `--git` does not exist or is not yet a repository, the directory is created and `git init` is run. |
| **LLM commit messages** | An OpenAI-compatible `/v1/chat/completions` endpoint produces a JSON plan of functionally coherent commits. |
| **Size limit** | Tries to keep each commit ≤ **200 changed lines** (insertions + deletions). A single oversized file is committed alone. |
| **Chronological dates** | Author and committer dates are taken from the newest modification time of the files in that commit (preserved from `--src` / the archive). |
| **Progress feedback** | Progress bars (via `tqdm`) for file copy (fallback path) and for the commit-creation loop; `rsync --info=progress2` when rsync is used. |
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

```bash
python ai_git_commit.py \
  --url  http://llamaserver:8080/v1 \
  --model qwen3.5-coder \
  --git  /path/to/the/repository \
  --src  /path/to/the/new/content \
  --name "Botter <the-great-ai@localhost.at>"
```

Optional:

- `--api-key …` or environment variable `OPENAI_API_KEY` (many local servers ignore it)
- `--dry-run` – print the planned commits only

## How it works

1. Ensures `--git` is a Git working tree. If the path does not exist (or is not yet a repository), the directory is created and `git init` is run automatically. Also refuses to proceed when something is already staged.
2. Resolves `--src`: if it is an archive, extract to a temp dir and strip leading single-directory wrappers; if it is a plain directory that has extra nested layers matching the repo layout, abort with a clear warning.
3. Syncs every file from the effective source into the repo root (`.git` is never touched).
4. Collects a list of changed paths with `git diff --numstat` and `git status --porcelain`.
5. Sends a compact summary to the model and asks for a JSON list of commits:
   ```json
   [
     {"message": "feat: add X", "files": ["src/a.py", "src/b.py"]},
     {"message": "fix: handle edge case", "files": ["src/c.py"]}
   ]
   ```
6. For each planned commit, stages only the listed paths and commits with the
   supplied author/committer identity.

## Limitations

- Grouping is **file-level**. A single file whose diff exceeds 200 lines cannot be
  split into smaller hunks automatically.
- The model must return valid JSON; the script extracts the first JSON array it finds.
- Deletions and renames are supported, but complex rename detection relies on Git’s
  own status output.

## License

MIT

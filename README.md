# AI Git Committer

Sync a non-Git source directory into a local Git repository and create one or more
commits whose messages are written by an LLM (OpenAI-compatible API).

## Features

- Copies / syncs `--src` into the working tree of `--git` (rsync preferred, with `--delete`).
- Detects added, modified and deleted files together with insertion/deletion counts.
- Asks the model to group changes into **functionally coherent** commits.
- Tries to keep the total changed lines (insertions + deletions) of each commit
  **≤ 200**. A single file that exceeds the limit is committed alone.
- Sets both author and committer from the supplied `--name` identity.
- Optional `--dry-run` to preview the plan without writing.

## Requirements

- Python 3.9+
- `requests`
- `git` on `$PATH`
- `rsync` recommended (falls back to a pure-Python copy if missing)

```bash
pip install requests
```

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

1. Verifies that `--git` is a Git working tree and that nothing is currently staged.
2. Syncs every file from `--src` into the repo root (`.git` is never touched).
3. Collects a list of changed paths with `git diff --numstat` and `git status --porcelain`.
4. Sends a compact summary to the model and asks for a JSON list of commits:
   ```json
   [
     {"message": "feat: add X", "files": ["src/a.py", "src/b.py"]},
     {"message": "fix: handle edge case", "files": ["src/c.py"]}
   ]
   ```
5. For each planned commit, stages only the listed paths and commits with the
   supplied author/committer identity.

## Limitations

- Grouping is **file-level**. A single file whose diff exceeds 200 lines cannot be
  split into smaller hunks automatically.
- The model must return valid JSON; the script extracts the first JSON array it finds.
- Deletions and renames are supported, but complex rename detection relies on Git’s
  own status output.

## License

MIT

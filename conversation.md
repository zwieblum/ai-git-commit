# Conversation

**User:**

i need a program that creates commits the differences from a new local directory to a local git repository. it should take a openapi style URL (e.g. --url http://llamaserver:8080/v1), a model name ( e.g. --model qwen3.5-coder), the git repository (e.g. --git here/is/the/repository), directory of the newest (non-git) local directory (e.g. --src her/is/the/new/content), the comitters email (e.g. --name "Botter <the-great-ai@localhost.at>"). the model should write the description for the git commits. code blocks in one commit should be at most 200 lines, bigger changes need to be broken up into functionally coherent blocks.

**Assistant:**

Created a Python program `ai_git_commit.py` that:

- Accepts the required CLI flags (`--url`, `--model`, `--git`, `--src`, `--name`) plus optional `--api-key` and `--dry-run`.
- Syncs the source directory into the Git working tree (rsync with `--delete`, excluding `.git`).
- Collects changed files with line counts via `git diff --numstat` and `git status --porcelain`.
- Calls an OpenAI-compatible `/v1/chat/completions` endpoint so the model produces a JSON plan of functionally coherent commits.
- Tries to keep each commit ≤ 200 changed lines (file-level grouping; a single oversized file is committed alone).
- Stages only the files belonging to each planned commit and commits them with the supplied author/committer identity.

Also includes a README and this conversation log.

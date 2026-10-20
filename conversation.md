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

**User:**

give me a clickable downloadlink

**Assistant:**

Provided a clickable download for `001-ai-git-commit.zip`.

**User:**

if the git repository does not exist, it should be created

**Assistant:**

Updated `ensure_git_repo` so that:

- If the path does not exist → create the directory and run `git init`.
- If the path exists but is not a Git repository → run `git init` in place.
- `collect_changes` now handles brand-new repositories that have no `HEAD` yet (avoids failing on `git diff HEAD`).

**User:**

if the new source moved the content down one directory (e.g.: git repository: program.py, in the new source: somedirectory/program.py - simillar for all other files/dirs) then the program should warn the user about the additional directory layers and stop. 

if --src points to an archive (*.zip/*.tar/*.tar,gz ...), then the archive should be unpacked to a temporary directory, preceeding directories sripped , then merged to the git as intended, and then removed.

**Assistant:**

Added two features:

1. **Extra directory layer detection** – if a plain `--src` directory wraps the content in one or more extra top-level directories that overlap with the repository root, the program prints a clear warning and exits without syncing.
2. **Archive support** – `--src` may be a `.zip`, `.tar`, `.tar.gz`, `.tgz`, `.tar.bz2`, `.tar.xz`, etc. The archive is extracted to a temporary directory, any single leading directory layers are stripped, the content is synced as usual, and the temporary tree is removed afterwards.

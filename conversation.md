# Conversation

**User:**

i need a program that creates commits the differences from a new local directory to a local git repository. it should take a openapi style URL (e.g. --url http://llamaserver:8080/v1), a model name ( e.g. --model qwen3.5-coder), the git repository (e.g. --git here/is/the/repository), directory of the newest (non-git) local directory (e.g. --src her/is/the/new/content), the comitters email (e.g. --name "Botter <the-great-ai@localhost.at>"). the model should write the description for the git commits. code blocks in one commit should be at most 200 lines, bigger changes need to be broken up into functionally coherent blocks.

**Assistant:**

Created a Python program `ai_git_commit.py` with CLI flags, LLM-generated commit messages, file-level grouping ≤ 200 lines, rsync sync, etc.

**User:**

give me a clickable downloadlink

**Assistant:**

Provided download for `001-ai-git-commit.zip`.

**User:**

if the git repository does not exist, it should be created

**Assistant:**

Auto-create directory + `git init` when missing; handle brand-new repos without HEAD.

**User:**

if the new source moved the content down one directory … warn and stop.
if --src points to an archive … unpack, strip preceding dirs, merge, remove temp.

**Assistant:**

Extra-directory-layer detection (warn + exit). Archive support with automatic strip and cleanup.

**User:**

the git commits must represent the file date from the file in "--src" …

**Assistant:**

Commit author/committer dates taken from the newest source-file mtime in each commit.

**User:**

Add a progress bar to the script

**Assistant:**

Progress via `tqdm` for pure-Python file copy and for the commit loop; `rsync --info=progress2` when rsync is available.

**User:**

add a README.md to the archive. it should also contain an extensive functional description, architecture, requrements etc.

**Assistant:**

Expanded README with features table, requirements, installation, usage, full functional description, architecture diagram, limitations. Progress bars included. Packaged as `005-progress-readme.zip`.

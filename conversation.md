# Conversation

**User:**

i need a program that creates commits the differences from a new local directory to a local git repository. it should take a openapi style URL (e.g. --url http://llamaserver:8080/v1), a model name ( e.g. --model qwen3.5-coder), the git repository (e.g. --git here/is/the/repository), directory of the newest (non-git) local directory (e.g. --src her/is/the/new/content), the comitters email (e.g. --name "Botter <the-great-ai@localhost.at>"). the model should write the description for the git commits. code blocks in one commit should be at most 200 lines, bigger changes need to be broken up into functionally coherent blocks.

**Assistant:**

Created initial program with LLM commit messages, ≤200-line grouping, rsync sync, etc.

**User:**

give me a clickable downloadlink

**Assistant:**

Provided download for `001-ai-git-commit.zip`.

**User:**

if the git repository does not exist, it should be created

**Assistant:**

Auto-create + `git init` when missing; handle repos without HEAD.

**User:**

if the new source moved the content down one directory … warn and stop.
if --src points to an archive … unpack, strip, merge, remove temp.

**Assistant:**

Extra-directory-layer detection; archive support with strip and cleanup.

**User:**

the git commits must represent the file date from the file in "--src" …

**Assistant:**

Commit dates taken from newest source-file mtime in each commit.

**User:**

Add a progress bar to the script

**Assistant:**

Progress via `tqdm` and `rsync --info=progress2`.

**User:**

add a README.md to the archive. it should also contain an extensive functional description, architecture, requrements etc.

**Assistant:**

Expanded README; packaged as `005-progress-readme.zip`.

**User:**

change the license to GPL3.
add a copyright "© Dr. Nikolaus Klepp - office@klepp.biz - https://klepp.biz"

**Assistant:**

License switched to GPL-3.0-or-later. Added copyright notice to the script header, README, and a full `LICENSE` file.

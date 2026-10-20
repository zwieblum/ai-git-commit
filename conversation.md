# Conversation

**User:**

i need a program that creates commits the differences from a new local directory to a local git repository. … model should write the description … code blocks in one commit at most 200 lines …

**Assistant:**

Created initial program (LLM messages, ≤200-line grouping, rsync, etc.).

**User:**

give me a clickable downloadlink

**Assistant:**

Provided `001-ai-git-commit.zip`.

**User:**

if the git repository does not exist, it should be created

**Assistant:**

Auto `git init` when missing.

**User:**

extra directory layer → warn and stop; archives → unpack, strip, merge, cleanup

**Assistant:**

Implemented.

**User:**

commits must use file dates from --src for chronological archive history

**Assistant:**

Author/committer dates from newest source mtime per commit.

**User:**

Add a progress bar

**Assistant:**

tqdm + rsync --info=progress2.

**User:**

extensive README

**Assistant:**

Expanded README; `005-progress-readme.zip`.

**User:**

change the license to GPL3 + copyright Dr. Nikolaus Klepp …

**Assistant:**

GPL-3.0-or-later + copyright; `006-gpl3-copyright.zip`.

**User:**

add the gpl-3.0 license file

**Assistant:**

Already present as `LICENSE` in the archive.

**User:**

one big file with a lot of changes: make the model plan the splitting in functional hunks

**Assistant:**

Large files (>200 changed lines) are now analysed as unified-diff hunks. The model returns groups of hunk indices with commit messages; the program applies those hunk groups sequentially as separate commits.

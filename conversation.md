# Conversation

**User:**

… (feature history) …

**User:**

make adding the archive name to the messages optional, off by default

**Assistant:**

`--mention-archive` flag (off by default).

**User:**

[bug report: git add exit 128 when committing large new file after other commits]

**Assistant:**

Fixed: `git diff HEAD` does not show untracked files, so after earlier commits the large new file produced an empty diff, the worktree file was removed, and `git add` failed. `_get_file_diff` now synthesises a new-file diff for paths not in HEAD. New files are staged as full final content (partial hunk apply only for files that already exist in HEAD).

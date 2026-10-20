# Conversation

**User:**

add a function that recreates the last archive from the repository. optionally it should be ably to recreate any given archive.

**Assistant:**

Added `--export [NAME]`:
- `--export` alone → archive the branch with the newest commit (last import)
- `--export NAME` → branch name or archive basename (with/without extension)
- `--export-format zip|tar|tar.gz` and `--export-output PATH`
Uses `git archive` with a top-level prefix matching the branch name.

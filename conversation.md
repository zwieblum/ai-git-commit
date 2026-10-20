# Conversation

**User:**

… (full feature history: LLM commits, auto-init, archives, extra-dir guard, file dates, progress, README, GPL-3, hunk split, --branch) …

**User:**

when an archive is given then automatically use the archives name (without extension) as branch name. --branch overrides this.

**Assistant:**

When `--src` is an archive and `--branch` is not given, the branch name is derived from the archive basename with the extension stripped (e.g. `snapshot-2020.zip` → branch `snapshot-2020`). Explicit `--branch` always wins.

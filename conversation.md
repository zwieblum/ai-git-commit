# Conversation

**User:**

* "--export" is not in the code - fix that. also "--export" does not require "--uri" "--model" "--src" "--name"
* if "--model" is omitted, then it should use the active model from the server.

**Assistant:**

--export was already in the tree; help/README clarified again. Export mode only requires --git and --export. --model is optional on import: when omitted, GET {url}/models is used and the first/active model is selected.

---

**User:**

* read the code. make it so that the commit messages are concise but at least 10 words up to 100 words long.

**Assistant:**

Added `MIN_MESSAGE_WORDS = 10` / `MAX_MESSAGE_WORDS = 100` and a `normalize_message()` helper in `ai_git_commit.py`. Both LLM prompts (file-level and hunk-level planning) now require concise messages of 10–100 words. In code, every planned commit message is normalized after planning: overlong messages are truncated at the word limit (line structure kept), too-short ones (including the fallback "chore: remaining…" / "update <file>" messages) are extended with the affected file names. The `--mention-archive` tag is reserved so the final message stays within 100 words. README updated.

---

**User:**

* bug: deletes source archive (output: "Syncing <archive>.zip → repo … Source directory does not exist: <archive>.zip"; the file still exists afterwards)

**Assistant:**

Could not reproduce: `resolve_source()` in this code extracts the archive and yields the extracted directory, so `sync_source_to_repo()` never receives the .zip path. The archive is not deleted (the error text is misleading). The traceback implies the running script yields the archive path itself, i.e. it is probably a different/older copy. Hardening added: refuse a --src located inside the --git repo (rsync --delete would remove it), and abort with a clear message if the resolved source is not a directory.

---

**User:**

* the commits happen against the very first release, not against the last release. fix it (screenshot: branch 018 forks from 001-ai-git-commit)

**Assistant:**

Cause: `ensure_branch()` ran `git checkout -b NEW` from whatever HEAD was checked out (here the first release), so the diff and history were relative to 001. New branches are now created from the latest release branch (`latest_release_branch()`: highest leading number such as 017-…, else newest commit date). Tested: starting on 001-a, new branch 003-c is created from 002-b. README updated.

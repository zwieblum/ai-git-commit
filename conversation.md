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

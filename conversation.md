# Conversation

**User:**

program crashes when URL is unreachable or model is unavailable

**Assistant:**

Added error handling in call_llm (013).

**User:**

[still saw full traceback – was running old script; --src only updates the git repo, not the executable]

**Assistant:**

Error path now prints a short message to stderr and calls sys.exit(1) with no exception chain. Replace the script from this archive (do not only import the zip as --src).

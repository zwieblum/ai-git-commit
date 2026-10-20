# Conversation

**User:**

… (feature history, bugfixes) …

**User:**

program crashes, when URL is unreachabe or model is unavailable

**Assistant:**

`call_llm` now catches connection errors, timeouts, HTTP errors, non-JSON bodies, and API error payloads. The program exits with a clear message instead of an unhandled traceback.

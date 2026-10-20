# Conversation

**User:**

… (history of features) …

**User:**

Add the archive name to the commit message

**Assistant:**

When `--src` is an archive, every generated commit message includes the archive filename (e.g. `feat: add parser (from snapshot-2020.zip)`). The model is instructed to mention it; a fallback appends `(from <archive>)` if it did not.

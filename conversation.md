# Conversation

**User:**

i need a program that creates commits the differences from a new local directory to a local git repository. … model writes commit messages … ≤200 lines per commit …

**Assistant:**

Created the program and iterated through features: auto-init, archives, extra-dir guard, file dates, progress bars, extensive README, GPL-3 + copyright, functional hunk splitting for large files.

**User:**

add an option to mark a branch

**Assistant:**

Added `--branch NAME`: checks out the branch if it exists, or creates it (including on an empty repo so the first commit lands on that branch). All subsequent commits are made on that branch.

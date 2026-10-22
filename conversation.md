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

---

**User:**

* remove __pycache__ and never add it again

**Assistant:**

Removed `__pycache__` from the project and added a `.gitignore` (`__pycache__/`, `*.py[cod]`). In `ai_git_commit.py`, sync now excludes `__pycache__`/`*.pyc`/`*.pyo` (rsync and fallback copy), deletes any already present in the repo (tracked ones become a normal deletion commit), and `collect_changes()` ignores new bytecode files. Tested with the fallback copy path; rsync is not installed in the test environment, so the rsync flags were not run.

---

**User:**

* the exported archive is created in the current working directory, not inside the repository.
* add an option "--strip" that when it's given strips the archive of all trailing directories.

**Assistant:**

Cause of the first problem: `export_archive()` passed a relative `--output` to `git -C <repo> archive`, which resolves relative paths against the repository, so the default `<branch>.zip` was written inside the repo. The default (and any relative path) is now resolved against the current working directory, so the archive is created there, not in the repository.

Added `--strip` (export mode only; error if used without `--export`). It omits the top-level `<branch>/` directory, so the archive holds the files directly at its root (works for zip, tar, tar.gz/tgz). I read "trailing directories" as the wrapper directory around the files (the leading path component). Without `--strip` the behaviour is unchanged (`<branch>/…`). Tested with zip, tar and tar.gz, with and without `--strip`, with `--export-output` pointing at a directory, and the `--strip`-without-`--export` error; tqdm was stubbed in the sandbox. README and `--help` epilog updated.

---

**User:**

make "--strip" the default, add "--no-strip"

**Assistant:**

`--strip` is now the default for `--export`: the archive holds the files directly at its root. Added `--no-strip` to get the previous layout with a single top-level `<branch>/` directory. Implemented as a `--strip/--no-strip` pair (`argparse.BooleanOptionalAction`, default unset → strip on); either flag is still rejected without `--export`. `export_archive(strip=…)` now defaults to `True`. Tested zip and tar.gz with no flag, `--strip` and `--no-strip`, plus the error case. README and `--help` examples updated. (Side finding in the previous round: output paths are resolved against the cwd, fixed in 021.)

---

**User:**

using llama-server, what settings would be the best settings for model "qwen2.5-coder-7b-instruct-q4_k_m" on a RTX4060 (8GB) ?

**Assistant:** Suggested `-ngl 99 -c 32768 -np 1 -fa on -ctk q8_0 -ctv q8_0 -b 2048 -ub 512 --jinja` with Qwen's recommended sampling (temp 0.7, top-k 20, top-p 0.8, repeat penalty 1.05).

---

**User:**

can these settings live inside a "model.ini"?

**Assistant:** Yes, via `llama-server --models-preset model.ini --models-max 1` (router mode): one section per model, section name = model id, keys = long CLI flags without dashes, absolute paths only, `[*]` for shared defaults, restart to reload.

---

**User:**

when the program has finshed successfully, give a list of all branches like "git branch --all"

**Assistant:**

After every successful run (import, dry-run, "no changes detected", export) the program now prints `git branch --all` output (`print_branches()`), under the heading "Branches (git branch --all):". The old `main()` body became `_run()`, which returns the repo path on each successful exit; `main()` prints the list afterwards. Runs that fail via `SystemExit`/exceptions don't print it. Tested against a mock OpenAI-compatible server: import, dry-run, identical re-import, export, and the error path. README updated.

---

**User:**

when an email is not given in the form "NAME <xx qyyy>", then the program failes. make it handle emails gracefully, e.g. xx@yyy.zz

**Assistant:**

`parse_author()` used to raise `ValueError` (traceback) unless `--name` was exactly `Name <email>`. It is now lenient: `xx@yyy.zz` / `<xx@yyy.zz>` → name `xx`, email `xx@yyy.zz`; `Name xx@yyy.zz` (no brackets), `Name <xx@yyy.zz` (missing `>`), `Name <not an email>` (→ `not.an.email@localhost`) and a bare `Name` (→ `name@localhost`) also work. A one-line note shows how the value was interpreted; only an empty `--name` is an error (clean message, no traceback). Verified with a table of inputs and an end-to-end import with `--name xx@yyy.zz` (git author/committer = `xx <xx@yyy.zz>`). README ("Identity formats") and `--help` updated.

---

**User:**

if an --src points to an archive and a branche with that name (witout extension) already exists, do not add it to the repository but give an error message

**Assistant:**

In import mode, when `--src` is an archive and no `--branch` is given, the derived branch name (archive name without extension, e.g. `013-llm-errors.zip` → `013-llm-errors`) is checked against the local branches (`branch_exists()`, `git show-ref`). If it exists, the program exits with an error before anything else happens (no LLM call, no checkout, no sync, no commits; also with `--dry-run`), e.g. `Error: branch '001-first' already exists in repo – refusing to import archive '001-first.zip' again.` An explicit `--branch NAME` still lets you add to an existing branch, and directory sources are unaffected. Note: a failed import that already created the branch (e.g. LLM server unreachable) leaves that branch behind, so a retry of the same archive is now refused – the error message says to remove it with `git branch -D NAME` (or use `--branch`). Tested with zip and tar.gz of the same name, `--dry-run`, explicit `--branch`, and a new name. README ("Existing branch check") updated.

---

**User:**

the list of bransches shoulfd only be printed when the user asks for it ("--list", ony needs "--git"). After adding to the repository only print the last branch name. After export print nothing.

**Assistant:**

Removed the automatic `git branch --all` output. New `--list` option (needs only `--git`, never creates the repo, cannot be combined with `--export`/`--src`) prints exactly the `git branch --all` output (`No branches found.` for an empty repo). After an import that created commits, only the name of the branch the commits went to is printed, as the very last line (after the temporary-directory cleanup message); dry-runs and "no changes" runs print nothing branch-related. Export prints no branch information (only its own `Exporting …` / `wrote …` lines, which were there before; say if you want those silenced too). Also factored the repo validation into `require_git_repo()`. Tested: import (zip and directory), dry-run, no-changes, export, `--list` (normal, missing path, non-repo, empty repo, with `--export`). README ("List mode", "Branch output") and `--help` updated.

---

**User:**

if only "--git" is given, then "--list" is assumed.

**Assistant:**

When `--git` is the only option on the command line (every other option still at its argparse default, so `--git repo` and `--git=repo` both count), `--list` is now assumed. Any additional option (`--dry-run`, `--export`, `--strip`, …) keeps the previous behaviour, e.g. `--git repo --dry-run` still reports the missing import options. A missing/non-repo path still gives the usual error. Tested all of these; README and `--help` updated.

---

**User:**

--list: imply this as default, if neither --export nor --src is given.
rename "--src" to "--add"

**Assistant:**

Renamed `--src` to `--add` everywhere (option, `args.add`, error/help texts, README); the old name is no longer accepted. List mode is now the default whenever neither `--export` nor `--add` is given (replacing the earlier "only `--git`" rule), so `--git repo --name …` or `--git repo --dry-run` also just lists; import-only options given in that case are ignored with a note on stderr (`Note: --name, --dry-run ignored – listing branches (use --add ARCHIVE_OR_DIR to import).`). `--list` together with `--add`/`--export` is still an error. Tested: import with `--add`, `--src` rejected, default list, ignored-options note, missing import options, export. `--add` shows `PATH` as metavar in `--help`.

---

**User:**

check model availability befor committing. Do this by sending "hi" as a test pompt.

**Assistant:**

New `check_model()`: after the cheap checks (staged changes, branch-exists) and **before** branch creation, sync and commits, the program sends the test prompt `hi` (system prompt "You are a helpful assistant.", max 32 tokens) through the existing `call_llm()` and prints `Checking model '…' with a test prompt …` / `model OK (reply: '…')`. Unreachable server, HTTP errors (404/503, "model not found"), a malformed answer or a timeout abort with the usual descriptive error before anything is changed (also with `--dry-run`, since it still queries the model). `call_llm()` got `max_tokens` and `allow_empty` parameters; a null/empty completion counts as OK for the check, so reasoning models that spend the small budget on thinking are not rejected. Tested against mock servers: OK, 404, malformed JSON, null content, server down, dry-run; in all failure cases no branch was created. A repository that does not exist yet is still initialised (empty) before the check, as before. README ("Model availability check") updated.

---

**User:**

add a "--revert" option, that removes the last branch from the repository. it must not have "--export" not "--add"

**Assistant:**

New `--revert` mode (needs only `--git`; combining it with `--add`, `--export` or `--list` is an error, and it takes precedence over the implied list default). It removes the "last" branch – chosen with the existing `latest_release_branch()` rule: highest leading number, else newest tip (`exclude=` parameter added so the previous branch can be picked as fallback). If that branch is checked out, the previous branch is checked out first, then the branch is deleted with `git branch -D`. Output: `Removed branch 'X' (was <sha12>); now on 'Y'.` plus a `Restore with: git -C repo branch X <full sha>` line. Safety: refuses to remove the only branch, and refuses when the working tree has uncommitted/untracked changes (message explains how to discard them, e.g. after a failed import). Never creates the repo; import-only options are ignored with a note (shared `note_ignored()` helper). Tested: revert while on the last branch / another branch, dirty tree, single branch, empty repo, option combinations, non-repo path, restore hint, branches without numeric prefix. README ("Revert mode"), `--help` description and examples updated.

---

**User:**

splitting in hunks does not work when done by the model. change it so that the model creates a plan for splitting, but splitting is done by the program. iterative process per hunk: take the file from the repository, change one hunk, commit it, repeat until all changes are done. when a commit fails, then revert the previous commits.

**Assistant:**

The model now only returns a plan (`[{"hunk": N, "message": "..."}]`: order + one message per hunk). The program does the splitting: `apply_commits()` takes the file from HEAD (`git show`), applies exactly one hunk with its own `apply_hunk()` (offset-tracking, expected lines verified; no `git apply`), stages and commits, and repeats until all hunks are done; the final file must equal the synced source. Any failed commit (or hunk mismatch/Ctrl-C) reverts all commits of the run (`_rollback()`: HEAD reset to its start, synced files restored uncommitted) and exits with an error. Before planning, all hunks are dry-run against HEAD; if they don't reproduce the file it is committed whole. Model output is repaired (bad/duplicate indices dropped, forgotten hunks appended with generic messages, unparsable answer → generic plan). Commit dates now come from the source mtimes captured before any rewrite. Tested with a real repo: sequential and shuffled hunk order, forced commit failure at commit 3 of 5 (2 commits reverted, working tree restored), mock model with bad indices; tqdm stubbed (not installable in the sandbox). Not covered: a new file is a single hunk, so it is still one commit. README updated.

---

**User:**

A branch created for the import should be deleted after a rollback

**Assistant:**

`ensure_branch()` now returns whether it created the branch. If the run ends without a commit, the new branch is deleted (`discard_created_branch()`): previous branch checked out again (`-f` after a real sync), synced untracked files removed (`git clean`, only after a real sync), `git branch -D`. This covers the rollback after a failed commit and also any other error before committing, `--dry-run` and "no changes" (they used to leave the branch behind). Existing branches (`--branch NAME`) are never deleted; successful imports are untouched. Tested with a mock model and a commit-msg hook rejecting the 4th commit of 6: 3 commits reverted, branch `002-b` deleted, back on `001-a`, clean tree; retry without hook succeeded; dry-run on a new branch left no branch. README updated.

---

**User:**

remove the progress bar; rename "--git" to "--repo"; when no repository is given, assume "repository" as the name

**Assistant:**

Progress bars are gone: `tqdm` import, all bars and `tqdm.write` (now plain `print`) removed, `tqdm` dropped from `requirements.txt`, and rsync no longer gets `--info=progress2`. `--git` was renamed to `--repo` everywhere (option, `args.repo`, help, README); the old name is rejected. `--repo` is no longer required: without it `./repository` is used (so `ai_git_commit.py --list` lists `./repository`, an import creates it). Tested with a mock model: import into the default `repository`, `--repo` explicit, `--git` rejected, list/revert/export with the default.

---

**User:**

is there a way to clean up a repository? e.g. delete all removed commits?

**Assistant:**

Yes: deleted branches leave unreachable commits (kept by the reflog and object store). `git reflog expire --expire=now --all && git gc --prune=now` removes them for good (irreversible, only local). Offered a `--cleanup` mode.

---

**User:**

add the --cleanup without confirmation

**Assistant:**

New `--cleanup` mode (`cleanup_repository()`): counts unreachable commits/objects (`git fsck --unreachable --no-reflogs`), runs `git reflog expire --expire=now --all` and `git gc --prune=now -q` without any confirmation, and prints the number removed plus the repository size before/after. Needs only `--repo` (default `./repository`), never creates the repository, cannot be combined with `--add`/`--export`/`--revert`/`--list`; import-only options are ignored with a note. README (new "Cleanup mode", argument table) and `--help` updated. Tested: after `--revert` the deleted commit exists until `--cleanup` and is gone afterwards, branches intact, size shrinks; error cases and missing repo.

---

**User:**

is there an option to add a long description to a git branch?

**Assistant:**

Yes: `git branch --edit-description` / `git config branch.<name>.description` (local config only, not shown by `git branch`, not pushed). Alternatives: annotated tag, committed notes file. Offered `--describe`.

---

**User:**

when "conversation.md" (prioritized) or something similar (README ...) is present, then create a short summary of the differences/progression from the last version to the current version as the description as an annotated tag on the branch tip. also add "--describe TEXT" to override this.

**Assistant:**

After a successful import the branch tip gets an annotated tag `desc/<branch>`. Message: `--describe TEXT` if given; otherwise, if the repository root holds `conversation.*` (first), `README*`, `CHANGELOG*`, `CHANGES*`, `HISTORY*` or `NOTES*`, a model-written summary (subject + body, ≤120 words) of the progression since the previous tip, built from the diff of that document (full text if new/unchanged) plus commit messages and file stat. No document and no `--describe` → no tag. Tag names are namespaced (`desc/…`) so they never clash with branch names; re-import into the same branch replaces the tag; failures only warn; `--dry-run` creates nothing. `--revert` now deletes the branch's tag too (it would keep the commits reachable). I read your option as `--describe`. Tested with a mock model: conversation.md diff reaches the prompt, README fallback, override without model call, no-doc case, dry-run, revert removing the tag. README updated.

---

**User:**

when "--add" is missing but a stray filename/directory is given, then imply "--add" on that

**Assistant:**

Added an optional positional `PATH` (parsed with `parse_intermixed_args`, so it works before, between and after options). Without `--add` it becomes `--add PATH` (note on stderr). Errors: `--add X` plus a stray path ("two sources given"), and a stray path with `--export`/`--revert`/`--cleanup`/`--list` (for `--export`, a word right after it is still the branch name). `--help` example and README ("Omitting --add") added. Tested: path before/after/between options, both given, `--export`/`--revert` combinations, no positional → list.

---

**User:**

"--list" should print the short description of the branch, too

**Assistant:**

`--list` still prints the `git branch --all` lines, but a local branch with a `desc/<branch>` tag now gets the subject line of the tag message appended (aligned in a column; remote/other lines unchanged, branches without a tag unchanged). Descriptions are read in one `git for-each-ref` call. README updated. Tested with described, undescribed, slash-named and remote branches.

---

**User:**

remove the "Subject:" prefix (--list showed `**Subject:** …` / `**Subject: …**`)

**Assistant:**

New `clean_subject()` strips markdown emphasis (`*`, backticks, `#`) and a leading `Subject:` / `Title:` label (case-insensitive) from the first line. It is applied when a description is generated (so new tags are clean), and when `--list` reads existing tags (so tags created by earlier versions display correctly without being rewritten). The summary prompt also tells the model not to use a prefix or markdown. Tested with both shown variants plus plain, `Title:`, lowercase and underscore-containing subjects.

---

**User:**

make the short description more detailed (~ 50 words)

**Assistant:**

The description tag message is now `short description` (one line, ~50 words: the prompt asks for 40–60, hard cut at 70) + blank line + optional body (whole text capped at 170 words). `--list` shows the short description (first paragraph of the tag, joined to one line) – so existing tags with a one-line subject keep working. `normalize_description()` also handles models that skip the blank line (first line = short description), wrap the short description over several lines, or add `Subject:`/markdown. README updated. Tested normalisation cases and an import with a mock model returning a ~50-word description.

---

**User:**

use only the differences in "conversation.md" as the source of the description.

**Assistant:**

`summarize_progress()` now sends the model nothing but `git diff -U0 <previous tip> HEAD -- <doc>` (for a first import: against the empty tree, i.e. the whole document as additions). The commit messages, the changed-file stat and the "full text if unchanged" fallback are gone; the prompt says to use only these changes. If the document did not change, no tag is created and a note says so (`--describe TEXT` still works). Applies to whichever description document was found (conversation.* first, then README… etc.). README updated. Tested: prompt content for v2 (only the added conversation lines), first import, unchanged document, README fallback.

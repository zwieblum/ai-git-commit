# Conversation

**User:**

ensure that short help, long help and readme are in sync. "--export" does not require --uri --model --src --name

**Assistant:**

Aligned `-h` / `--help` (description, argument groups, epilog examples) with the README. Export mode needs only `--git` and `--export`; import mode needs `--git --url --model --src --name`.

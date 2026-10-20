# Conversation

**User:**

* "--export" is not in the code - fix that. also "--export" does not require "--uri" "--model" "--src" "--name"
* if "--model" is omitted, then it should use the active model from the server.

**Assistant:**

--export was already in the tree; help/README clarified again. Export mode only requires --git and --export. --model is optional on import: when omitted, GET {url}/models is used and the first/active model is selected.

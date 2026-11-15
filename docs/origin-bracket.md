# The origin bracket

```
[sent by Grok Bot]
[sent by Grok Bot · Scout]          ← when a bot name is configured (bot_name / CLAUDE_PROJECTS_BOT_NAME) or passed per call
```

- `claude_thread_send` and `claude_thread_create` put it on the **first line** of every message they type. Text a person types in the app has no bracket.
- It's one constant, `ORIGIN_PREFIX = '[sent by Grok Bot'` in [`claude_projects/origin.py`](../claude_projects/origin.py), so you can grep for it: `grep -n '^\[sent by Grok Bot'`.
- It's idempotent: a message that already starts with a header never gets a second one.
- Read results flag every user message with `sent_by_bot`. The MCP tool descriptions tell the orchestrating agent how to treat bracketed messages.
- `claude_thread_create` asks the Project to keep the bracket at the top of the child thread's task, so child threads know too.
- [AGENTS.md](../AGENTS.md) is a short note for coding agents. Copy it into a repo's `AGENTS.md` (the shared standard), and for Claude Code, which reads `CLAUDE.md`, also add a `CLAUDE.md` with the single line `@AGENTS.md`, as this repo does. `install.sh --claude-md` adds the note to `~/.claude/CLAUDE.md` for local Claude Code sessions.

The bracket is a label, not a security boundary. Anyone who can type in a thread can type it.

# Security

This server acts with the full authority of the Claude account signed in to the app. It can send messages that
spend usage, create projects and change cloud environment variables. Treat access to it like access to that account.

- **Keep it private.** It binds `127.0.0.1` by default with a random bearer token (`~/.config/claude-projects-mcp/mcp.token`, chmod 600).
  DNS-rebinding protection is on. Add a tunnel hostname to `allowed_hosts` rather than disabling it. A bearer token is
  all-or-nothing, not a per-tool permission model.
- **The viewport is the account.** x11vnc listens on localhost only and needs a password. noVNC binds `127.0.0.1` unless you pass
  `--viewport-bind`. Reach it with an SSH tunnel.
- **Local data.** ccp reads a temporary private copy of the app's IndexedDB and HTTP cache (in the state dir, chmod 700) and deletes it
  after each read. Field names that look like credentials are dropped, but transcripts and attachments are returned as they are.
  They are the user's private content.
- **Clipboard.** Non-ASCII sends go through the display's clipboard (saved and restored). Use a display nothing else shares.
- **Secrets for cloud environments** are read from files on the host and typed into the app. They are never returned or logged.
  Cloud environments are account-wide, so other projects that use one see its variables.
- **The `[sent by Grok Bot]` bracket is a provenance label, not authentication.** Anyone who can type in a thread can type it.

Report a vulnerability privately through GitHub's "Report a vulnerability" (Security tab) on this repository.
Please don't open a public issue for it.

# Limits and known friction

- **Platform.** Linux only, and only Ubuntu 22.04+ / Debian 12+, which is where the Claude desktop app ships. One Claude account per install.
- **Fragile by nature.** It depends on the app's accessibility names and local cache layout. Any app update can change them.
- **Speed.** Calls take 5–30 s, and one UI action runs at a time.
- **Usage.** Sending spends your Claude usage and shows up in the app like any message you'd type.
- **sudo.** The installer uses apt. If sudo needs a password your bot can't supply, you run one command yourself (see [INSTALL.md](../INSTALL.md)).
- **Sign-in.** Sign-in happens in a browser inside the headless viewport. If it ends on a `claude://` link instead of returning to the app, run `bin/claude-open-url '<link>'`.
- **Reaching the server.** Your bot needs SSH to the computer, or your own tunnel or VPN plus a private copy of the token. Nothing sets up a tunnel for you.
- **Non-ASCII typing.** It goes through the clipboard and is covered by a mock test, not proven against every app build.
- **Freshness.** Cached reads can be stale. Each response carries freshness stamps.
- **Display size.** The default viewport is 540×1170. Change it with `--screen`.
- **Permission cards.** Detection is generic (prompt text plus option buttons) and covered by mock tests; a new card layout can slip past it, and then the thread simply shows as waiting. Pressing a card is always an explicit tool call.
- **Forwarding** relays the transcript text as the app renders it (formatting like tables becomes plain text). Files are re-uploaded from staging.

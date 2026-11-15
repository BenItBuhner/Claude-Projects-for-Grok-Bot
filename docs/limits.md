# Limits and known friction

- **Platform.** The app it drives runs on Ubuntu 22.04+ / Debian 12+, where the Claude desktop app for Linux ships. Macs and Windows PCs host that Linux in a small VM or WSL2 ([Hosts](hosts.md)). The Windows script is tested on GitHub's Windows runners; the Mac script hasn't been run on a real Mac yet. One Claude account per install.
- **Projects beta.** The account needs Claude Code Projects. `claude-projects setup check-beta` tells you; without it the plugin stays idle.
- **Fragile by nature.** It depends on the app's accessibility names and local cache layout. Any app update can change them.
- **Speed.** Calls take 5–30 s, and one UI action runs at a time.
- **Usage.** Sending spends your Claude usage and shows up in the app like any message you'd type.
- **sudo.** The installer uses apt. If sudo needs a password your bot can't supply, you run one command yourself (see [INSTALL.md](../INSTALL.md)).
- **Sign-in.** Sign-in happens in a browser inside the headless viewport. If it ends on a `claude://` link instead of returning to the app, run `bin/claude-open-url '<link>'`. New installs turn the viewport off again after sign-in (`viewport_mode: on-demand`).
- **Reaching the server.** Your bot needs SSH to the computer, or your own tunnel or VPN plus a private copy of the token. Nothing sets up a tunnel for you.
- **Non-ASCII typing.** It goes through the clipboard and is covered by a mock test, not proven against every app build.
- **Freshness.** Cached reads can be stale. Each response carries freshness stamps.
- **Display size.** The default viewport is 540×1170. Change it with `--screen`.
- **Permission cards.** Detection is generic (prompt text plus option buttons) and covered by mock tests; a new card layout can slip past it, and then the thread simply shows as waiting. Pressing a card is always an explicit tool call.
- **Forwarding** relays the transcript text as the app renders it (formatting like tables becomes plain text). Files are re-uploaded from staging.

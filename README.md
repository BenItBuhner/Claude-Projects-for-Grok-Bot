# Claude Projects for Grok Bot

<!-- HERO_LINK: once the demo post is up, wrap the <img> below in <a href="DEMO_URL">...</a> so the image opens it. -->
<p align="center"><img src="docs/assets/claude-code-projects-plugin-hero.webp" width="100%" alt="The Claude Code mascot linked to the Grok Bot orb by a dotted line on a dark background."></p>

<!-- DEMO_URL: replace with X post link -->
<p align="center">Demo: coming soon on X</p>

**Grok Bot, working in your Claude Code Projects.** Hand off work to a Project, keep track of what's done or waiting on
you, pick up results, and unblock threads stuck on a permission card. It runs on your own Claude subscription, through
the real Claude app on an always-on computer of yours.

## Set it up

Paste this to your Grok Bot (or any agent that can run commands):

```text
Set up Claude Projects for Grok Bot for me, end to end, from
https://github.com/BenItBuhner/Claude-Projects-for-Grok-Bot

Clone it, read skill/SKILL.md, and follow its Part A (Setup) from the top until `ccp setup status` says ready.
Do everything you can yourself. Only stop to ask me when you need:
- which always-on computer to use (recommend one; offer your own box if I don't have one),
- me to paste one sudo command, if apt needs a password,
- me to sign in to Claude in the viewport link you give me.
Tell me plainly if my Claude account doesn't have Claude Code Projects yet.
Then connect yourself to it, run the read-only verification, and show me what passed.
Keep the skill, so you can re-check, reconfigure or tidy up later.
Never send test messages into my Projects, and never show me tokens or passwords, only where they're saved.
```

You'll be asked at most three things:

1. **Which computer runs it.** Best is an always-on Linux machine (Ubuntu 22.04+ or Debian 12+). An always-on Mac or
   Windows PC works too: the agent sets up a small Linux runtime on it. Your agent's own box is the fallback.
2. **One sudo command**, only if installing packages needs your password.
3. **Signing in to Claude once**, in a browser tab the agent gives you (a noVNC viewport of the app).

You need a Claude account with **Claude Code Projects** (a beta). If yours doesn't have it yet, the agent tells you
and leaves everything installed and idle until it does.

Later, just ask your agent to re-check, reconfigure, tidy up or move it. The skill covers all of that.

## What it does

- **Hand off work.** "Ask the docs project to fix the broken links" goes straight into that Project's thread.
- **Keep track.** Your bot reads your Projects and their threads, and sees what's done or waiting on you.
- **Pick up results.** It waits for replies, fetches files and PRs, and reports back wherever you're chatting with it.
- **Unblock threads.** It sees permission cards ("add this folder to the session"), approves what's clearly part of
  your task, and asks you about the rest.
- **Relay between Projects.** It forwards a message and its files from one thread to another, with a header saying
  where it came from.
- **Your rules.** A plain-text policy says what it may send or approve on its own.

Everything it types starts with `[sent by Grok Bot]`, so you and Claude can always tell its messages from yours.

## How it works

```
Grok Bot ──MCP──▶ ccp_mcp.py ──▶ ccp (CLI) ──AT-SPI + xdotool──▶ Claude desktop app on a headless display
                                       └──── private copy of the app's IndexedDB (project list, sessions)
```

No Claude API, tokens or cookies: it opens Projects with `claude://` links, reads the app's accessibility tree, and
types into the app's own composer. That makes it honest about its limits: one UI action at a time (5–30 s each), and a
Claude app update can break it. Sends spend your Claude usage. More in [Limits](docs/limits.md).

## Learn more

- [Hosts](docs/hosts.md): Linux, Mac, Windows and the agent's box
- [Manual install](docs/manual-install.md): the same setup by hand, configuration, connecting other MCP clients
- [How it works](docs/how-it-works.md): architecture, ids, permission cards, files, transports, all 24 tools
- [Agent skill and policy](skill/SKILL.md)
- [The `[sent by Grok Bot]` tag](docs/origin-bracket.md) · [Limits](docs/limits.md) · [Security](SECURITY.md) · [Changelog](CHANGELOG.md)

Unofficial: not made or endorsed by Anthropic or by Grok Bot.

## License

MIT, see [LICENSE](LICENSE). The Claude and Grok Bot marks in the README image belong to their owners and aren't covered by this license.

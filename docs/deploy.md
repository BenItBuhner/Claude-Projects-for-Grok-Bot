# Updating a running install

The MCP server and the background jobs it starts (attachment fetches and sends) drive the Claude app on the host's
display. Restarting the server while one of them runs can cut a fetch or a send off halfway, so an update waits until
the host is idle.

## The busy check

```bash
./claude-projects jobs            # prints {"busy", "lock_holder", "jobs_running"}
```

It exits 3 while the host is busy and 0 when it is idle. The host is busy when:

- a live process holds the UI lock (`lock_holder` names the process id, the operation and when it started; a holder file
  left behind by a killed process doesn't count), or
- any background job is running (`jobs_running` lists each fetch or send with its job id, step and age).

Background jobs run in their own transient systemd scopes (`claude-projects-job-*`), so restarting the MCP service doesn't kill them,
but a restart can still interrupt the call that is waiting on one. `claude-projects status --json` reports the same
`busy` flag along with the app's state.

## Update steps

```bash
cd ~/claude-projects-mcp                 # your checkout
git fetch origin && git checkout <reviewed commit or tag>

# wait up to 10 minutes for the host to go idle; give up rather than cut a send off
for i in $(seq 1 60); do ./claude-projects jobs >/dev/null && break; sleep 10; done
./claude-projects jobs >/dev/null || { echo "still busy; not restarting"; exit 1; }

systemctl --user restart claude-projects-mcp         # or `python3 claude_projects/proc.py restart claude-projects-mcp` in process mode
curl -s http://127.0.0.1:8791/healthz    # -> ok
./claude-projects status --json
```

Restart only the MCP service. The app, the display and the viewer services don't need a restart for a code update.
Coming from 1.0.2 or earlier, run `claude-projects setup install` once after the checkout: it moves the services to
their `claude-projects-*` names (see [Migration from older names](how-it-works.md#migration-from-older-names)). A
hand-built `claude-projects-mcp.service` keeps its name; change its `ExecStart` to
`<checkout>/venv/bin/python -m claude_projects.mcp_server`, add `Environment=PYTHONPATH=<checkout>`, then
`systemctl --user daemon-reload` and restart it.

## xclip

There is no `bin/xclip` in this repository, so an update has nothing to copy for it. The plugin runs the `xclip` set in
the config (`xclip`, or `CLAUDE_PROJECTS_XCLIP`). By default that is the system package that `install.sh` installs. A host that keeps
its own build somewhere else, such as `~/.local/bin/xclip`, points the config key at that path, and an update leaves it
alone.

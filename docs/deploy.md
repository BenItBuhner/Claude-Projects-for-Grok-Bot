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

Background jobs run in their own transient systemd scopes (`cpg-job-*`), so restarting the MCP service doesn't kill them,
but a restart can still interrupt the call that is waiting on one. `claude-projects status --json` reports the same
`busy` flag along with the app's state.

## Update steps

```bash
cd ~/claude-projects-mcp                 # your checkout
git fetch origin && git checkout <reviewed commit or tag>

# wait up to 10 minutes for the host to go idle; give up rather than cut a send off
for i in $(seq 1 60); do ./claude-projects jobs >/dev/null && break; sleep 10; done
./claude-projects jobs >/dev/null || { echo "still busy; not restarting"; exit 1; }

systemctl --user restart cpg-mcp         # or `python3 cpg_proc.py restart cpg-mcp` in process mode
curl -s http://127.0.0.1:8791/healthz    # -> ok
./claude-projects status --json
```

Restart only the MCP service. The app, the display and the viewer services don't need a restart for a code update.
If your install still uses an older unit name (`ccp-mcp`, or a hand-built `claude-projects-mcp.service`), restart that
one instead.

## xclip

There is no `bin/xclip` in this repository, so an update has nothing to copy for it. The plugin runs the `xclip` set in
the config (`xclip`, or `CPG_XCLIP`). By default that is the system package that `install.sh` installs. A host that keeps
its own build somewhere else, such as `~/.local/bin/xclip`, points the config key at that path, and an update leaves it
alone.

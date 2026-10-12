# examples/ — runnable examples (ops + a toy project)

> **Example, not core.** Everything under `examples/` is a copy-pasteable
> demonstration of how to run Ansatz unattended and what a project looks like on
> disk. **Nothing in the engine depends on `examples/`** — these scripts only wrap
> the real interfaces (`bin/cgm`, `bin/consult`, and the `python -m cgm.*`
> entry points). Each script also carries an "EXAMPLE, NOT CORE" banner so it is
> never mistaken for the control path.

Ansatz has exactly one unattended mode: **run Claude Code as a
resident main agent** (`ops/main-agent-tmux.sh`). The **strategic judgment** —
elaborate → consult GPT → record `master_guidance` → dispatch workers — lives in
that main agent and its skills (`elaboration`, `consult`), *not* in shell.
The two loops here (`strategy-loop.sh`, `watchdog.sh`) are only the unattended
**cadence** and **liveness** scaffolding around that agent.

All scripts resolve paths through `scripts/env.sh` (no hardcoded absolute paths,
no project names baked in) and take the project as a parameter. Operator-facing
prose follows `OPERATOR.md`; the scripts themselves only emit English mechanics.

## `ops/` — unattended-operation scripts

### `main-agent-tmux.sh`
Starts Claude Code detached in a tmux session, in the repo root, so it inherits
`CLAUDE.md`, the skills, and `.mcp.json`. `.mcp.json` is what wires the gateway
MCP server (`python -m cgm.gateway` via `bin/cgm-mcp`); this launcher does
**not** wire MCP itself.

```bash
bash examples/ops/main-agent-tmux.sh
tmux attach -t cgm-main        # watch / interact; CGM_MAIN_TMUX overrides the name
```

Requires `tmux` and the `claude` CLI on PATH. The main agent is the sole control
path — there is no separate Node CLI or persona-seed layer.

### `strategy-loop.sh <project>`
One parameterized strategic-cadence loop. Each beat it runs the consult CLI on
the project's current elaboration and writes the reply under
`runtime/projects/<project>/strategy/`. It does **not** record `master_guidance`
or dispatch — that write goes through the gateway and is owned by the
`consult` skill.

```bash
bash examples/ops/strategy-loop.sh <project>
touch runtime/projects/<project>/.strategy.stop   # graceful stop at the next beat
```

- `CGM_STRATEGY_BEAT` — seconds between consults (default `7200`, ~2h).
- `CGM_CONSULT_TRANSPORT` — `gpt_pro` (default), `claude_api`, `claude_code`, or `off`.
  There is no `web`/`auto` transport.

The loop consults only when an `elaboration.md` is present for the project; a
real deployment consults on *new state*, not a blind timer.

### `watchdog.sh <project>`
Liveness / stall alerting. Each beat it probes the verify service at
`CGM_VERIFY_URL` and reads `cgm status <project> --json`, alarming if the
verify endpoint is down or any worker's label is `stuck?` / `timeouts?` / `dead` /
`error`.

```bash
bash examples/ops/watchdog.sh <project>
```

- `CGM_WATCHDOG_BEAT` — seconds between checks (default `300`).
- `CGM_VERIFY_URL` — health endpoint (from `env.sh`; not hardcoded).

**The `CGM_NOTIFY` contract.** Alarms fire `${CGM_NOTIFY:-:}`, which is any
command the operator sets; the message is passed both as an argument and on
stdin. The default `:` is a silent no-op, so the watchdog is quiet until you
wire a hook. Examples:

```bash
# append to a log
CGM_NOTIFY='tee -a runtime/logs/watchdog-alarms.log' bash examples/ops/watchdog.sh <project>
# POST the message body to a webhook
CGM_NOTIFY='curl -fsS -d @- https://example.invalid/hook' bash examples/ops/watchdog.sh <project>
```

There is no Telegram / vendor binding. Set `CGM_NOTIFY` in
`config/cgm.env` to make it the default for a deployment.

### Keeping the loops running
These loops run in the foreground. To make them persist beyond your shell, wrap
them the same way you would anything else — a `tmux` window or `setsid`. Do
**not** register them in the services pidfile/autostart manifest: that registry
belongs to `scripts/services.sh` (which owns `verify` and the dashboard);
`examples/` never touches it.

## `project/` — a toy project on disk
`project/PROBLEM.md` plus a 2-fact `project/fact_graph/` show the shape of a
Ansatz project — a verbatim problem statement and a content-addressed fact DAG
written to `cgm.core`'s real schema. It is **illustrative sample data**, not a
verified run; see `project/fact_graph/README.md`. For a paper-pipeline example,
see the write-paper skill's own toy project under
`agents/skills/write-paper/examples/paper/`.

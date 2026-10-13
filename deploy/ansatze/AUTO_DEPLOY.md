# Automatic deployment after a pull request is merged

Once installed, the website follows **the latest `main` commit whose complete
Source checks workflow passed**. A pull request's review approval does not
publish its branch: merge it first. Both the public VitePress site and the Python
console, including shared views, come from the same release.

```text
Pull request → review + tests → merge into main → Source checks pass
                                                     ↓
                                 Host polls public GitHub metadata (2 min)
                                                     ↓
                             Build as restricted ansatze-build user
                                                     ↓
                           Pause admissions → wait until work is idle
                                                     ↓
                               Switch /opt/ansatze/current atomically
                                                     ↓
                             Restart console + sponsored broker
                                                     ↓
                          Health + public release marker → publish
                                         failure → restore previous
```

There is **no GitHub PAT, webhook secret, SSH deployment key, or self-hosted
GitHub Actions runner**. The repository is public, so the host can read the
[workflow-runs API](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow)
without authentication. A timer performs at most about 30 normal metadata
requests per hour, below GitHub's usual unauthenticated limit of 60 per hour;
other clients sharing the host's public IP share that allowance. Rate limiting,
API errors, non-successful CI, and a newer untested `main` all leave production
unchanged. Polling adds roughly two minutes after CI, plus build and drain time.

## Install once on the existing host

This migration targets the service layout installed by
[`install.sh`](install.sh). It preserves `/etc/ansatze` credentials,
`/var/lib/ansatze` application state, the console mount, existing service
hardening, registration and Lean broker drop-ins, Caddy routes, and optional
news/problem timers. It never edits or resets the developer checkout.

Prerequisites: root access; systemd with transient service sandboxing and
cgroup IP filtering; Python 3.11–3.13 with venv support; Node.js and npm; Git,
curl, and Caddy. The installer defaults to this host's existing Python and
Node locations. Use `--python` and `--node-bin` for another supported host.

Run these from a reviewed copy of this repository:

```bash
python3 deploy/ansatze/install_auto_deploy.py \
  --render-to /tmp/ansatze-auto-deploy-review

# Inspect the rendered files before the one-time host change.
sudo python3 deploy/ansatze/install_auto_deploy.py --apply

systemctl status ansatze-auto-deploy.timer
journalctl -u ansatze-auto-deploy.service --since today
cat /opt/ansatze/deployment-status.json
```

The installer validates Caddy before replacing its configuration. Original
files are backed up under `/etc/ansatze/backup/auto-deploy-<timestamp>`. It
creates a non-login `ansatze-build` system account, installs reviewed deployment
programs root-owned under `/usr/local/lib/ansatze`, and writes nonsecret settings
to `/etc/ansatze/auto-deploy.json`. It redirects code paths with systemd drop-ins
and the static root with a Caddy reload. **Installation does not restart the
running console or broker.** Initially `/opt/ansatze/current` points at the old
checkout, which remains the initial rollback target.

The first handoff from the older console is conservative: it briefly pauses
all console requests, including reads, and requires both persisted work and
all existing console/broker TCP connections to drain. The legacy console uses
HTTP/1.0. If a connection remains, the handoff is deferred and the page becomes
available again. This fallback is allowed only when `current` resolves to the
explicitly configured legacy checkout. Once the new console runs, subsequent
rollouts require its request activity-counter protocol; missing counters fail closed.

## What a release changes

| Location | Role |
| --- | --- |
| `/opt/ansatze/releases/<40-character SHA>` | Complete source, its own virtualenv, built site, root-owned receipt |
| `/opt/ansatze/current` | Atomic symlink used by Caddy and installed service drop-ins |
| `/opt/ansatze/previous` | Last code target, retained for rollback |
| `/opt/ansatze/source.git` | Public source mirror, fetched without credential helpers |
| `/opt/ansatze/deployment-status.json` | Latest deployment, deferral, or rollback result; no run contents |
| `/run/ansatze-deploy/maintenance` | Short console-admission pause; removed after success, deferral, or failure |
| `/run/ansatze-deploy/bootstrap-maintenance` | Additional read pause for the first legacy handoff only |

The virtualenv is built at its final absolute release path, so its executable
entrypoints remain valid. Package installers and VitePress run in a separate
unprivileged systemd unit. That unit cannot read application credentials or
user homes, modify the host, reach the sponsored loopback broker, access cloud
metadata addresses, or retain child processes after the build. A narrow DNS
stub exception permits `127.0.0.53`; private and link-local networks remain
denied. The source build is never run as root. Root only fetches/extracts source,
validates output paths, freezes ownership, switches symlinks, and controls
services. Escaping symlinks, hard-linked files, and special files in build
outputs are rejected before privileged writes.

Repository updates **do not rewrite the installed deployment agent, Caddy
configuration, service policies, provider credentials, Lean broker/toolchain,
or external engine installations**. Changes to those require a separately
reviewed administrator installation. Regular application, console, sharing,
and public-site changes deploy automatically. Database migrations are not
reversed by code rollback; changes requiring irreversible data migration need
a separately planned migration and backup.

## Work is drained before activation

The agent defers before building when run records, analysis rechecks, Lean
harnesses, or automatic pipelines report active work. Before switching code it
pauses new console requests at Caddy, temporarily pauses the optional
news/problem timers, and checks both the running console's in-flight request
counter and persisted background states again. This also covers GET requests
that may start a guest automatic pipeline. Existing requests are allowed to
finish; new console reads, polling and shared views receive a brief 503 during
the final handoff. The public documentation site remains available. Long or
stale active records defer deployment; they are not killed or rewritten by
the agent. Paused timers and any unfinished build unit are recorded for
cleanup, including when systemd interrupts the deployment service.

The actual service restart creates a short reconnection window. Users may need
to retry an action that received HTTP 503 with `Retry-After: 15`. API clients
must retain their unsent inputs. Runtime workspaces, account databases, saved
runs, and sharing snapshots remain outside release directories.

## Check, pause, retry, or roll back

Read public eligibility without installing or deploying anything:

```bash
python3 deploy/ansatze/auto_deploy.py --check
```

Run the installed check immediately, or pause future automatic checks:

```bash
sudo systemctl start ansatze-auto-deploy.service
sudo systemctl stop ansatze-auto-deploy.timer
# Resume after maintenance:
sudo systemctl start ansatze-auto-deploy.timer
```

A successful rollout checks the console `/health`, broker `/healthz`, and the
public site's `/release.json` over local HTTPS with the real hostname and
certificate. It does not spend provider credits. A failed rollout restores the
previous code target and restarts the two services. The activation journal is written and synced before switching code. Cleanup
restores the previous target after an interrupted activation, including when
the next timer starts after a reboot. The rejected SHA remains
blocked in `deployment-status.json` so the timer does not repeatedly interrupt
the site. Merge a corrected commit to retry; after diagnosing a transient
problem, an administrator can clear only that status file and start the service
again.

For a manual rollback, stop the timer and schedule an idle maintenance window.
Resolve and review `/opt/ansatze/previous` first; switch `current` atomically to
that target, restart `ansatze-kimi` followed by `agent-monitor`, then verify both
health endpoints and the public marker. Leave the timer stopped until `main`
contains the corrected code, or it will intentionally deploy the latest
passing main again. Do not delete the release referenced by `current` or
`previous`. Old releases are retained; disk cleanup is an explicit operator
action.

## GitHub repository settings

Protect `main` with required review and the Source checks jobs (`monitor`, both
`cgm` matrix jobs, and `website`). Restrict who can push or bypass protection.
The `Main release ready` job summarizes eligibility after those jobs pass.
A successful GitHub workflow means a commit is eligible; the host's status file
and journal report whether it was actually deployed or deferred by active work.

Keep fork pull-request tests on GitHub-hosted runners without production
credentials. Do not replace this model with `pull_request_target` executing
contributor code, deploy on an approval event alone, or give a build a production
provider key. A contributor's PR cannot deploy before it is merged into the
maintainer-controlled branch and passes that branch's checks.

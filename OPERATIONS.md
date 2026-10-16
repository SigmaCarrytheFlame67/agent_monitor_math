# Hosted deployment map

This file documents how the hosted Agent Monitor deployment at
<https://ansatze.ai> is wired. The installable source of truth is
[`deploy/ansatze/`](deploy/ansatze/README.md): its `install.sh` renders the
Caddyfile and systemd units, and the files it installed under `/etc` are what
actually runs. Hostnames, TLS and the console mount are never taken from other
files in this repository.

## Public entry points

- Homepage and documentation: <https://ansatze.ai/>
- Stable console entry: <https://ansatze.ai/sign-in>
- `https://www.ansatze.ai/*` answers with a permanent (308) redirect to the apex
  when its DNS record points at this host. The site must stay on one origin:
  the docs hand problems to the console through same-origin storage, and the
  research client requires a same-origin `/sign-in` redirect.

The site runs on one EC2 instance (public IP `44.196.29.12`) behind Caddy with
Let's Encrypt certificates. The DNS records must be DNS-only (not proxied by
Cloudflare): synchronous DAG and Lean requests can outlast a proxy's 100-second
origin timeout. The security group must allow 80 and 443 only; the console
(4600) and the sponsored Kimi broker (4610) listen on loopback. Earlier
`sslip.io` and `moonshot.hailab.io` addresses are obsolete. A successful
loopback request using `curl --resolve` checks Caddy's routing but does not
establish public reachability; `deploy/ansatze/smoke.sh --public` also checks
through ordinary DNS.

The documentation site is mounted at `/`, so public pages use clean URLs such
as `/news` rather than `/docs/news`. `/docs/*` is not aliased on ansatze.ai;
only the historical `/docs/sign-in` variants remain direct, non-cacheable
sign-in redirects.

The `/sign-in` route is deliberately stable. Caddy redirects it to the current
mounted console, which means the randomized mount can be rotated without
rebuilding the documentation site. The redirect destination is observable to
any visitor; authentication, not knowledge of that path, is the security
boundary.

The redirect must land on `<mount>/login` (not `<mount>/`): the docs research
client follows `/sign-in` and derives the API base from that final path. Caddy
matches `/sign-in` before the static site, whose `sign-in.html` bridge would
otherwise loop.

The page can also deliberately create a restricted guest session. Guest access
is enabled by default in application code and can be disabled with
`AGENT_MONITOR_GUEST_ACCESS=0`. It is not equivalent to
`AGENT_MONITOR_PUBLIC=1`, which automatically creates guest sessions instead of
showing the sign-in/guest choice. Before enabling either mode on a hosted
instance, verify the guest engine allowlist, concurrency/session-creation
and rolling run/verification limits, output-token cap, and provider-key policy
from `.env.example`. Guests and new accounts default to **Ansatz CGM · Kimi K3**
(engine id `kimi`): the tool-free CGM pipeline of the Math Agent Harness
(strategist, up to two independent workers, one cold model-verifier call per
lemma, bounded repair, curator) pinned to Kimi K3. A guest run is limited to
12 model calls and 2400 s by the guest budget profile (`AGENT_MONITOR_GUEST=1`
is set for every guest run), with an 8192-token output ceiling per call; a
typical run takes 5–25 minutes on Kimi. Its verified facts, exploring graph
and deterministic `proof.md` are model-verifier results, never a Lean
certificate. Hosted runs use the sponsored Kimi broker (`ansatze-kimi.service`,
loopback only), which is the only process that holds the Kimi key; the console
environment holds no Kimi key. A visitor's own Kimi key goes to the operator's
Kimi endpoint (`AGENT_MONITOR_KIMI_BASE_URL`, default the built-in Kimi K3
base); account-level base URLs are ignored for this engine. The one-call
`plain` baseline is also eligible when enabled. Guests cannot set custom provider origins or inherit
unrelated server credentials. The allowance is **10 cumulative run starts** per
guest session, after which sign-in is required; deleting runs does not restore
starts. Claude Code and Codex are for the administrator only (a sign-up is a
member account, which cannot use them). A `PLAIN_CMD` or
`KIMI_PROOF_CMD` override disables the corresponding engine for guests because
arbitrary local commands are outside the built-in contract.
Guest Formal/Lean and informal/formal DAG views are enabled by the reviewed
update in `artifacts/guest-formal-dag-2026-09-15/`. The derived pipeline starts
as soon as a guest run finishes and uses only short-lived Kimi proxy
credentials; Lean checking always uses the isolated broker. A run whose CGM
fact graph proves its target gets its informal DAG deterministically from the
facts (`cgm-fact-graph`, no model call; labelled model-verifier accepted only
with a valid harness receipt, otherwise "unattested CGM facts") before Lean
translation, which then follows the fact blueprint. Completed guest runs whose pipeline could not start (for
example, guest capacity was full) start it once when opened. Direct Verify,
Compile, Check, Audit, and feedback actions and DAG refreshes are available to
guests, bounded separately from proof-run starts
(`AGENT_MONITOR_GUEST_DERIVED_ACTIONS_PER_HOUR`, a global concurrency cap, at
most four Lean repair rounds for Verify/generation and one for a feedback
revision; see "Guest Formal/Lean and DAG actions" below). Coding-agent Formal
harnesses still require sign-in. Lean generation defaults to four repair rounds
(`lean_verify.AUTO_MAX_REPAIRS`) when a request omits `max_repairs`, as the
automatic pipeline does; signed-in requests may ask for at most six. The
console's Verify button requests two rounds for every user. Feedback revisions
allow at most three.
Self-registration is closed unless `AGENT_MONITOR_REGISTRATION_OPEN=1` is
explicitly set, including during first-account bootstrap; the owner's
administrator account is created on the host with `agent-monitor create-admin`.
ansatze.ai opens registration with a reviewed drop-in (see "Registration and
what members can do" in `deploy/ansatze/README.md`). Self-registered members
are untrusted: `agent_monitor/account_policy.py` keeps them on the tool-free
engines (`kimi`, `plain`, and `math_harness` on their own API keys only)
without subscriptions, subagents or custom provider endpoints, because ordinary
local harnesses do not isolate hostile prompts from the host. Enrollment
changes require a daemon reload and service restart after
confirming no proof processes are active. `AGENT_MONITOR_REGISTRATION_EMAIL_SUFFIXES`
(for example `.edu`) limits which addresses may sign up, and
`AGENT_MONITOR_REQUIRE_EMAIL_VERIFICATION=1` requires an emailed confirmation
link before a new account can sign in; it needs the SMTP settings, which also
send password-reset links. `agent-monitor set-password` sets a password from the
host when email is not configured.

## Ansatz CGM runs

The `kimi` and `math_harness` engines run the CGM pipeline in
`agent_monitor/runners/math_harness_runner.py`. Its budgets come from the
console environment (`/etc/ansatze/console.env`; restart `agent-monitor`
after a change) and are clamped to hard ceilings. Guest, public and sponsored
runs (`AGENT_MONITOR_GUEST`, `AGENT_MONITOR_PUBLIC_KIMI` or
`AGENT_MONITOR_SPONSORED_KIMI` is `1`, which includes a signed-in account's
sponsored Kimi runs) always use the guest profile, whose defaults are also its
ceilings, so an environment value can only lower them.

| Variable | Signed-in default (ceiling) | Guest, public, sponsored |
|---|---|---|
| `AGENT_MONITOR_CGM_MAX_CALLS` (recorded provider calls, low-effort and format retries included) | 16 (64) | 12 |
| `AGENT_MONITOR_CGM_MAX_WORKERS` (also bounded by `AGENT_MONITOR_MAX_ITERATIONS`) | 3 (3) | 2 |
| `AGENT_MONITOR_CGM_MAX_REPAIRS` | 2 (4) | 1 |
| `AGENT_MONITOR_CGM_MAX_VERIFY_CANDIDATES` | 2 (3) | 2 |
| `AGENT_MONITOR_CGM_MAX_LEMMAS` | 8 (16) | 6 |
| `AGENT_MONITOR_CGM_MAX_SECONDS` (wall clock) | 5400 (14400) | 2400 |
| `AGENT_MONITOR_CGM_CONCURRENCY` (provider calls in flight) | 3 on API routes, 1 on Codex/Claude subscription routes unless set (6) | 2 |

- `AGENT_MONITOR_CGM_API_TIMEOUT` (default 300 s, or 630 s on a sponsored
  Kimi run; 60–900) is each API call's total timeout;
  `AGENT_MONITOR_CGM_REASONING_EFFORT` (`low`, `high` or
  `max`) overrides the Kimi effort of every stage but the curator. Guest and
  sponsored CGM calls always have an 8192-token output ceiling.
- On one linked Codex or Claude Code account each call may take 600 s
  (`AGENT_MONITOR_CODEX_TIMEOUT`; `AGENT_MONITOR_MATH_HARNESS_CLAUDE_TIMEOUT`,
  where Claude Code workers and repairs get 1200 s). At Extra high or Ultra
  effort, CGM and Analyze calls, follow-ups included, get 1800 s unless one
  of those or `AGENT_MONITOR_CLAUDE_TIMEOUT` is set. Every call still ends
  with the run's wall clock.
- A problem statement may be at most 44,000 bytes of UTF-8 text; larger ones
  are refused before a run slot or a sponsored credential is used.
- Calls still in flight 15 s after `AGENT_MONITOR_CGM_MAX_SECONDS` are
  abandoned. Stop sends SIGTERM: the runner abandons its in-flight calls,
  publishes nothing and exits 143 (`kimi` and `math_harness` alike); any
  other failure exits 1.
- A run writes `proof.md` only when it publishes, so the derived-views
  pipeline (Lean and the DAGs) waits at least the run's outer watchdog, which
  follows `AGENT_MONITOR_CGM_MAX_SECONDS`, plus 300 s
  (`AGENT_MONITOR_AUTO_PIPELINE_TIMEOUT`, default 7200 s, only raises it).
- Every published run carries `math_harness/project/engine_receipt.json`.
  The current writer records receipt schema 3 (schema 4 for Max, below) with verification policy
  `solved-first-topological-waves-stop-on-target@1` (the first block, in the
  worker's written order, that the verifier does not accept decides the
  outcome; at concurrency 1 this is exactly the schema-2 order). Receipts of
  schema 3 with the earlier `solved-first-level-waves-stop-on-target@2` label
  and schema-2 receipts (`solved-first-topological-stop-on-target@1`) still
  attest; anything else, including a Codex-route receipt that claims response-only
  isolation version 4 (see "Codex vetting"), is shown as unattested.
  Validation is fail-closed, and an attested result is labelled model-verifier
  accepted, never a Lean certificate.
- **Max** (model `max`, route `dual_subscription`) runs `math_harness` on
  both of the run owner's linked subscriptions at once. Claude Code, on the
  first of `fable`, `opus`, `sonnet` the account offers, answers the
  strategist, verifier and curator calls; Codex, on the first of
  `gpt-6-astra`, `gpt-6.1-sol`, `gpt-6-sol`, `gpt-5.6-sol` it offers, answers
  the worker and repair calls; both at maximum effort, with the run mode fixed.
  The console offers it only when both accounts are linked, which on this site
  means the administrator; members and guests are refused, and Max is never
  billed to an API key. Each call sees only its own account: Codex calls get
  `CODEX_HOME` and the service `HOME`, never `CLAUDE_CONFIG_DIR`, and Claude
  calls never get `CODEX_HOME`. Each call may take 1800 s and the run 10800 s
  (`agent_monitor/cgm_max.py`); concurrency stays 1. The receipt is schema 4:
  transport `hybrid`, with `workflow.routes` naming the advisor (Claude Code)
  and worker (Codex) models, and every call checked against its side. A
  follow-up continues as Max, or stops if either account was disconnected.
  Lean and graph work after a Max run uses the Codex account with the worker
  model at high effort. Derived Codex calls have a fixed per-call limit, so
  the same cap applies after any Codex-subscription run made at extra-high or
  maximum effort; the console's run-profile note says so.
- Sponsored calls go through the Kimi broker's credential gateway, which runs
  at most `PUBLIC_KIMI_GATEWAY_CONCURRENCY` (default 8) upstream calls at once.
  Each guest run keeps up to 2 in flight, so a few concurrent runs can fill
  it. A call that finds every slot busy waits for one, first come first
  served, for up to `PUBLIC_KIMI_GATEWAY_QUEUE_SECONDS` (default 60, at most
  120; 0 turns the wait off) while fewer than `PUBLIC_KIMI_GATEWAY_QUEUE_DEPTH`
  (default 32) calls wait. The wait comes out of that call's own total time
  (`PUBLIC_KIMI_UPSTREAM_TIMEOUT_SECONDS`, default 600 s), and the gateway
  answers every call within that time (HTTP 504 if Kimi is slower). Clients
  must wait longer than that: the sponsored ones wait 630 s and send their
  remaining time as `X-Ansatze-Timeout`, so ship the broker with them (until
  then, `PUBLIC_KIMI_UPSTREAM_TIMEOUT_SECONDS=225` keeps the old timing). A
  call whose caller hangs up first is stopped within a second or two, so it
  keeps no slot. Only a full queue, or a wait that
  runs out, returns HTTP 429, and such a call never reached the provider. The
  sponsored clients may send such a call again (next item).
- Sponsored Kimi clients (the CGM runner, derived Formal/DAG and Lean calls,
  the news summarizer) send their whole remaining timeout as
  `X-Ansatze-Timeout`, so the broker and gateway can only answer sooner;
  CGM, Formal/DAG and Lean calls wait 630 s (a CGM call less near the run's
  deadline), the news summarizer 90 s. A sponsored call is retried only when
  it certainly never reached processing (the broker could not be reached; a
  typed `forwarded: false` busy, unavailable or soon-freed quota refusal; or
  an upstream 429/503 within 2 s): at most three attempts, after the reply's
  `retry_after` (2–30 s) or 5 s then 15 s, never with less than 30 s of the
  call left, and at most six retries per CGM run. A timeout, any other 5xx,
  an empty completion or an untyped error is never replayed. On a sponsored
  CGM run a strategist call that stays refused is sent again after 30, 60
  and 120 s; when it still fails, or fails ambiguously, the run continues
  with the direct plan instead of ending (a spent call budget or an expired
  token still ends it), and when every call of every worker was refused one
  rescue worker turn runs after 60 s (no pause leaves the run less than
  300 s). The run's final error says what happened, for example "Kimi's
  provider is temporarily overloaded and refused the request (HTTP 503).
  Please try again in a few minutes."; it adds "No tokens were used." only
  when no call of the run was processed.
- Those resends are not counted in `AGENT_MONITOR_CGM_MAX_CALLS`: a run sends
  at most six transport retries and three strategist resends, all of calls
  refused before processing, so the strategist's one call record can stand
  for up to ten requests and a guest run makes at most 21 (the rescue worker
  is a recorded call). Derived Formal/DAG and Lean calls retry at most twice
  each. The gateway counts a call against `PUBLIC_KIMI_GATEWAY_CALLS_PER_HOUR`
  before forwarding it, so upstream 429/503 refusals use that quota too.
- Every 2xx reply a direct API call received is booked, whatever the
  provider and whether sponsored or not: an empty completion that ended a
  CGM call keeps its tokens in the receipt's call record and the usage
  events, a user-key Kimi call that replaced an empty completion books both
  replies, and Formal/DAG and Lean calls book each reply in the usage ledger.
- After deploying a change to `agent_monitor/kimi_k3.py`, restart
  `agent-monitor`: the console loads that module at start, and Formal/DAG and
  Lean calls keep their previous sponsored handling until the restart.

### Guest Formal/Lean and DAG actions

Manual guest Verify, Compile, Check, Audit, feedback revisions and
model-backed DAG refreshes (Outline, Formal · Lean) each use one guest
Formal/DAG action. Starting the automatic pipeline when a guest proof finishes
also counts as one; at most two guest pipelines run at once, and one that
cannot start then starts when the run is next opened.

| Variable | Default | Range |
|---|---|---|
| `AGENT_MONITOR_GUEST_DERIVED_ACTIONS_PER_HOUR` (per guest session) | 24 | 1–60 |
| `AGENT_MONITOR_GUEST_DERIVED_GLOBAL_ACTIONS_PER_HOUR` (all guests) | 120 | 1–480 |
| `AGENT_MONITOR_GUEST_DERIVED_GLOBAL_CONCURRENCY` (guest actions running at once) | 2 | 1–8 |
| `AGENT_MONITOR_GUEST_DERIVED_ACTIONS_PER_CLIENT_PER_HOUR` (per client address) | 48 | 1–480 |

- A guest runs one action at a time. While the automatic pipeline is still
  running the answer is HTTP 409; a limit gives HTTP 429. Both carry a
  `Retry-After` header and a plain `{"error": …}` sentence, which the console
  turns into "try again in about N minutes".
- Compile, Check, Audit and feedback revisions refresh the formal DAG on a
  background thread afterwards, which can make a sponsored Kimi call. That
  refresh keeps the action's slot until it finishes, so the one-at-a-time and
  site-wide concurrency limits cover it; the request itself still returns at
  once.
- Lean repair rounds are capped at 4 for Verify/generation
  (`lean_verify.GUEST_MAX_REPAIRS`) and 1 for a feedback revision
  (`lean_verify.GUEST_REVISE_MAX_REPAIRS`). A guest's automatic formalization
  uses all 4; the console's Verify button requests 2. Guest Lean source is limited to
  100,000 characters, and every model call uses Kimi K3 through the sponsored
  broker.
- Reading any graph or Lean result, refreshing the model-free Informal · CGM
  view (`kind=engine`, by GET or POST), and reading the Lean toolchain status
  (`GET /api/lean/status`: broker availability and the pinned toolchain only,
  never host paths) are free.

### Guest fairness, request bodies and retention

The guest limits above are per guest identity and site-wide. Because anyone
can create a guest identity, each client address also has its own budget:

| Variable | Default | Range |
|---|---|---|
| `AGENT_MONITOR_GUEST_SESSIONS_PER_CLIENT_PER_HOUR` (new guest sessions) | 6 | 1–120 |
| `AGENT_MONITOR_GUEST_RUNS_PER_CLIENT_PER_HOUR` (guest run starts) | 12 | 1–240 |
| `AGENT_MONITOR_GUEST_CLIENT_CONCURRENCY` (active guest runs) | 1 | 1–16 |
| `AGENT_MONITOR_GUEST_RUN_RETENTION_DAYS` (0 keeps finished guest runs) | 0 (30 on ansatze.ai) | 0–3650 |

- The client is the last `X-Forwarded-For` hop when the console's peer is the
  loopback proxy (Caddy replaces any value a client sends), or the remote peer
  itself; IPv6 addresses are grouped by /64. Requests from this host are
  exempt. Clients behind one NAT share a budget, and someone with many
  addresses can still reach the site-wide caps: those remain the cost bound.
- The sponsored broker still keys its own per-client limits by a hash of the
  account id (`X-Public-Client-IP`); the console's per-client limits above are
  the per-address control.
- Guests can attach at most two text files (Markdown, LaTeX, Lean, plain text,
  CSV, TSV, JSON), 64 KB in total; `guest_policy.attachments` advertises it.
  A guest `/api/runs` body is limited to 1 MB and a guest `/chat` request is
  refused before its body is read; members keep the 29 MiB attachment
  ceiling. Every connection has a 300 s socket timeout, and the unit sets
  `MemoryHigh=10G`, `MemoryMax=12G` and `TasksMax=4096`.
- With a retention period set, the console deletes finished guest runs (run
  record and workspace; usage history is kept) that ended more than that many
  days ago, checking every six hours. `agent-monitor purge-guest-runs` remains
  for manual clean-ups.
- Every POST and DELETE must come from the console's own origin: a request
  whose `Origin` differs from `Host`, or whose `Sec-Fetch-Site` is
  `cross-site`/`same-site`, gets HTTP 403, and the HTML-form content types
  (`text/plain`, `application/x-www-form-urlencoded`, `multipart/form-data`)
  get HTTP 415, before the body is read. Tools that send neither header (curl,
  `smoke.sh`) are unaffected.

## Analyze runs

Analyze checks an existing manuscript (a link, pasted text or uploads) into a
verdict-annotated statement graph. The runner is
`agent_monitor/runners/analyze_runner.py`, started in the run workspace like
the CGM runners; it reads `analysis/request.json` (written by `jobs.py` with
`agent_monitor.analyze.request`) and makes tool-free model calls through the
same response-only turn machinery and routes as `math_harness` (sponsored
Kimi through the loopback broker with the `math_harness` credential scope, an
own key, or, for administrators only, a Codex or Claude account; Max is
refused).

The console offers it as the third mode next to "Informal only" and
"Informal + formalization": `POST /api/runs` with `run_kind: "analyze"`
(engine `analyze`). Old records without `run_kind` are proofs; the engine
listing advertises `modes: ["analyze"]` for this engine (`["prove"]` for every
other one) and a per-viewer `analyze` policy, so a browser shows the mode only
when the server offers it.

- **In the console.** The composer's mode switch shows Analyze only while the
  listing offers it (a page served before the server knew Analyze, or with
  `AGENT_MONITOR_ANALYZE_ENABLED=0`, keeps the two proof modes; a runner that
  still needs setup is shown to administrators only). One pasted link is
  previewed (`/api/analyze/resolve`; the openai/math root lists its
  manuscripts to pick one); otherwise the pasted text or the attached files
  are the manuscript. A Problems-page hand-off (`mode: "analyze"`,
  `analyze_url`) fills the composer and previews the link; nothing starts
  until the visitor presses Analyze. An analysis shows its report (raw HTML in
  it is shown as text), the runner stages, and the Manuscript DAG: blue
  checked by a model verifier, amber gap, red error, grey unclear, hollow not
  checked (green stays reserved for Lean-kernel results), with verdict
  filters, reference / inferred / model-proposed edges, and per-result
  Re-check, Formalize statement and Open in source. A result whose verifier
  reply could not be read is "not checked (no usable verifier output)", as in
  the report, never "unclear". Continue and chat are hidden for analyses;
  their Progress pane and process log drop raw model JSON and token-usage
  lines; the Lean tab is named "Lean statements" and never speaks of
  verifying a proof; projects offer a Prove / Analyze kind.

- **Who may analyze what.**

  | Role | Route | Sources | Results / calls / wall clock |
  |---|---|---|---|
  | guest | sponsored Kimi K3 only (a guest's own key is not used) | arXiv, GitHub and openai/math links; paste ≤ 20,000 characters; the text-only guest uploads (2 files, 64 KB) | 12 / 16 / 2,400 s |
  | member | sponsored Kimi K3, or its own API key (never a subscription or Max) | + any public HTTPS PDF/TeX/zip/tar.gz link; paste ≤ 400,000 characters; TeX/Markdown/text/PDF and zip/tar(.gz) uploads | sponsored 30 / 36 / 3,600 s; own key 120 / 140 / 3,600 s |
  | admin | any route, including a Codex or Claude account (response-only, tool-free) | all | 256 / 300 / 14,400 s |

  A guest's generic HTTPS link is refused with "Guests can analyze arXiv and
  GitHub links; this file needs a member account." A guest starts at most one
  analysis per hour (three per client network), and each one also counts
  against the guest run limits (session, hourly and site-wide). A guest
  re-checks at most 2 results of an analysis (counted on the run record),
  2 per guest session and 4 per client network each hour, on top of the guest
  Formal/DAG limits. A member's analysis counts against the member run limits
  (2 at once, 12 per hour). In public mode (`AGENT_MONITOR_PUBLIC=1`, where
  every account inherits the server's provider keys) a member analyzes on
  sponsored Kimi K3 only, like the own-key CGM harness, which public mode
  removes. Every refusal is a plain message: 400 for the request, 403 for a
  switch, 429 with `Retry-After` for capacity.
- **Sponsored capacity.** At most two sponsored analyses (and re-checks) run
  at once (`AGENT_MONITOR_ANALYZE_SPONSORED_CONCURRENCY`, 1-8, default 2): an
  in-memory semaphore keyed by run, released when the worker exits however it
  ends (finished, failed, stopped, crashed); a restart starts with none held.
  A site-wide daily cap (`AGENT_MONITOR_ANALYZE_SPONSORED_RUNS_PER_DAY`,
  default 40, UTC days; `0` turns sponsored Analyze off) counts every
  sponsored analysis and every sponsored re-check in
  `$AGENT_MONITOR_DATA_DIR/analyze_sponsored_usage.json` (file-locked; a start
  that never launched is refunded). Refusals: "Sponsored Analyze is busy …"
  (429, retry in 60 s) and "Sponsored Analyze is at today's limit — try
  tomorrow or use your own API key" (429, retry at UTC midnight). Guests get a
  share of that pool, never all of it: at most
  `AGENT_MONITOR_ANALYZE_GUEST_SPONSORED_CONCURRENCY` slots (default half of
  the slots, at least one) and `AGENT_MONITOR_ANALYZE_GUEST_SPONSORED_RUNS_PER_DAY`
  of the daily runs (default half of the daily cap, rounded down; at most the
  cap; `0` keeps guests off sponsored Analyze), counted as `guest_runs` in the
  same file ("Sponsored Analyze for guests is busy …" / "… at today's limit").
  The share follows who asks: an administrator's re-check of a guest's
  analysis is not charged to it. A sponsored analysis is also held to 150
  calls and 5,400 s whatever the role, inside the broker token's 200 calls
  and 7,200 s.
- **Kill switches.** `AGENT_MONITOR_ANALYZE_ENABLED=0` hides the engine from
  the listing and refuses Analyze for everyone (403); existing analyses stay
  readable. The switches also cover model work on existing analyses:
  re-checks and statement formalization (∎ Formalize statements, and the
  automatic Lean stage) answer the same 403 when Analyze is off, when guests
  are off (for a guest), and, for an analysis that ran on the sponsored
  route, when sponsored Analyze is off (a daily cap of 0, or a guests' share
  of 0 for a guest). The deterministic Manuscript DAG and compiling or
  checking Lean that already exists are not model work and stay available. `AGENT_MONITOR_ANALYZE_GUESTS=0` refuses guests only ("Analyze
  needs a signed-in account on this site."), and so does
  `AGENT_MONITOR_GUEST_HOSTED_KIMI=0` (guests analyze on sponsored Kimi K3
  only). An `ANALYZE_CMD` override makes the runner operator-chosen local
  code, so it removes Analyze from guests and members. `AGENT_MONITOR_GUEST_ENGINES` and `AGENT_MONITOR_MEMBER_ENGINES`
  name proof engines; Analyze follows its own switches.
- **Run record.** `run_kind: "analyze"`, `formalize` (false unless asked) and
  `run["analyze"]` (`agent_monitor.analyze.record.run_block`: source card,
  caps, focus, summary; `recheck` for the latest re-check, `rechecks` for the
  running tally: re-checks started, their calls, tokens and estimated cost).
  A re-check runs as a job of the console process: one that a restart (or a
  crash) left "running" becomes `interrupted` ("the previous verdict is
  kept") when the run is read, at startup (the manifest flags it with
  `recheck_running`) or on Stop. Each re-check call is booked in the usage
  ledger as stage `recheck`, billed like the analysis; `totals` stay the
  analysis's own engine usage, as for Lean and DAG calls. `run["analysis"]`
  stays the Monitor block. The run list shows "Analyze · <title>". The library
  and published DAG memory are never injected into an analysis; the
  operator's memory gets only its label and verdict counts, and an analysis
  never becomes shared DAG memory.
- **Success.** Exit 0 with `analysis/report.md` and `analysis/analysis_graph.json`
  published; nothing is promoted to `proof.md`. The outer watchdog is the
  run's deadline plus one API call plus 300 s (`AGENT_MONITOR_CLI_TIMEOUT`
  does not apply; the caps above are the knob). The derived-views pipeline
  waits at least that long for an analysis to end
  (`AGENT_MONITOR_AUTO_PIPELINE_TIMEOUT` only raises it). Stop sends SIGTERM
  first, so the runner publishes what had settled.
- **Environment.** The runner gets the run's route exactly as `math_harness`
  does (the sponsored broker token is issued under the `math_harness` scope),
  the console data directory (`AGENT_MONITOR_DATA_DIR`, for the shared arXiv
  slot) and, for Analyze only, `AGENT_MONITOR_ANALYZE_GITHUB_TOKEN`, which is
  scrubbed from every other child process. An own-key route never carries a
  broker token. Guests always run with `AGENT_MONITOR_GUEST=1` and 8,192
  output tokens per call.
- **HTTP API.** `POST /api/analyze/resolve {url}` previews a link (metadata
  only, at most 10 s; guests 10 per hour and 20 per network, members 30,
  administrators 120). `GET /api/run/<id>/proof_graph?kind=analysis` returns
  the manuscript graph; `kind=informal` its deterministic projection (an
  analysis's DAG is never regenerated from its report).
  `POST /api/run/<id>/analysis/recheck {node_id}` re-checks one result in the
  background (owner or administrator; guests spend a re-check of their
  allowance and a derived action, members a run start); the outcome lands in
  `run["analyze"]["recheck"]`.
  `POST /api/run/<id>/lean_verify {action: "verify", targets: [...], prove:
  false}` formalizes the statements of the selected results (default: the
  main results) on the server's Lean 4.14 / Mathlib checker: "Statements
  compiled · proofs not attempted" (`scope: "statements"`; Compile and Check
  keep that scope); upstream Lean is linked and not re-checked by this server. Continue and chat answer 400
  "Analyze runs can't be continued. Start a new analysis, or re-check a node
  in the DAG." Compilation (`/pdf`, `final.pdf`, `final_latex`) is refused:
  the TeX is a third-party manuscript.
- **What leaves the server.** The Sandbox shows `analysis/report.md`; the owner
  can open the stored source file, never anyone else. A shared snapshot holds
  the report, the Manuscript DAG (node statements, labels, verdicts:
  "checked by a model verifier", never "verified") and the source link, never
  `analysis/source/**`, proof excerpts or agent transcripts.

- **Stages.** 1 Ingest (`analysis/source/`, `manuscript.json`), 2 Segment
  (`segments.json`: LaTeX via pylatexenc, Markdown/text by headers, PDF text
  via pypdf), 3 Check (one paper-map call, then one verifier call per result,
  main results and their dependencies first; proofs over the prompt budget
  are checked in parts), 4 Roll-up, 5 Report (`analysis_graph.json`,
  `report.md`, `receipt.json` written last, and the `proof_graph.json`
  projection). `nodes/<id>.json` keeps each result's verifier reply.
- **Verdicts.** `checked`, `gap`, `error`, `unclear`, `not_checked`, with a
  closure verdict over each result's dependencies (definitions, remarks and
  other statements without a verdict pass it on; no closure is given when
  the model, not the parser, located the statements). A reply that cannot be
  interpreted stays `unclear` with `interpreted: false` and never counts as
  checked (`coverage.uninterpretable`). They are model judgements: receipts
  say `certificate: false`, and the report names the model and route.
- **Structure.** Proofs attach by `\ref`, by number ("Proof of Theorem 1.1")
  or to the preceding statement; prose proofs are recognized by their start
  and end sentences ("In this part we deduce Theorem~\ref{..}", "This proves
  Theorem~\ref{..}"); a result whose proof the parser cannot locate is
  reported as "No proof was located by the parser", never as unproved.
- **Nothing runs.** Fetched TeX, `.py`, `.cpp` and notebooks are never
  compiled or executed (archives keep manuscript files only and list the
  rest). The LaTeX parser, the PDF text extractor and the Markdown/text
  segmenter run in isolated subprocesses (`python -I`, an empty working
  directory, no credentials in their environment) with a 1 GiB address
  space and 60 s (PDF 90 s) of CPU; Stop kills them at once.
- **Fetches.** `agent_monitor/safe_fetch.py`: HTTPS on 443 to public
  addresses only (the connection is pinned to the checked address), at most
  three revalidated redirects, 25 MiB per file, 30 MiB and 120 s per source,
  and at most 40 s per request including TLS and headers (a watchdog closes
  the socket; Stop does too). arXiv requests start at least 3 s apart, across
  every runner and the console when they share
  `$AGENT_MONITOR_DATA_DIR/cache/analyze-arxiv-slot` (the runner sets this;
  the console sets `analyze.sources.ARXIV_SLOT_PATH = "auto"`). GitHub refs
  resolve through the git ref advertisement, so only a folder listing (or a
  repository whose ref list exceeds 4 MiB) uses the REST API. Set
  `AGENT_MONITOR_ANALYZE_GITHUB_TOKEN` to lift the 60/hour limit; it is sent
  only with those connector calls (never with a pasted link, and links to
  `api.github.com` or `codeload.github.com` are refused), and a folder is
  listed only for a repository whose ref advertisement answers anonymously
  (a public one). Use a
  **fine-grained token limited to public repositories with no permissions**:
  a classic token, or one that can read private repositories, would let the
  connector read whatever the token can. Guests may use arXiv and GitHub
  links only.
- **Parser packages.** A run whose venv lacks `pylatexenc` or `pypdf` stops
  with a configuration error (exit 2) naming the package, before any model
  call.
- **Exit status.** 0 published, 2 configuration, 143 stopped (settled
  results are still published), 1 failed (also when no attempted check
  produced a verdict: provider errors, empty answers, uninterpretable output
  or the time limit; the structure is still published).
- **Run record.** `agent_monitor.analyze.record.run_block(workspace)` builds
  the `run["analyze"]` block (source, caps, focus, summary) from the
  workspace after the runner exits or after a re-check.
- **Re-check one result.** `analyze_runner.py --recheck <node_id>` in the run
  workspace, with the run's route environment: at most 8 calls and 15
  minutes; the receipt lists every re-check. It prints
  `{"type": "analyze.recheck", "result": {...}}` and exits 0; a re-check that
  yields no verdict keeps the previous result and exits 1, and a stopped one
  changes nothing (143).
- **Evaluation.** `scripts/eval_analyze.py` runs the withdrawn openai/math
  Weil-classes paper (archived revision `adc7f12`) and control papers;
  `--dry-run --local NAME=DIR` plans without model calls or network.
- **Dependencies.** `pylatexenc==2.11` and `pypdf==6.20.0` (pinned in
  `pyproject.toml`) must be installed in the shared venv.

| Variable | Default |
|---|---|
| `AGENT_MONITOR_ANALYZE_ENABLED` / `AGENT_MONITOR_ANALYZE_GUESTS` | on / on |
| `AGENT_MONITOR_ANALYZE_SPONSORED_RUNS_PER_DAY` | 40 (0 turns sponsored Analyze off) |
| `AGENT_MONITOR_ANALYZE_SPONSORED_CONCURRENCY` | 2 (1-8) |
| `ANALYZE_CMD` | unset (an override removes Analyze from guests and members) |
| `AGENT_MONITOR_ANALYZE_<PROFILE>_MAX_NODES`, `_MAX_CALLS`, `_MAX_SECONDS` (`GUEST`, `MEMBER_SPONSORED`, `MEMBER_OWN_KEY`, `ADMIN`) | 12/16/2400, 30/36/3600, 120/140/3600, 256/300/14400 (ceilings 256/300/14400) |
| `AGENT_MONITOR_ANALYZE_CONCURRENCY` | 3 (sponsored and guest at most 2, subscriptions 1) |
| `AGENT_MONITOR_ANALYZE_API_TIMEOUT` | 300 s (630 s on sponsored Kimi) |
| `AGENT_MONITOR_ANALYZE_GITHUB_TOKEN` | unset |

## Research atlas and shared memory

The Problems catalogue at `/problems` (`/research/open-problems.json`, schema 3)
lists every Erdős problem (all states), every problem of the Kourovka Notebook’s
main section (arXiv:1401.0300v48), the seven Millennium problems, Hilbert’s and
Smale’s lists, Ramanujan’s legacy and researched entries of Wikipedia’s list.
The old `/open-problems` URL is a redirect stub that keeps `?source=` and
`?problem=`. Each entry has one category — solved, AI proposed, partial or open
— with rule codes, badges and evidence; AI proposed never counts as solved.
Categories come from operator decisions, then source-of-record markers (teorth
state, Kourovka ∗/♯, the Clay listing), then research that cites accepted
evidence, then curation; see `docs/problem-sources.md`. Inputs:
`agent_monitor/data/problem_sources/*.json` (pinned source facts, refreshed by
`scripts/refresh_problem_sources.py`), `open_problem_curation.json`,
`problem_status_research.json` and `problem_status_decisions.json`.
`scripts/build_problem_catalogue.py` writes the three identical copies and the
bundled empty overlay deterministically and refuses to drop a published id.
Publishing a rebuilt catalogue is a static docs release (commit, `npm run
docs:build`); the console does not read the catalogue. “Click to solve”
preserves an existing account or opens a guest workspace, loads source context,
and waits for the visitor to choose Prove.

## News refresh

The News page at `/news` replaces Overview in public navigation. The combined
platform tour and getting-started guide are at `/quickstart`; `/overview` is
kept as a compatibility redirect.

`ansatze-news.timer` (optional; `deploy/ansatze/install.sh --with-news`) runs
`scripts/update_math_news.py` from the checkout on the hour and half-hour, so a
change to `agent_monitor/news*.py` or `agent_monitor/data/news_*.json` is live
at the next tick: apply such changes right after a tick, or stop the timer
while you do. The `ansatze-news.service` worker runs as a dedicated
`ansatze-news` system user that sees only the checkout (read-only) and its
interpreter. It stores its feed and source-cache state under
`/var/lib/ansatze-news/`. Caddy serves `/var/lib/ansatze-news/news.json` at
`/research/news.json` whenever that file exists, independently of the static
website release; until the first refresh publishes it, the snapshot bundled
with the docs build is served. Rebuilding VitePress cannot replace a live feed
with an older snapshot. The page polls this read-only endpoint every three
minutes.

Sources (`agent_monitor/data/news_sources.json`, schema 2; at most 40) are:

- RSS and Atom feeds of labs, journals, journalism and mathematicians' blogs.
  Each source has a role (primary source, journalism, commentary, preprint,
  repository), a minimum check interval, a per-source item cap, keyword lists
  for titles, for excerpts and for the feed's own categories, and
  deterministic category rules (an OpenAI post about a theorem is filed under
  Mathematics). An excerpt-only match from a source with `triage` set waits
  until Kimi K3 includes it. Blog posts (role commentary) appear only as
  related coverage inside a story; a post that belongs to no story stays in
  the cache, unpublished (a reviewed seed record from a blog is always
  shown). Sources with `excerpt_fallback: false` (the Kourovka
  Notebook editors' feed) show only a headline until Kimi K3 has summarized
  the post. E-mail addresses are removed from all source text.
- The openai/math repository history (`type: changelog`): one item per dated
  `## Month D, YYYY` section of `history.md`. A section is published only once
  it is pinned to the commit that added it: a commit of the `history.md`
  commit feed, dated from 14 hours before the stated date to two days after
  it, where `history.md` at that commit (read from `revision_url`) has the
  section and `history.md` at the previous commit of the feed does not. A pin
  is made once and kept in `state.json` (and recovered from `news.json` if the
  state is lost), so commits that later leave GitHub's short commit feed
  change nothing. A section that cannot be pinned is held and listed in the
  journal line (`held_changelog`: section, time first held, reason). A
  `waiting` section is retried every run; a `before-window` section is older
  than the commits in its window and stays held, so publish it as a reviewed
  seed record if it matters. A "Withdrawals" sub-heading makes a section a
  Correction, otherwise it is an Update. Section text is source text; only its
  summary may come from the model, and every number in that summary must
  appear in the section.
- arXiv: RSS listings keep earlier daily batches for 30 days (`retention:
  rolling`) and take new and cross-listed announcements only; one arXiv
  search-API query (at most every three hours) adds AI-assisted and formal
  mathematics preprints with their first-version dates. Versioned and
  unversioned links are one item.
- Evidence-only commit feeds (`publish: false`): the `history.md` commits
  (kept for 90 days; a changelog's evidence feed must not filter commits by
  keyword), and google-deepmind/formal-conjectures commits whose titles name a
  catalogue problem (recorded as `formalization` hints, below).

Story rules at the end of the same file group reviewed members (the
openai/math release, its updates and the reaction to it) without the model.

Kimi K3 works through the loopback broker as client `ansatze-news` (its own
hashed client address), with reasoning effort "low", no tools, and strict JSON
replies tied to supplied ids. Its tasks are summaries (at most six per call;
opinions attributed to their authors; every number must appear in the source
text), triage (include or exclude borderline items; importance 0 to 3; one
topic), changelog summaries, problem links (candidate problems come from the
names and numbers in `docs/public/research/open-problems.json`; the model
chooses a relation and must quote the source verbatim) and story grouping (a
story needs two published items, one of them a primary source, journalism or
repository record). The model never writes titles, URLs, dates, categories,
statuses or links; a malformed reply is discarded whole, and an overlong or
unsupported summary row only for that item. Model text is published only if it
is already plain text: links, markup, TeX and character references such as
`&lt;` are rejected. A strong item that triage excludes keeps its place without
a model rating, and the model rates a preprint or a blog post at most 1, so
their own text cannot lift them into the Highlights strip (importance 2 or
more). A run makes at most four calls and a UTC day at most
`ANSATZE_NEWS_MODEL_CALLS_PER_DAY` (default 60), counted in `state.json`,
because the broker's own counters reset when it restarts; 0 turns the model
off. A run records its possible calls in `state.json` before making any, so a
failed publication or a killed run still counts them, and it starts model
stages only within its first 10 minutes. `--dry-run` never calls the model.
Set the cap with `sudo systemctl edit ansatze-news.service` (`[Service]` and
`Environment=ANSATZE_NEWS_MODEL_CALLS_PER_DAY=…`). A failed task backs off for
30 minutes, doubling up to a day (with five minutes of slack for the timer's
start jitter). A task skipped without a call (cap or deadline reached, or the
broker refused an earlier task of the same run) is not counted as a failure.
Without the model, or while a task backs off, cards show headlines and quoted
source excerpts, and items wait for summaries. Problem links point to
`/problems?problem=<id>` and never change a problem's status.

The worker validates configured HTTPS sources, dates, redirects, article
links, duplicates, size limits, and relevance rules. It accepts gzip bodies up
to the size limit and retries a source once when a cache sends an unexpected
encoding. Source failures preserve last-good entries, and the page reports
source-check and publication dates separately; the journal line lists failing
source ids (`source_error_ids`) and model task errors. A summary belongs to
the content it was written from (title and excerpt, without arXiv's "Announce
Type" prefix), so a re-announced preprint keeps it and a corrected record gets
a new one, at most twice a week. Editorial seed records remain unchanged by the
model. Automatic arXiv entries show the source headline while a summary is
pending. Proof announcements do not become certified solutions.

Outputs under `/var/lib/ansatze-news/`:

- `news.json` (0644, schema 1): items, sources and top-level `stories`. Items
  may carry `importance`, `topic`, `problem_links`, `story_id`,
  `source_role`, `date_precision`, `updated_at` and `summary_source`
  (`editor` for reviewed seed records, `model` for Kimi K3, `source` for the
  source's own text); statuses include Update and Correction. The page labels
  every summary with its origin and shows source text as a quotation. A story
  is filed under its lead's category and moves up the list with its lead,
  primary sources, journalism and corrections, not with blog posts.
- `status_proposals.json` (0644): unreviewed hints for the problems updater.
  These are problem links of published items (`matched_by: kimi-k3`) and
  `formalization` hints from formal-conjectures commit titles
  (`deterministic`). They are kept for 90 days, at most 500, and are never a
  status change. Caddy does not serve this file.
- `state.json` (0600): source caches, summaries, triage, links, stories,
  changelog pins, task back-off and the daily model budget.

Reviewed seed records (`agent_monitor/data/news_seed.json`) include the
openai/math release of October 6, 2026 (722 manuscripts, 719 after the October
7 update) and that update as a Correction, linked to commits `adc7f12` and
`fd4aeeb`. Their facts are taken from the repository at those commits; the
reaction is the AHM statement, which the release card links.

Check a change before it reaches the timer by running the candidate code from
a copy against copies of the worker's files (as `ubuntu`; the state file is
private to the worker, so copy it first). The script imports the code next to
it, so the copy runs, not the live checkout that the timer uses:

```bash
mkdir -m 700 /tmp/news-check
cp -a <the patched tree> /tmp/news-check/code
sudo install -m 600 -o ubuntu /var/lib/ansatze-news/state.json /var/lib/ansatze-news/news.json /tmp/news-check/
cd /tmp/news-check/code
/home/repo/agent_monitor_math/.venv/bin/python scripts/update_math_news.py --output /tmp/news-check/news.json \
  --state /tmp/news-check/state.json --seed agent_monitor/data/news_seed.json --no-model
```

This writes only the copies (including `/tmp/news-check/status_proposals.json`)
and prints the journal line. Add `--dry-run` to write nothing. The one-time
backfill after this release fetches each source's `backfill_url` (the arXiv
search over about 30 days) and triages more than usual. Preview it with
`--backfill --dry-run` on the copies, then run it once as the worker (stop the
timer first; the runtime override is gone after a reboot):

```bash
sudo systemctl stop ansatze-news.timer
sudo systemctl edit --runtime ansatze-news.service
#   [Service]
#   ExecStart=
#   ExecStart=<the unit's ExecStart line> --backfill --max-model-calls 12
sudo systemctl start ansatze-news.service
journalctl -u ansatze-news.service -n 3 --no-pager
sudo rm -r /run/systemd/system/ansatze-news.service.d && sudo systemctl daemon-reload
sudo systemctl start ansatze-news.timer
```

The backfill run counts its 12 possible calls before making any, stops at
the daily model cap, and starts model stages only within its first 10 minutes,
so it ends inside the unit's 15-minute start timeout; items it could not
summarize or triage are handled by the next scheduled runs.

Maintained inputs: `agent_monitor/data/news_sources.json` and
`agent_monitor/data/news_seed.json`. Code: `agent_monitor/news.py`,
`agent_monitor/news_kimi.py`, `agent_monitor/news_links.py`,
`agent_monitor/kimi_tasks.py` and `scripts/update_math_news.py`.

Check scheduler health with `systemctl status ansatze-news.timer` and
`systemctl status ansatze-news.service`; inspect worker results with
`journalctl -u ansatze-news.service`. Run a manual check with
`systemctl start ansatze-news.service`. To pause automatic collection, stop
`ansatze-news.timer`; the last published feed remains available. Public refresh
metadata shows whether source checks are current. Never put provider keys in
feed records, static files, command arguments, or service-unit environment.

## Problem status refresh

`ansatze-problems.timer` (optional; `deploy/ansatze/install.sh
--with-problem-status`) runs `scripts/update_problem_status.py` every six hours
(00:15, 06:15, 12:15, 18:15 UTC plus up to five minutes) as the
`ansatze-problems` system user, with the news worker’s sandbox. It publishes
`/var/lib/ansatze-problems/problem-status.json` (0644, served by Caddy at
`/research/problem-status.json` with `Cache-Control: no-cache`; until then the
empty overlay bundled with the docs build is served) and keeps `state.json`
(0600: HTTP validators, upstream snapshots, the signal ledger, the model budget)
beside it. The output is written before the state.

The overlay is computed against the checkout’s catalogue (`catalogue_sha256`)
and never adds or removes entries. Category changes come only from deterministic
source signals: a teorth state change, Kourovka markers in a newer arXiv
version, an openai/math withdrawal of a manuscript an entry cites, or a Clay
listing change; a row whose category a reviewed decision set keeps it and gets
“Sources disagree” instead (unless the source now agrees with the decision).
arXiv (every named problem searched about once a week, each search looking back
to the previous one; model only), Palomar, formal-conjectures commits (and
research-category changes of the Erdős files they touch; a source note says when
more commits landed than the feed lists), linked Wikipedia articles and the
public `news.json` problem links become “New reports (unreviewed)”. Wikipedia
reports carry no article text (the quote stays in `state.json`), wait 12 hours
before triage and are withdrawn when the edit is reverted. Each source fails
closed and keeps its last good result; errors are listed in `source_errors`,
including `kimi-k3` when the broker is down or refusing. Kimi K3 (client id
`ansatze-problems`, through the loopback broker) only labels candidate matches
with a verbatim quote: at most 8 calls per run and
`ANSATZE_PROBLEMS_MODEL_CALLS_PER_DAY` (default 48; `0` disables) per UTC day,
counted in `state.json` because the broker’s own counters reset on restart. It
backs off for the rest of a run when the broker is busy or over quota; arXiv
entries it could not classify wait for the next run.

Review with `scripts/review_problem_status.py list --overlay
/var/lib/ansatze-problems/problem-status.json`, then `show`, `accept`, `set`,
`reject` or `revoke`; decisions go to
`agent_monitor/data/problem_status_decisions.json` and take effect after
`scripts/build_problem_catalogue.py`, a commit and a docs build; `show --state
/var/lib/ansatze-problems/state.json` prints a Wikipedia report’s private quote.
A dry run on copied state: `scripts/update_problem_status.py --output
/tmp/x/out.json --state /tmp/x/state.json --dry-run` (a dry run writes nothing
and never calls the model; to try the model, run without `--dry-run` against
copies). `--only <sources>` checks some sources; the others keep their last
published entries and status.

Two source notes need a person. A newer Kourovka edition whose parse loses
problems or ∗/♯ markers in bulk (or gains markers in bulk, or grows parse
anomalies) is refused with a
`kourovka` source error: check the PDF, then rebuild the fact file with
`scripts/refresh_problem_sources.py --kourovka-pdf … --force` and the catalogue.
An `openai-math` note about withdrawals that match no manuscript means
`history.md` lists a title missing from the pinned contents (added later, or
worded differently); the matched withdrawals still apply. Refresh
`openai_math.json` (with `--openai-previous-commit` for an intermediate
revision if needed) and rebuild.

Check with `systemctl status ansatze-problems.timer`,
`journalctl -u ansatze-problems.service` (one JSON line per run: entries,
signals, source errors, model calls) and `curl -sI
https://ansatze.ai/research/problem-status.json`. Stop the timer to pause; the
last overlay stays published. A newer Kourovka edition needs pypdf in the venv
(`pyproject.toml` pins it); without it that source reports an error and the
others continue.

## Research atlas

The default atlas at `/dag` displays an informal–formal bipartite graph imported
from the supplied `graph.json` and identical KG ZIP archives. It includes 24,950
informal entries, 16,682 real Lean declarations and 16,761 resolved connections.
Unresolved references are optional and visibly separate.
The **Show dependencies** toggle loads 54,128 informal references and 72 proof
links from `/research/bipartite-dependencies.json`, preserving the supplied
direction and labels. Formal-to-formal dependencies are absent from this input.
Dependency review evidence is in `artifacts/atlas-dependencies-2026-09-15/`.
Reproduce the public
index and lazy source details with `scripts/import_bipartite_atlas.py`; use
`--check` to verify all generated artifacts. Review evidence and the previous
static release are in `artifacts/bipartite-atlas-2026-09-15/`. Publish static
updates with the existing atomic `npm run docs:build`; no service restart is
needed. The source graph remains available at `/dag?view=source`.

The original informal research atlas imports eligible active tagged mathematical
statements from the pinned Stacks Project source without a node-count cap.
Disconnected statements remain visible; only actual source citations become
arrows. Coverage counts, deliberate exclusions, the pinned source, import method,
and licenses are documented at `/dag-sources`; reproduce the data with
`scripts/import_informal_atlas.py`. The ansatze.ai Caddyfile compresses
responses and serves the prebuilt `research/*.json.gz` files directly. The catalogue is source literature, separate from model
memories. It is never labeled as model-generated work.

The expanded catalogue and independently checked source provenance are recorded
in `artifacts/atlas-expansion-2026-09-10/`. Updating its static backend index uses
the hash-bound `scripts/promote_atlas_index.py` without restarting the console;
the running backend does not load that catalogue into model memory.

Shared model memory currently requires explicit publication by the completed
run's owner. The final assistant output must have recorded response-model
identity from the eligible GPT-6, GPT-5.6 Sol, or Fable model list. Session
summaries and selected model names do not establish authorship. API Plain,
API Math Harness, and Claude response streams preserve this identity. Native
Codex CLI output currently omits it, so those results cannot be published as
shared memory. Existing runs without response provenance are also ineligible.
Registered harness runs retrieve relevant published memories automatically;
guest and sponsored public runs do not. Publication remains an owner action.

## Claude Code pinning

The console runs Claude Code with `DISABLE_AUTOUPDATER=1`, but the binary it
finds on the service `PATH` was `~/.local/bin/claude`, a symlink into the
standalone `~/.local/share/claude/versions/` tree that the owner's own
interactive Claude Code sessions update in place, so the console's Claude Code
version could change without a deploy. Pin it like Codex, user-level and first
on the service `PATH` (the unit template puts `~/.npm-global/bin` first):

```bash
PATH=/home/ubuntu/.local/node24/bin:$PATH \
  npm i -g --prefix ~/.npm-global @anthropic-ai/claude-code@2.1.283
~/.npm-global/bin/claude --version       # 2.1.283 (Claude Code)
```

2.1.283 is the build the live console used on 2026-09-27. After pinning, run
one Claude-subscription Plain run and one Math Harness run before relying on
it, and record any newer vetted build here before switching.

## Codex vetting

The hosted response-only Codex adapter is vetted against exactly
`codex-cli 0.153.4`, `codex-cli 0.157.1` and `codex-cli 0.160.0`
(`RESPONSE_ONLY_CODEX_CLI_VERSIONS` in `agent_monitor/runners/codex_backend.py`);
any other build makes Codex-subscription Plain, Math Harness and derived
Lean/DAG calls fail closed with an error naming the binary it found. Install the
pinned build user-level and keep it first on the service `PATH` (the unit
template puts `~/.npm-global/bin` first; engine discovery searches it first):

```bash
PATH=/home/ubuntu/.local/node24/bin:$PATH \
  npm i -g --prefix ~/.npm-global @openai/codex@0.160.0
~/.npm-global/bin/codex --version        # codex-cli 0.160.0
```

Do not rely on `~/.local/bin/codex`: it points at the standalone
`~/.codex/packages/standalone/current` tree, which can self-update past the pin.
To re-vet a build, put it first on `PATH` and run
`tests/test_codex_response_only_contract.py`; its live test drives the real CLI
against a loopback capture provider and fails if any tool reaches the model,
either in the top-level `tools` field or inside an `input` item
(`additional_tools`). Only then add the version to the exact pin set. The
adapter's contract is `RESPONSE_ONLY_TOOL_ISOLATION_VERSION = 5`; Math
Harness receipts record it, and Codex-route receipts that claim version 4 were
written before the catalog fix (when "responses lite" models could still
receive code-mode and multi-agent tools), so they are no longer attested.
After a
deploy, run one real Codex-subscription Plain run on `gpt-5.6-sol` and one on
`gpt-5.5` and confirm both complete (the capture provider cannot show whether
the real backend accepts the tool-free request shape). Tests and narrow
hosted-source patches for the earlier release are retained in
`artifacts/dag10k-update-2026-09-10/`. The older workspace backend is not a
drop-in replacement for the hosted backend.

## Find the live wiring

| Component | Where |
|---|---|
| Code and virtualenv | `/home/repo/agent_monitor_math` (this checkout, `.venv` on Python 3.13) |
| Console | `agent-monitor.service`, user `ubuntu`, `127.0.0.1:4600`, data in `/var/lib/ansatze/console` |
| Kimi key holder | `ansatze-kimi.service`, user `ansatze-kimi`, `127.0.0.1:4610`, data in `/var/lib/ansatze/kimi` |
| Credentials | `/etc/ansatze/credentials/` (root 0400; loaded with `LoadCredential=`) |
| Console settings | `/etc/ansatze/console.env` (root-managed, no provider keys) |
| Console mount | `/etc/ansatze/mount` |
| Edge | `/etc/caddy/Caddyfile`; backups in `/etc/ansatze/backup/` |
| Lean checks | `/run/agent-monitor-lean/{core,mathlib}.sock` (see `deploy/lean-broker/`) |
| Static site | `/home/repo/agent_monitor_math/docs/.vitepress/dist` |

On the host, inspect the effective configuration instead of guessing:

```bash
sudo systemctl cat agent-monitor.service ansatze-kimi.service
sudo systemctl show agent-monitor.service \
  -p FragmentPath -p DropInPaths -p WorkingDirectory -p ExecStart
sudo sed -n '1,240p' /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
deploy/ansatze/smoke.sh
```

A Python change in this checkout is live only after
`sudo systemctl restart ansatze-kimi agent-monitor`; imported modules stay in
memory until then. Edge or unit changes go through the templates in
`deploy/ansatze/` and `sudo deploy/ansatze/install.sh --steps render,services,caddy`.

## Publish documentation

Install dependencies once with `npm ci`, then publish with:

```bash
npm run docs:build
```

The build is rendered into a staging directory and atomically exchanged with
the directory Caddy serves, so readers do not see a half-built site. The swap
uses `mv --exchange` where available (GNU coreutils 9.5+), otherwise the Linux
`renameat2(RENAME_EXCHANGE)` call, and only as a last resort two renames.

After changing routing, validate before reloading Caddy and smoke-test the
public surface:

```bash
sudo deploy/ansatze/install.sh --steps render,caddy
curl -I https://ansatze.ai/
curl -I https://ansatze.ai/news
curl -I https://ansatze.ai/sign-in
deploy/ansatze/smoke.sh --www --public
```

Do not put API keys, password-reset tokens, the console mount, or DNS provider
credentials in this file. Production secrets are loaded through encrypted
systemd credentials; local development values belong only in ignored local
configuration.

# TODOS

## CSP-iterate (APM-native iterate/watch over CSP)

**What:** Implement `iterate <app>` and `watch <app>` semantics natively in
satdeploy-apm (C, inside CSH). Same wedge as the Python CLI provides over SSH:
push + bsdiff patch + health check + log streaming. Runs over CSP, uses existing
DTP file transfer, writes history.db through the APM's existing writer.

**Why:** CSP is the space-industry default transport (GomSpace, Space Inventor,
Space Cubics — all maintainers of libcsp, all on Julian's pilot lead list). SSH
doesn't work in orbit and rarely works on pre-flight CubeSats with CAN-only
hardware. The iterate wedge is value-transport-independent; teams without SSH
access currently get the pre-wedge experience (bare `satdeploy push` over CSH,
no fast edit-to-running loop).

**Why not now:** Decided 2026-04-22 via /plan-ceo-review. Three reasons: (1)
Reversing cd38042 (Python+CSP) is wrong — that architectural decision was
load-bearing and ongoing maintenance tax was the reason it shipped. (2) Building
APM-iterate in C burns ~1-2wk against a 0-slack thesis timeline and re-opens
every eng-review landmine (P0 #1-4) in a harder language. (3) Phase 0 exists to
validate the wedge with pilots, not to ship full coverage. Julian outreach is
the cheapest way to learn whether CSP teams will pay — if ≥1 pilot says "we need
this on CSP," that's pre-sold Phase 1; if they shrug, you saved the 2 weeks.

**Context:** The APM already owns CSP (per 2026-04-17 "Python=SSH, C=CSP Boundary"
plan). It already has `satdeploy push/status/rollback/list/logs` working over
CSP/DTP and writes to the shared history.db. Adding iterate is additive: agent
receives DEPLOY command, applies bsdiff patch, health-checks via libparam, logs
back through CSP. Watch would live on the ground side in CSH — watchdog-style
file monitoring firing iterate on save. Same semantics, different transport.

**Gating signal:** ≥1 Julian outreach pilot explicitly asks for CSP support, OR
a Phase-0 evaluation reveals CSP-only teams can't use the tool at all. Landscape
research (2026-04-22) confirmed CSP teams desperately need the STICK (rollback
+ audit + validate) more than the wedge; the wedge is the pitch-winner.

**Effort estimate:** human ~2 weeks / CC ~2-3 days of concentrated C work.
Reuses existing DTP + history writer + backup logic in agent. New surface: port
the bsdiff-patch apply path from satdeploy/bsdiff.py to C, add iterate command
to APM slash command dispatcher, wire to existing agent DEPLOY handler.

**Priority:** P2 — important but Phase 1 (post-thesis submit end-June 2026).
Revisit after Julian's pilot responses land.

**Depends on:** bsdiff patch format stability (already locked via `bsdiff4==1.2.3`
pin + Week 1 feasibility test). APM history.db writer (already shipped per
Phase 2 of 2026-04-17 plan).

## C Unit Test Framework for satdeploy-agent

**What:** Set up cmocka or Unity test framework for satdeploy-agent C code.

**Why:** Agent has zero unit tests. Hash migration (SHA256) and backup naming changes
can only be verified manually on the FlatSat. Regressions in C code are caught late.

**Context:** Agent is ~500 lines across 4 source files (deploy_handler.c,
backup_manager.c, app_metadata.c, dtp_client.c). Would need mock implementations
of libcsp socket operations, libparam get/set, and DTP client API. cmocka is the
standard choice for embedded C testing.

**Depends on:** Nothing — independent of all other work.

## DTP Server Tests

**What:** Add pytest tests for `satdeploy/csp/dtp_server.py`.

**Why:** The DTP server handles ground-to-satellite file transfer. It's the only
Python module with zero test coverage. A bug here means failed deploys over CSP.

**Context:** dtp_server.py is 244 lines. Key behaviors to test: metadata request
handling, chunked data sending, timeout handling, server start/stop lifecycle.
All mockable with pytest-mock (mock ZMQ sockets and threading).

**Depends on:** Nothing — independent of all other work.

## Demo Video for Outreach

**What:** Record a 2-minute screencast showing a real deploy + rollback on FlatSat hardware over CSP.

**Why:** Video proof of working satellite OTA on real hardware is the single strongest credibility
asset for cold outreach. A satellite engineer watching this gets more conviction in 2 minutes than
reading any README. Do after first 3 outreach conversations to learn what to emphasize.

**Context:** Record on the DISCO-2 FlatSat. Show: (1) `satdeploy push` deploying a file over CSP,
(2) `satdeploy status` showing the new version, (3) `satdeploy rollback` restoring the previous
version. Keep it under 2 minutes, no narration needed — terminal output is compelling enough.

**Effort:** S (human: ~2 hours for setup + recording)

**Priority:** P2 — after first 3 outreach conversations

**Depends on:** Repo must be public first.

## Benchmark Data in README

**What:** Measure deployment time, bandwidth efficiency, and rollback speed on real FlatSat hardware
over CSP. Add concrete numbers to README.

**Why:** Satellite engineers think in link budgets and pass windows. Concrete numbers (e.g., "2MB
file in 45s over 9600 baud, rollback in 3s") speak their language and make the positioning
tangible. Abstract feature lists don't convert.

**Context:** Run on DISCO-2 FlatSat. Measure: deploy time for various file sizes (500KB, 1MB, 2MB,
5MB), rollback time, bandwidth utilization. Present as a table in the README under a "Performance"
section.

**Effort:** S (human: ~1 hour on FlatSat + CC: ~15 min to format)

**Priority:** P2 — after first outreach conversations reveal which metrics matter most

**Depends on:** Nothing — can be done anytime with FlatSat access.

## Bugs Found — End-to-End QA (2026-03-22)

Tested full pipeline: pytest → CLI smoke → Docker C builds → CSP end-to-end
(zmqproxy + agent + CSH/APM). The APM→agent path works. Python CLI CSP path
does not.

### BUG-001: Python CSP transport uses wrong ZMQ pattern [CRITICAL] — FIXED

**Component:** `satdeploy/transport/csp.py`

**Problem:** The Python CSP transport used a `ZMQ_DEALER` socket connecting to
`tcp://localhost:4040`. The real libcsp stack uses `ZMQ_PUB`/`ZMQ_SUB` through
zmqproxy on ports 6000 (publish/tx) and 7000 (subscribe/rx). These are
incompatible ZMQ patterns — the Python CLI could not communicate with the real
satdeploy-agent.

The status command appeared to work but actually timed out silently and returned
an empty dict, which the CLI rendered as "not deployed" for all apps.

**How to reproduce (before fix):**
```bash
# 1. Start Docker with agent
docker start cshdev
docker exec -d cshdev zmqproxy
docker exec -d cshdev /satbuild/satdeploy-agent/build/satdeploy-agent

# 2. Try Python CLI (will hang/timeout)
satdeploy status --config-dir /tmp/satdeploy-test

# 3. Compare with APM via CSH (works)
# In Docker with PTY:
#   csh -i /csh/init/zmq1.csh
#   apm load
#   satdeploy status -n 5425
```

**Root cause:** `csp.py` used `zmq.DEALER` (request/reply pattern). libcsp uses
`zmq.PUB` + `zmq.SUB` (broadcast pattern) through zmqproxy (XSUB/XPUB forwarder).
Wire format: raw CSP packets (4-byte header + payload), first 2 bytes used as
SUB topic filter (encodes destination address).

---

### BUG-002: CSP transport disconnect() hangs [HIGH] — FIXED

**Component:** `satdeploy/transport/csp.py`

**Problem:** `disconnect()` blocked indefinitely because `zmq.Context.term()`
waits for all pending messages to be sent. The socket's default LINGER is
infinite.

**How to reproduce (before fix):**
```bash
satdeploy status --config-dir ~/.satdeploy  # Ctrl+C required to exit
```

**Fix:** Set `zmq.LINGER = 0` on sockets before closing.

---

### BUG-003: `satdeploy config` documented but doesn't exist [MEDIUM] — FIXED

**Component:** `satdeploy/cli.py`

**Fix:** Added `satdeploy config` command to the Python CLI. Displays target
name, transport settings, backup config, and all app definitions.

---

### BUG-004: APM deploy requires options before positional arg [MEDIUM] — FIXED

**Component:** `satdeploy-apm/src/satdeploy_apm.c`

**Problem:** `satdeploy deploy test_app -f /tmp/binary` failed because slash's
optparse uses POSIX-style parsing (stops at first non-option argument).

**Fix:** Added argv reordering before optparse_parse — options are moved before
positional args so `satdeploy deploy test_app -f /tmp/binary` and
`satdeploy deploy -f /tmp/binary test_app` both work.

---

### BUG-005: C compiler warnings in backup_manager.c [LOW] — FIXED

**Component:** `satdeploy-agent/include/satdeploy_agent.h`

**Fix:** Increased `MAX_PATH_LEN` from 256 to 512. Backup paths like
`/opt/satdeploy/backups/<app>/YYYYMMDD-HHMMSS-<hash>.bak` could approach
256 bytes with long app names or deep backup directories.

## Refactor `push` command to `satdeploy/push.py` module

**What:** Extract the 404-LOC `push` command body from `satdeploy/cli.py` into `satdeploy/push.py`. Leave the Click decorator + arg parsing in cli.py; call `push.run_push(ctx)` for the business logic. Mirrors the Phase 0 pattern for iterate/watch/validate.

**Why:** cli.py is 1602 LOC. Push alone is 404 LOC of inline business logic. Phase 0 establishes the clean "Click dispatches thin, modules do business logic" pattern. push is the only remaining old-style inline command. Without this refactor, new contributors see inconsistent patterns (iterate.py is clean, push inline) and future command additions get pulled toward the wrong model.

**Context:** `push` at `cli.py:599-1001`. Reads config, runs Deployer, handles backup, rollback fallback, service management, history logging, dependency resolution, git provenance, and CSP node override. Extracting requires: move functions `stop_services`, `start_services`, `sync_service_file` (if push-specific) or keep them as shared helpers. Preserve all tests in `tests/test_cli_push.py` — they pass through Click runner.

**Effort:** M (human: 1-2 days / CC: ~1-2h with careful test preservation)

**Priority:** P2 — cleanup, not blocking product work

**Depends on:** Phase 0 shipped. iterate/watch/validate modules exist as reference pattern.

## Multi-target / Fleet primitive (Phase 1, post-thesis)

**What:** Parallel `iterate` across N targets, fleet dashboard tab showing app version per target, comparative status view.

**Why:** Every commercial sat operator is or wants to be a constellation. Single-target satdeploy feels dated within 18 months. Doubles pilot value prop (`manages your constellation`). CEO plan rescope (2026-04-18) deferred this from Phase 0 to keep thesis timeline viable.

**Context:** `--config` flag already partial for target switching. New work: multi-config loader, parallel orchestration (asyncio), fleet dashboard view, comparative status (sat-1 is v2, sat-2 is v1). Simulation for local dev: 2 local ZMQ agents bound to different ports.

**Effort:** L (CC: ~4-6 days)

**Priority:** P1 — biggest pilot value-add once thesis ships

**Depends on:** Phase 0 shipped.

## Drone outreach collateral (Phase 1, post-thesis)

**What:** Drone-targeted cold-email script variant, drone company email list (~10 shops: Auterion, PX4 commercial, Brinc, Shield AI, Skydio enterprise, etc.), drone-framing demo copy.

**Why:** Drones run aarch64 Linux on constrained/lossy links. SSH transport already built in satdeploy. Natural TAM expansion. CEO plan rescope (2026-04-18) deferred from Phase 1b to post-thesis per adversarial reviewer (burns Week 1-2 attention during feasibility test).

**Context:** Same satdeploy binary, different positioning. Demo gif framing changes: "for your drone fleet" instead of "for your flatsat."

**Effort:** S (CC: ~1-2 days)

**Priority:** P1 — doubles paying customer count once thesis and first sat pilot are in hand

**Depends on:** Thesis submitted, first sat pilot case study ready.

## satdeploy doctor (dependency check)

**What:** `satdeploy doctor` command that checks all shell-out dependencies (bsdiff, bspatch, gdbserver, debuginfod, objdump, rsync) are installed and version-compatible. Prints install hints per platform.

**Why:** Phase 0 adds 4-5 shell-outs which creates dependency-fragility surface. When first pilot installs satdeploy and one dependency is missing/old, cryptic shell failure is worse than typed error with install command. Surfaced in CEO review Section 10 (2026-04-18).

**Context:** Each dependency is called via `subprocess`. Centralize version/path detection in one module. Output like `apt install gdb-multiarch elfutils bsdiff`. Exit codes: 0 all good, 1 missing deps, 2 version issues.

**Effort:** S (CC: ~30-45 min)

**Priority:** P2 — reduces pilot onboarding friction

**Depends on:** Phase 0 shell-outs (bsdiff, debuginfod, etc.) shipped.

## Cloud-hosted dashboard tier (Phase 1 SaaS)

**What:** `dash.satdeploy.com` — hosted FastAPI dashboard with per-org auth, billing, fleet aggregation across customers. Opt-in via `satdeploy dashboard deploy cloud`.

**Why:** Expo/Codemagic business model — open dev tool + hosted SaaS tier. YC pitch requires a recurring-revenue story. CEO plan rescope (2026-04-18) kept Phase 0 local-only to avoid pilot compliance reviews; cloud tier is Phase 1.

**Context:** Auth (OAuth / API keys), billing (Stripe), DPA for EU customers, hosting (Fly/Render), data egress posture for sat customers (opt-in only, never default). Sensitive: some pilot customers will refuse to pipe flatsat data to third-party cloud.

**Effort:** L (CC: ~10 days)

**Priority:** P1 — business model foundation

**Depends on:** Phase 0 local dashboard shipped + 1 pilot converted to paid.

## Telemetry backchannel (Phase 1 retention hook)

**What:** Target-side daemon collects process RSS, CPU %, service up/down, last log line. Reports via CSP every N seconds. Dashboard shows live metrics per app per target. New history.db table: `telemetry`.

**Why:** Once deploys are fast (wedge), ops visibility becomes the next pain (stick). Differentiates satdeploy from pure deploy tools. Foundation for Phase 3 fleet observability. CEO plan (2026-04-18) deferred from Phase 0.

**Context:** Daemon in C on target, Python ingestion into history.db, dashboard live-update view. CSP protocol addition (new message type). Schema migration.

**Effort:** L (CC: ~5-7 days)

**Priority:** P2 — Phase 1 retention

**Depends on:** Phase 0 shipped + pilot customer explicitly requests it.

## satdeploy whoami (state legibility)

**What:** `satdeploy whoami` — one command that shows which flatsat is connected, what's deployed, last iterate timestamp, debug status. State legibility in 1 second.

**Why:** New dev installs satdeploy, wants to sanity check. `whoami` answers "am I connected?" / "what's deployed?" / "what's latest?" in one invocation. Delight item from CEO review (2026-04-18).

**Effort:** S (CC: ~30 min)

**Priority:** P3 — delight, can slip to Phase 1

**Depends on:** Phase 0 CLI foundations.

## Sysroot auto-fetch from build-artifact URL (UX polish)

**What:** Instead of requiring user to set `SATDEPLOY_SDK` env var, `satdeploy iterate` detects ABI mismatch, reads Yocto manifest hash from target, fetches matching SDK tarball from build-artifact URL, extracts to `~/.satdeploy/sysroots/<hash>/`.

**Why:** Phase 0 requires user to have local SDK matching target build. Manual. Phase 1 UX goal: zero-config onboarding per WOW principle #1.

**Context:** Build artifact URL must be set in config (`build_artifact_base:`). Yocto manifest hash already embedded in target `/etc/build-id` or similar. Download + untar ~500MB SDK. Cache.

**Effort:** M (CC: ~2-3 days)

**Priority:** P2 — onboarding UX

**Depends on:** Phase 0 sysroot sync shipped.

## Exec view dashboard ("what shipped this week")

**What:** Dashboard widget showing weekly deploy summary: apps deployed, validations passed/failed, rollback count, avg iterate time. Exec-legible metrics.

**Why:** CTO demo-to-board moment. CEO plan (2026-04-18) cut from Phase 0 as YAGNI; Phase 1 polish.

**Effort:** S (CC: ~1 day)

**Priority:** P3 — polish

**Depends on:** Phase 0 dashboard shipped + pilot explicitly asks.

## Slack/Discord webhook on iterate

**What:** Optional config key `webhook_url:` — satdeploy posts iterate events to Slack/Discord. Converts "hey did you deploy the fix?" DMs into silent group awareness.

**Why:** Delight item from CEO review (2026-04-18). Team-awareness hook.

**Effort:** S (CC: ~30-45 min)

**Priority:** P3 — delight

**Depends on:** Phase 0 shipped.

## Compliance Audit Command (`satdeploy audit`)

**What:** Export deployment history as a formatted report (markdown or PDF) for compliance documentation.

**Why:** Launch providers and insurers may require configuration management documentation
as commercial small-sat grows. A `satdeploy audit` command that auto-generates deployment
history reports from history.db would give funded startups a reason to adopt beyond "nice
dev tool." Identified during CEO review (2026-04-13) but deferred until a customer
explicitly asks for compliance docs.

**Context:** Formats existing history.db data. No new data collection needed. Output
should include: deployment timeline, per-app version history, hash verification status,
and any rollback events. Consider PDF output via weasyprint or markdown for simplicity.

**Effort:** S (human: ~1 day / CC: ~30 min)

**Priority:** P3 -- build only after validated external demand

**Depends on:** Phase 1 community launch validation. Only build if a funded startup
specifically asks for compliance/audit documentation.

**Update 2026-04-20 (landscape revision):** Markdown-only version pulled into Phase 0
as scope item R5 — specifically to answer the "enterprise-first legibility from day 1"
critique from the Thiago landscape article. PDF output (weasyprint) stays deferred
under this TODO.

## DESIGN.md / design system doc

**What:** Create a `DESIGN.md` at repo root capturing the satdeploy design system:
typography (monospace font for ticker/timeline, UI font for labels), color palette
(green/yellow/red status tiles + one accent), spacing scale, component vocabulary
(status tile, live-activity ticker row, monospace timeline table, `<details>`
collapsible, type-to-confirm modal), link/button patterns, responsive breakpoints.

**Why:** plan-design-review (2026-04-20) flagged R6 design review passed 9/10 only
because universal design principles + "reuse existing dashboard tiles" filled the
gap. Future dashboard additions (Phase 1 cloud tier, telemetry preview, fleet view)
will drift without a captured system. `/design-consultation` skill produces this.

**Context:** Run `/design-consultation` after Phase 0 dashboard ships so the doc
is written against real live components rather than imagined ones.

**Effort:** S (human: ~4-6h via /design-consultation / CC: ~1-2h)

**Priority:** P2 — Phase 1 foundation for design consistency as features expand

**Depends on:** Phase 0 dashboard + R6 shipped (need real components to document)

## `satdeploy tour` scripted walkthrough

**What:** A scripted, narrated 3-minute walkthrough (`satdeploy tour`) that runs against
a local target with zero hardware. Sets up demo, iterates a 500KB binary, runs validate,
pushes with override (showing warn prompt), deploys, breaks something deliberately, rolls
back, opens the dashboard. Between each step a brief terminal caption explains the pitch
point ("this is the wedge", "this is the stick", "this is the audit trail").

**Why:** The plan's platonic ideal says "CTO demos to board in 3 min." `tour` is that,
shippable, zero-setup. Primary pitch asset alongside the 20s cold-email gif. Proposed in
landscape revision (2026-04-20); dropped during timeline reconciliation because the 20s
gif already handles cold reach and adding the tour would stack against a 0-slack calendar.

**Context:** Wraps existing demo setup + scripted subprocess calls. Captions via
`click.echo` with `click.pause()` between stages (or `--autoplay` flag with wall-clock
timing for recorded demos). Different from existing `satdeploy demo` (which is just
environment setup). Idempotent — running twice tears down cleanly first.

**Effort:** S (human: ~4-6h / CC: ~1-2h)

**Priority:** P2 — Phase 1 pitch asset, after first pilot explicitly asks for a
narrated demo

**Depends on:** Phase 0 iterate + push + rollback + validate + dashboard all shipped.

## `satdeploy init` first-run welcome block

**What:** After `satdeploy init` completes (or on first `satdeploy`-any-command run
with an existing config), print a short "what to try next" block pointing at
`satdeploy tour`, `satdeploy iterate`, and the dashboard URL.

**Why:** First-impression leverage. Zero-config-to-first-value is what separates
survivor-shape dev tools from ones that die in the onboarding funnel. Deferred from
landscape revision (2026-04-20) because R4 `satdeploy tour` and R2 positioning are
higher-leverage for the same pitch moment.

**Context:** Similar pattern to `fly launch` post-run output, or `next create-app`'s
"next steps". 3-5 lines. Not a first-run wizard — just an `echo` at the end of relevant
commands when no history exists yet.

**Effort:** XS (human: ~1h / CC: ~30min)

**Priority:** P2 — Phase 1 delight

**Depends on:** R4 `satdeploy tour` (to be the thing we point at)

## `satdeploy feedback` command

**What:** One command that opens a pre-filled GitHub issue (or mailto) with last N
iterate events, anonymized config summary (target count, transport, backup_dir, app
names redacted), and satdeploy version. User fills in the "what went wrong" text.

**Why:** Pilot feedback loop compounds. When a mission engineer hits friction, the
distance between "this is annoying" and "Mahmood knows about it" is a `satdeploy
feedback` command, not a 20-minute issue-filing ceremony. Retention hook identified
in landscape revision (2026-04-20); deferred because survival-sample-size of pilots
is zero right now — speculative until at least one pilot is live.

**Context:** Open URL via `webbrowser.open()`. Pre-fill body with triple-backtick
diagnostics block. Never includes file hashes, hostnames, or paths — only shape
metadata (counts, types, transport name).

**Effort:** XS (human: ~2h / CC: ~15min)

**Priority:** P2 — Phase 1 retention hook

**Depends on:** At least one live pilot so feedback has a recipient worth calibrating
against.

## Ansible / deploy-script import command

**What:** `satdeploy import <path>` — reads an existing Ansible playbook, Fabric script,
or shell deploy script and generates a rough satdeploy config.yaml. Not a perfect
translation — a starting point that reduces adoption friction for pilots already
running their own ad-hoc deploy tooling.

**Why:** The Thiago landscape article's "tool fatigue / consolidation wins" pattern:
satdeploy succeeds by replacing N ad-hoc scripts, not by being the Nth tool. An import
command says "show me what you have, I'll translate it" — friction near zero. Deferred
from landscape revision (2026-04-20) because speculative — no pilot has said "how do
I switch" yet, and the parser work is real (3+ incompatible input formats).

**Context:** Start with Ansible-only (most common in flight-software shops). Parse
inventory + playbook tasks matching `copy:`, `systemd:`, and `command:` patterns.
Emit satdeploy config + a diagnostics file listing unsupported tasks the user must
translate manually.

**Effort:** M (human: ~1 week / CC: ~2-3h for Ansible-only v0)

**Priority:** P2 — adoption friction reducer, only build when a pilot asks

**Depends on:** At least one pilot that currently uses Ansible/scripts and is willing
to be the reference import target.

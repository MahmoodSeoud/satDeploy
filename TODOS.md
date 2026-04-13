# TODOS

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

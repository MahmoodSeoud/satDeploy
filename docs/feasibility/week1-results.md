# Week 1 feasibility — patch-size distribution (agent ARM binaries)

**Date:** 2026-04-21. **Target:** satdeploy-agent, cross-compiled aarch64-poky-linux, 4 successive commits covering routine debug-logging and typedef fixes. **Harness:** `scripts/feasibility_test.py --pair-dir` (pair-batch mode, extension on top of commit e48392f).

## Distribution (3 adjacent-pair bsdiffs)

| Metric          | p50       | p99       | Notes |
|-----------------|-----------|-----------|-------|
| Patch size      | 7,966 B   | 11,582 B  | Absolute bytes of the compressed bsdiff patch |
| Patch ratio     | 1.62 %    | 2.35 %    | As % of new binary size (~492 KB) |
| Compute latency | 85.3 ms   | 87.3 ms   | Python `bsdiff4==1.2.3`, x86_64 host |

Full data: [`week1-agent-pair-batch.json`](./week1-agent-pair-batch.json).

## Implication for the p50 ≤10s edit-to-running claim

At a conservative 100 KB/s sustained CSP throughput, an 8 KB patch takes ~80 ms to transfer; the full 492 KB binary would take ~5 s. Compute + transfer combined consume less than 200 ms of the 10 s budget in this regime. **The dominant cost in the edit-to-running loop is service stop/start + health check, not bsdiff or upload.** Phase 0 Week 1 eval plan should time those steps explicitly on DISCO-2.

## Caveats

- **Sample is small (3 pairs).** Commits were adjacent on the agent branch; the 5th candidate (`e9239c3`) had a genuinely broken build (incomplete-typedef regression fixed in the next commit) and was excluded.
- **Churn is small-scale** (debug logging, one-line typedef fixes). Realistic feature-commit churn (new DTP params, git-provenance tracking) would likely push p99 higher — 5-10% patch ratio would not be surprising. The thesis evaluation should re-run pair-batch against a 10-20-commit window spanning real features, not just debug churn.
- **No DISCO-2 hardware loop yet.** Compute-ms was measured on the host, not target. Target-side bspatch apply time is unknown until Week 2 wires bspatch into the agent and a local CSP loop can measure it end-to-end.

## Landmine #4 (bsdiff version skew)

Status: **pin enforced, format verified, target-side verification deferred to Week 2.**

- `bsdiff4==1.2.3` pinned in `pyproject.toml`. **Live venv was found drifted to 1.2.6 during this session** (the pyproject pin had never been reinstalled after an earlier dependency update); re-pinned to 1.2.3.
- Two sentinel tests added in `tests/test_bsdiff_util.py`:
  - `test_bsdiff4_pinned_to_v1_spec_version` — asserts `bsdiff4.__version__ == "1.2.3"`; catches future venv drift.
  - `test_compute_patch_emits_legacy_bsdiff40_format` — asserts every emitted patch starts with `BSDIFF40` magic; catches format drift if the pin is ever relaxed.
- **Full byte-equal verification through the agent's C bspatch is blocked until Week 2:** `grep -r 'bsdiff\|bspatch' satdeploy-agent/` returns nothing. The agent has no patch-apply path yet. When Week 2 wires bspatch into `satdeploy-agent` + the Week 2 `iterate` command, add a local CSP loop test that: (1) computes a patch in Python, (2) sends it via DTP to a locally-running agent, (3) reads the target-side SHA256, (4) asserts byte-equal with the expected new binary. If that test fails, landmine #4 has fired for real and the fix is to ship a matched-source bspatch with the agent build rather than relying on the Python pin alone.

## Debug symbol pipeline — end-to-end verified

Week 1 deliverable: script at [`scripts/debuginfod_demo.sh`](../../scripts/debuginfod_demo.sh). Exit 0 proves every link in the pipeline:

1. ARM cross-compile (`/opt/poky` Yocto SDK, `aarch64-poky-linux-gcc` via `$CC`).
2. Split debug info (`aarch64-poky-linux-objcopy --only-keep-debug` + `--strip-debug` + `--add-gnu-debuglink`).
3. Local debuginfod serving by build-id (wrapper at `satdeploy debuginfod serve`, binary from Yocto SDK's `/opt/poky/sysroots/x86_64-pokysdk-linux/usr/bin/debuginfod`).
4. `aarch64-poky-linux-gdb` with `DEBUGINFOD_URLS=http://localhost:8002` resolves source lines on the stripped binary.

Evidence from last run (build-id varies by invocation):

```
Line 7 of "/tmp/.../hello.c" starts at address 0x844 <main> and ends at 0x858 <main+20>.
/tmp/.../hello.c:
10      int answer = compute_answer(7);
   0x000000000000085c <+24>:  bl      0x818 <compute_answer>
```

Source-line + assembly interleaving on a stripped ARM ELF, fetched over HTTP from the local debuginfod. Thesis-citable.

Re-run with: `bash scripts/debuginfod_demo.sh`.

## How to reproduce

```bash
# From repo root, with /opt/poky Yocto SDK available:
git worktree add --detach /tmp/sat-pairs HEAD
cd /tmp/sat-pairs && git submodule update --init --recursive \
    satdeploy-agent/lib/csp satdeploy-agent/lib/dtp satdeploy-agent/lib/param
source /opt/poky/environment-setup-armv8a-poky-linux

mkdir -p /tmp/agent-binaries
for idx_commit in 01:268b5e4 03:a761361 04:b342369 05:4999cbe; do
    idx="${idx_commit%:*}"; sha="${idx_commit#*:}"
    git checkout "$sha"
    git submodule update --recursive \
        satdeploy-agent/lib/csp satdeploy-agent/lib/dtp satdeploy-agent/lib/param
    (cd satdeploy-agent && \
        meson setup build-arm --cross-file yocto_cross.ini --reconfigure && \
        ninja -C build-arm)
    cp /tmp/sat-pairs/satdeploy-agent/build-arm/satdeploy-agent \
       "/tmp/agent-binaries/${idx}-${sha}.bin"
done

cd <repo-root>
.venv/bin/python scripts/feasibility_test.py --pair-dir /tmp/agent-binaries --json
```

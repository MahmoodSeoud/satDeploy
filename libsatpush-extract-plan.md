# libsatpush extract — continuation plan

Handoff state for picking up the libsatpush extract on a different machine.
Generated 2026-05-22 after Step 4b of /plan-eng-review.

## Where we are

The library `libsatpush/` exists as a sibling directory inside the satdeploy
repo with:

```
libsatpush/
├── LICENSE                      MIT, matches repo root
├── meson.build                  static_library + pkg-config install rules
├── include/
│   └── satpush/satpush.h        public API (serve / pull naming)
└── src/
    ├── satpush_version.c        satpush_version_string()
    ├── satpush_ctx.c            create/destroy/strerror/sha256_file/resume_unlink
    ├── satpush_pull.c           pull-side implementation (adapted from agent)
    ├── satpush_serve.c          serve-side registration wrappers
    ├── satpush_internal.h       private ctx struct
    ├── session_state.c          sidecar persistence (adapted)
    ├── session_state_internal.h internal header for sidecar
    ├── sha256.c                 bundled SHA256 (Brad Conte, public domain)
    └── sha256.h                 internal header
```

Both consumers are wired up:

- **satdeploy-agent** — full integration. Compiles all six libsatpush
  sources inline (relative path), uses `satpush_pull_file` in
  `deploy_handler.c`, creates a global `g_satpush_ctx` in
  `deploy_handler_init`.
- **satdeploy-apm** — minimal integration. Compiles only
  `satpush_serve.c` (the rest would clash with the APM's own SHA256
  implementation). Uses `satpush_serve_register` / `satpush_serve_unregister`
  with `ctx == NULL`.

The pre-extract source files are still on disk for revert safety but
have been removed from the meson build:

- `satdeploy-agent/src/dtp_client.c` — inert, replaced by libsatpush
- `satdeploy-agent/src/session_state.c` — inert
- `satdeploy-agent/src/sha256.c` — inert
- `satdeploy-agent/include/session_state.h` — header still on disk
- `satdeploy-agent/include/sha256.h` — header still on disk; still
  referenced by `backup_manager.c`. ABI-compatible with libsatpush's
  internal sha256.h (identical struct layout, different guard).

Three TODOs were appended to `TODOS.md`:

1. Replace OpenSSL SHA256 with bundled implementation (P3) —
   actually moot for the agent since it never used OpenSSL; still
   relevant for the APM as a cleanup item.
2. `satpush_init_csp_zmq()` convenience helper (P3).
3. Decompose `satpush_pull_file_impl` after the extract lands (P2).

## What's left (Steps 5-9)

### Step 5 — README + example CLI

Goal: ship `libsatpush/` as something a stranger can clone, read, and try.

- Write `libsatpush/README.md` based on the draft from the /plan-eng-review
  conversation. Pitch the primitive, document the API in 50 lines, show
  the empirical F3.b numbers, link the LICENSE.
- Add a tiny `libsatpush/examples/push.c` + `libsatpush/examples/recv.c`
  demonstrating serve + pull over ZMQ loopback. Keep them under 80 LOC.
- Uncomment the `subdir('examples')` line in `libsatpush/meson.build`.

### Step 6 — Unit tests (14)

Goal: every public API path has a test asserting expected behavior.

Cover, at minimum:
- `satpush_create` with valid params, NULL state_dir, missing state_dir, zero throughput
- `satpush_pull_file` with NULL opts, file_size mismatch, hash mismatch
  (returns `SATPUSH_HASH_MISMATCH`), resume sidecar exists + hash matches
  (resumes), resume sidecar + hash differs (wipes), cancellation via
  progress callback
- `satpush_serve_register` / `satpush_serve_unregister` register-then-unregister
  round trip
- `satpush_sha256_file` with valid file, missing file, permission denied
- `satpush_resume_unlink`
- `satpush_strerror` for every result enum

Framework: cmocka or Unity. Both have meson wrap files. Pattern that fits
the agent's existing `experiments/` harness:

```
libsatpush/tests/
├── meson.build
├── test_ctx.c
├── test_pull_validation.c
├── test_serve.c
└── test_sha256.c
```

Uncomment the `subdir('tests')` line in `libsatpush/meson.build`.

### Step 7 — F3.b regression sweep (CRITICAL)

This is the IRON RULE check from the /plan-eng-review test plan. The
internal `satpush_pull_file_impl` is intentionally bit-identical to the
pre-extract `dtp_download_file`. Running the existing sweep must produce
the same numbers.

```bash
# 1. Build the agent against libsatpush
cd satdeploy-agent
meson setup build --wipe
ninja -C build

# 2. Run the sweeps
cd ..
bash experiments/sweep_tail_race.sh    # n=30 at 0% loss, 3 sizes
bash experiments/sweep_loss_rates.sh   # n=5 per loss rate (1%, 5%, 10%)

# 3. Diff the CSVs against the pre-extract baseline
diff experiments/results/tail_race.csv ${BASELINE}
diff experiments/results/loss_rates.csv ${BASELINE}
```

Expected (from the pre-extract `experiments/results/`):

| Loss rate | Mean retry rounds | Single-pass success |
|-----------|-------------------|---------------------|
| 0% (naive build) | n/a (DTP_MAX_RETRY_ROUNDS=0) | 1/30 |
| 0% (smart build) | ~1.0 | 30/30 |
| 1% | 1.8 | 5/5 |
| 5% | 6.8 | 5/5 |
| 10% | hits 8-cap | 0/5 |

If numbers shift, something semantic changed in the move. Likely
suspects:
- `compute_missing_intervals` walker semantics
- `session_state_path` resolution under the new `state_dir` parameter
- `expected_eot_ts_ms` math when `throughput_bps` flows through opts

### Step 8 — Yocto recipe (meta-satdeploy)

The Yocto recipe at `meta-satdeploy/recipes-connectivity/satdeploy-agent/`
builds the agent. After the extract, it needs to also install libsatpush
headers and the `.pc` file so other recipes can `pkg-config --libs
libsatpush`.

Concretely: the recipe runs `meson install`, which (post-extract) installs:
- `${includedir}/satpush/satpush.h`
- `${libdir}/pkgconfig/libsatpush.pc`
- `${libdir}/libsatpush.a` (static library)

Verify with `bitbake -e satdeploy-agent | grep satpush` after a rebuild.

### Step 9 — Commit, push, PR

Squash the extract into clean commits if the work-in-progress branch has
WIP commits. Recommended commit boundary:

1. `feat(libsatpush): introduce cross-pass resumable file delivery library`
   (Steps 1-2: skeleton + moved sources)
2. `refactor(agent): consume libsatpush for pull operations`
   (Step 4a)
3. `refactor(apm): consume libsatpush for serve operations`
   (Step 4b)
4. `docs(libsatpush): add README and example CLI`
   (Step 5)
5. `test(libsatpush): unit tests for public API`
   (Step 6)

Open PR against main with the empirical F3.b numbers in the description.

## How to continue on the other machine

```bash
# 1. Clone or pull
git fetch origin
git checkout extract-libsatpush

# 2. Read the design state
cat libsatpush-extract-plan.md   # this file
cat libsatpush/include/satpush/satpush.h
cat TODOS.md                      # for the three deferred TODOs

# 3. Build to verify the wiring lands cleanly on the new machine
cd satdeploy-agent
meson setup build --wipe
ninja -C build

cd ../satdeploy-apm
meson setup build --wipe
ninja -C build

# 4. Pick a next step (5, 6, 7, 8, or 9) and continue.
#    Step 7 (F3.b regression) is the highest-priority gate before
#    anything ships; everything else is polish on top.
```

## Risk register

Things that might bite on the next machine but are not yet verified:

1. **Header collision between agent's `sha256.h` and libsatpush's
   internal one.** Identical struct layouts, different header guards.
   ABI-compatible. Each translation unit picks one based on include path
   order. Has not been observed to break a build but flagged as a watch
   item.
2. **APM's unresolved symbols.** `satpush_serve.c` calls into libdtp
   payload APIs; the APM has `b_lundef=false` so unresolved symbols are
   allowed at link time and resolved at dlopen. If a hardened linker
   refuses, the fix is `-Wl,--unresolved-symbols=ignore-in-shared-libs`
   on the APM's link_args.
3. **libsatpush standalone build is not yet supported.** `cd libsatpush
   && meson setup build` will fail at dependency resolution (no csp /
   dtp_client subprojects under `libsatpush/lib/`). That's expected for
   v0.1; adopters consume through satdeploy-agent's build until a
   wrap-file path is added.
4. **Yocto recipe is not yet updated.** `meta-satdeploy` needs Step 8.
   On-device deploys still work because the agent statically links
   everything; only the install rules need updating.

## Files touched in this extract

```
A  libsatpush/LICENSE
A  libsatpush/meson.build
A  libsatpush/include/satpush/satpush.h
A  libsatpush/src/satpush_ctx.c
A  libsatpush/src/satpush_pull.c
A  libsatpush/src/satpush_serve.c
A  libsatpush/src/satpush_internal.h
A  libsatpush/src/satpush_version.c
A  libsatpush/src/session_state.c
A  libsatpush/src/session_state_internal.h
A  libsatpush/src/sha256.c
A  libsatpush/src/sha256.h
M  satdeploy-agent/meson.build
M  satdeploy-agent/include/satdeploy_agent.h
M  satdeploy-agent/src/deploy_handler.c
M  satdeploy-apm/meson.build
M  satdeploy-apm/src/satdeploy_apm.c
M  TODOS.md
A  libsatpush-extract-plan.md      (this file)
```

Pre-extract files retained on disk but not in the build (delete after
Step 7 verifies the F3.b regression sweep matches baseline):

```
satdeploy-agent/src/dtp_client.c
satdeploy-agent/src/session_state.c
satdeploy-agent/src/sha256.c
satdeploy-agent/include/session_state.h
```

`satdeploy-agent/include/sha256.h` stays — backup_manager.c still
includes it. Eventual cleanup: have backup_manager.c call
`satpush_sha256_file` via the public API and delete the agent's
sha256.h entirely.

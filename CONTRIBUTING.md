# Contributing to satdeploy

Thanks for looking. satdeploy is a young project that ships software to
real spacecraft, so the bar for changes that touch the wire format or the
cross-pass resume sidecar is unusually high. The bar for everything else is
"please open a PR, the worst that happens is we talk about it."

## What lives where

```
satdeploy-agent/    C, runs on the satellite, listens on CSP port 20.
satdeploy-apm/      C, dlopen'd into csh on the ground station.
docs/               Markdown reference docs.
meta-satdeploy/     Yocto layer for distributing the agent in flight images.
scripts/            Dev container, test apps, demos.
init/               Templates copied into the dev container's csh session.
```

The two C components share a protobuf schema (`satdeploy-apm/src/deploy.proto`)
and read/write the same SQLite database (`~/.satdeploy/history.db`). They do
**not** share other code.

## Before you start

1. **Clone with submodules.** `--recurse-submodules` is not optional — both
   components vendor `libcsp`, `libdtp`, `libparam`, and the APM also vendors
   `slash` and `apm_csh`. Forgetting this is the most common source of
   broken first builds.

   ```bash
   git clone --recurse-submodules https://github.com/MahmoodSeoud/satDeploy.git
   ```

   If you already cloned: `git submodule update --init --recursive`.

2. **Pick an environment.** macOS host? Use the dev container — `libcsp` and
   `libdtp` don't build on darwin. Linux host? Native build is fine.

   ```bash
   ./scripts/docker-dev.sh    # macOS or Linux, drops into a 2-pane tmux
   ```

   The container live-mounts the repo, so edits on the host show up
   instantly inside.

## Build, run, iterate

Inside the dev container (or on a Linux host with build deps installed —
see [docs/building.md](docs/building.md)):

```bash
build-all                             # both components, native build
./satdeploy-agent/build-native/satdeploy-agent --version
csh-zmq                               # csh with the test config wired up
csh> apm load
csh> satdeploy push hello
csh> satdeploy status
csh> satdeploy rollback hello
```

For the cross-pass resume code path, push the 50 MB `payload` app and Ctrl-C
the agent mid-transfer; the next push must read the sidecar at
`/var/lib/satdeploy/state/payload.dtpstate` and request only the missing
seqs.

For ARM cross-compile (the only flight-correct build):

```bash
source /opt/poky/environment-setup-armv8a-poky-linux
cd satdeploy-agent
meson setup build-arm --cross-file yocto_cross.ini --wipe
ninja -C build-arm
```

## The CSP version pinning gotcha (read this)

The APM is dlopen'd into csh. They share `csp_packet_t` structs — same
process address space — so if `satdeploy-apm/lib/csp` and csh's `lib/csp`
disagree about field offsets, packets get silently corrupted on the way
between them. We've shipped that bug before. You don't want to.

When you bump `satdeploy-apm/lib/csp`, sync against csh's pin:

```bash
cd satdeploy-apm/lib/csp
git checkout $(cd /path/to/csh/lib/csp && git rev-parse HEAD)
```

The dev container handles this for you (`Dockerfile.dev` builds csh from
HEAD by default; override with `--build-arg CSH_REF=<commit>` if you need
to test against a specific csh release).

## Tests

We don't have unit tests for the C code yet — `libcsp`, `libdtp`, and
`libparam` would need mock implementations and that work hasn't shipped.
What we do have:

- The dev container's end-to-end loopback (push → status → rollback)
- Manual cross-pass resume verification with the 50 MB `payload` app
- The agent's protobuf round-trip implicitly exercised on every push

If your change is non-trivial, run the full loopback before opening the PR:

```bash
./scripts/docker-dev.sh tmux
# left pane (csh):
csh> apm load
csh> satdeploy push hello
csh> satdeploy push -a
csh> satdeploy status
csh> satdeploy rollback controller
csh> satdeploy logs hello
```

If your change touches `dtp_client.c`, `session_state.c`, or
`deploy_handler.c`, also exercise the cross-pass path:

```bash
csh> satdeploy push payload     # ~50 MB
# right pane: Ctrl-C the agent mid-transfer
# right pane: agent -i ZMQ -p localhost -a 5425   # restart it
csh> satdeploy push payload     # must resume, not restart
```

## Wire-format changes

If your change modifies any of:

- `satdeploy-apm/src/deploy.proto` (the request/response schema)
- `satdeploy-agent/include/session_state.h` (the sidecar layout)
- The handshake order in `deploy_handler.c`
- DTP transport defaults in either component

…then it is a **wire-format change**. Coordinate the bump:

1. Add a `### Wire compatibility` block to the next CHANGELOG entry naming
   exactly what changed.
2. Bump the minor version in both `satdeploy-agent/meson.build` and
   `satdeploy-apm/meson.build`.
3. Land the agent change first if backward-compat reads are possible;
   otherwise, document that APM and agent must be upgraded together.

The cross-pass resume sidecar is content-addressed by the full SHA256, so
a re-staged binary always invalidates it — but a sidecar layout change
doesn't carry that protection. Don't rename fields without bumping the
sidecar version word.

## Commits

We use [conventional commits](https://www.conventionalcommits.org/) with
emoji prefixes:

```
✨ feat(scope): summary
🐛 fix(scope): summary
📝 docs(scope): summary
♻️ refactor(scope): summary
🔥 chore(scope): remove dead code
🧪 test(scope): add coverage
```

One logical change per commit.

## Pull requests

- Reference any issue your PR fixes.
- For wire-format changes, include the CHANGELOG entry in the same PR.
- For new operator-facing commands, update `docs/commands.md` in the same PR.

That's it. Open the PR, we'll talk.

## Code of conduct

Be kind. The satellite community is small.

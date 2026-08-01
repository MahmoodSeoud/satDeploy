#!/usr/bin/env bash
# Deterministic test-binary generation. Unlike scripts/make-test-apps.sh
# (which uses /dev/urandom and changes between runs), the experiment harness
# needs reproducibility — same seed must produce the same bytes, so a
# successful trial can be re-run for forensics.
#
# We use /dev/urandom seeded via openssl's rand command in PRG mode... no,
# simpler: we use `dd` from /dev/zero piped through openssl enc with a
# password derived from the seed. AES-CTR with a known key is a reproducible
# pseudo-random stream and is fast enough for 100 MB files.

set -euo pipefail

FIXTURE_DIR="${FIXTURE_DIR:-/tmp/satdeploy-fixtures}"
mkdir -p "$FIXTURE_DIR"

# Where staged fixtures land, i.e. the -f path each push names. The harness
# reads this to build the push command line.
FIXTURE_STAGE_DIR="${FIXTURE_STAGE_DIR:-/tmp/satdeploy-test-apps}"

# fixture_make <size_bytes> <seed_int> [out_path]
#
# Writes a deterministic-by-seed binary of the requested size. Echoes the
# absolute path to stdout. Idempotent — if the file already exists with
# the right size, it's reused (which lets repeated trials skip regen).
fixture_make() {
    local size_bytes="$1"
    local seed="$2"
    local out_path="${3:-${FIXTURE_DIR}/blob_${size_bytes}_${seed}.bin}"

    if [ -f "$out_path" ] && [ "$(stat -c%s "$out_path")" = "$size_bytes" ]; then
        echo "$out_path"
        return 0
    fi

    # AES-CTR with a key derived from the seed. -nosalt makes it a pure
    # function of the password (no random IV), so seed → bytes is fixed.
    # We feed /dev/zero in so the output is just the AES-CTR keystream.
    local password
    password="seed-${seed}-satdeploy-experiments"
    head -c "$size_bytes" /dev/zero \
        | openssl enc -aes-256-ctr -nosalt -pass "pass:${password}" \
        > "$out_path" 2>/dev/null

    chmod +x "$out_path"
    echo "$out_path"
}

# fixture_sha256 <path>
fixture_sha256() {
    sha256sum "$1" | awk '{print $1}'
}

# fixture_stage <fixture_path>
#
# Stage the fixture under $FIXTURE_STAGE_DIR with a size-derived app name
# (hello, controller, telemetry, payload). satdeploy v2 has no config file:
# the push names both paths with -f/-r, so the app name is only a label —
# but keeping the size->name mapping keeps CSV rows comparable with the v1
# cells.
#
# Emits the app name on stdout so the caller can pass it to csh_push.
fixture_stage() {
    local fixture_path="$1"
    local size_bytes
    size_bytes="$(stat -c%s "$fixture_path")"

    local app
    if [ "$size_bytes" -le 1024 ]; then
        app="hello"
    elif [ "$size_bytes" -le 200000 ]; then
        app="controller"
    elif [ "$size_bytes" -le 10000000 ]; then
        app="telemetry"
    else
        app="payload"
    fi

    local target_local="${FIXTURE_STAGE_DIR}/${app}"
    mkdir -p "$(dirname "$target_local")"
    # Use cp -f, not symlink — the APM may stat the file and chmod, and
    # symlink semantics around DTP registration are untested. cp is safe.
    cp -f "$fixture_path" "$target_local"
    chmod +x "$target_local"

    echo "$app"
}

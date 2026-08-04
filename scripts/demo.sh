#!/usr/bin/env bash
# demo.sh — bring up the satdeploy lab, ready to use.
#
#   scripts/demo.sh        the lab: tmux with a ground station (csh) on the left
#                          and the satellite (agent) on the right. Type commands.
#   scripts/demo.sh auto   unattended self-check, for CI or a cold repo.
#
# The ground station comes up with three command sets loaded:
#   satdeploy ...     push / status / list / rollback / logs
#   csp_link ...      measure how good the link is (loss, RTT, jitter)
#   csp_loss ...      turn packet loss on and off mid-session
#   csp_monitor ...   record every frame that crosses the link
# The last two come from the csp-intercept repo, auto-mounted when it sits next
# to this one (override with CSP_INTERCEPT=/path). Without it the lab still runs;
# only the instrument commands are missing.
#
# First time: docs/QUICKSTART.md   Full reference: docs/MANUAL.md
#
# Everything runs inside the satdeploy-dev Docker image; the host needs only
# Docker. Builds land in container-local dirs (see scripts/dev-shell.sh for
# why: macOS virtiofs mtimes trip meson's clock-skew abort on the mount).
set -euo pipefail

MODE="${1:-interactive}"
LOSS_PCT="${2:-10}"
IMAGE="${IMAGE:-satdeploy-dev}"
CTR="${CTR:-satdeploy-demo}"
DEMO_PACE_US="${DEMO_PACE_US:-2000}"   # slows the blast so a live Ctrl-C is catchable
# Seed 18: glibc rand()'s first dozen draws all clear 12%, so the demo's FIRST
# push command (a single unprotected CSP transaction) survives the lossy broker
# instead of deterministically eating the very first drop.
DEMO_SEED="${DEMO_SEED:-18}"

# ---------------------------------------------------------------------------
# Inner entrypoints (run INSIDE the container via docker exec). Dispatch first
# so the host-side section below stays readable.
# ---------------------------------------------------------------------------
if [ "$MODE" = "--inner-setup" ]; then
    # $2 = broker kind (plain|lossy), $3 = loss pct, $4 = seed
    BROKER="$2"; PCT="$3"; SEED="$4"
    cd /satdeploy
    . scripts/dev-shell.sh
    # Build output goes to a log: on a first run it is thousands of meson and
    # ninja lines, which buries the one thing the operator needs to know (did
    # it work). The log path is printed on failure.
    echo ">>> Building the satellite agent and the ground station..."
    if ! build-all >/tmp/build.log 2>&1; then
        echo "BUILD FAILED. Last 20 lines of /tmp/build.log:" >&2
        tail -20 /tmp/build.log >&2
        exit 1
    fi
    scripts/make-test-apps.sh >/dev/null
    mkdir -p /tmp/satdeploy-target /tmp/satdeploy-backups
    # Short, memorable paths for the lab: /apps is what you have on the ground,
    # /target is the satellite's filesystem. The long /tmp/satdeploy-* paths the
    # experiment harness uses still exist; these are the same files, named so a
    # push command fits on one line and reads at a glance.
    ln -sfn /tmp/satdeploy-test-apps /apps
    ln -sfn /tmp/satdeploy-target /target
    pkill -x zmqproxy 2>/dev/null || true
    pkill -x zmqproxy-lossy 2>/dev/null || true
    echo ">>> Starting zmqproxy"
    nohup zmqproxy >/tmp/zmqproxy.log 2>&1 &
    sleep 0.5

    # The csp-intercept tooling, if the repo is mounted: the loss injector and
    # the wire monitor, both as csh APMs so they are commands at the prompt
    # rather than processes to restart.
    if [ -d /csp-intercept ]; then
        echo ">>> Building the test instruments (csp_link + csp_loss + csp_monitor)..."
        {
            if [ -f /root/builds/csp-intercept/build.ninja ]; then
                meson setup /root/builds/csp-intercept /csp-intercept -Dfrontends=true --reconfigure
            else
                meson setup /root/builds/csp-intercept /csp-intercept -Dfrontends=true
            fi
            ninja -C /root/builds/csp-intercept \
                apm/libcsh_csp_link.so apm/libcsh_csp_loss.so apm/libcsh_csp_monitor.so
        } >>/tmp/build.log 2>&1 || {
            echo "INSTRUMENT BUILD FAILED. Last 20 lines of /tmp/build.log:" >&2
            tail -20 /tmp/build.log >&2
            exit 1
        }
        cp /root/builds/csp-intercept/apm/libcsh_csp_link.so \
           /root/builds/csp-intercept/apm/libcsh_csp_loss.so \
           /root/builds/csp-intercept/apm/libcsh_csp_monitor.so \
           "$SATDEPLOY_APM_DIR/"
    else
        echo ">>> (csp-intercept not found -- csp_link / csp_loss / csp_monitor unavailable)"
    fi
    echo ">>> Ready. Opening the lab..."
    exit 0
fi

if [ "$MODE" = "--inner-tmux" ]; then
    # $2 = pace us
    PACE="$2"
    tmux kill-session -t demo 2>/dev/null || true
    tmux new-session -d -s demo -c /satdeploy
    tmux split-window -h -t demo:0 -c /satdeploy
    tmux send-keys -t demo:0.0 "cat /satdeploy/scripts/demo-cheatsheet.txt; export SVU_BLAST_PACE_US=$PACE; csh -i /satdeploy/init/lab.csh" Enter
    tmux send-keys -t demo:0.1 "/root/builds/agent/satdeploy-agent -i ZMQ -p localhost -a 5425" Enter
    tmux select-pane -t demo:0.0
    exec tmux attach -t demo
fi

if [ "$MODE" = "--inner-auto" ]; then
    cd /satdeploy
    . scripts/dev-shell.sh
    build-agent-naive >/dev/null
    export SVU_BLAST_PACE_US=200
    CSV=/tmp/demo-acts.csv
    rm -f "$CSV"
    declare -a NAMES EXPECT GOT
    run_act() { # name, expected(success|not-success), harness args...
        local name="$1" expect="$2"; shift 2
        echo ">>> $name"
        set +e
        timeout 400 experiments/harness.sh --csv "$CSV" "$@" >/dev/null 2>&1
        set -e
        local outcome
        outcome=$(tail -1 "$CSV" | awk -F, '{print $(NF-2)}')
        NAMES+=("$name"); EXPECT+=("$expect"); GOT+=("$outcome")
    }
    run_act "Act 1: clean deploy (100KB, zmq)" success \
        --experiment e1 --size 102400 --seed 11 --link zmq
    run_act "Act 2: kill mid-transfer + resume (5MB, kill@2.5MB)" success \
        --experiment e4 --size 5242880 --seed 12 --link zmq \
        --kill-at-byte 2621440 --max-passes 2 --timeout-s 150
    RESUME_LOG=$(grep -l "restored" /tmp/satdeploy-experiments/*/pass-2.agent.log 2>/dev/null | tail -1)
    run_act "Act 3: 10% frame loss, flight build (kiss)" success \
        --experiment e2 --size 102400 --seed 13 --link kiss --loss-pct 10
    AGENT_BIN="$AGENT_NAIVE_BIN" run_act \
        "Act 4: 10% frame loss, naive control (ablation)" not-success \
        --experiment e2 --size 102400 --seed 13 --link kiss --loss-pct 10

    echo
    echo "==================== DEMO RESULT ===================="
    FAIL=0
    for i in "${!NAMES[@]}"; do
        ok=PASS
        if [ "${EXPECT[$i]}" = "success" ]; then
            [ "${GOT[$i]}" = "success" ] || ok=FAIL
        else
            # The ablation must fail to DELIVER but must never install garbage.
            { [ "${GOT[$i]}" != "success" ] && [ "${GOT[$i]}" != "sha_mismatch" ]; } || ok=FAIL
        fi
        [ "$ok" = "FAIL" ] && FAIL=1
        printf "  %-52s %-14s [%s]\n" "${NAMES[$i]}" "${GOT[$i]}" "$ok"
    done
    if [ -n "$RESUME_LOG" ]; then
        echo "-----------------------------------------------------"
        echo "  Act 2 resume evidence ($RESUME_LOG):"
        grep -E "svu: (restored|round|VERIFIED)" "$RESUME_LOG" | sed 's/^/    /'
    fi
    echo "====================================================="
    [ "$FAIL" = 0 ] && echo "ALL ACTS PASSED" || echo "SOME ACTS FAILED"
    exit "$FAIL"
fi

# ---------------------------------------------------------------------------
# Host side
# ---------------------------------------------------------------------------
cd "$(dirname "$0")/.."

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo ">>> Building $IMAGE image (first run is slow)"
    docker build -f Dockerfile.dev -t "$IMAGE" .
fi

# Mount csp-intercept when it is there: it supplies the loss injector and the
# wire monitor as csh APMs. Absent, everything else still works.
MOUNTS=(-v "$(pwd):/satdeploy")
CI="${CSP_INTERCEPT:-$(cd .. && pwd)/csp-intercept}"
HAVE_CI=0
if [ -d "$CI/apm" ]; then
    MOUNTS+=(-v "$CI:/csp-intercept")
    HAVE_CI=1
fi

# Recreate the container if it is missing, or if lossy mode needs the
# csp-intercept mount and the running one was started without it.
NEED_START=0
if ! docker ps --format '{{.Names}}' | grep -qx "$CTR"; then
    NEED_START=1
elif [ "$HAVE_CI" = 1 ] && ! docker exec "$CTR" test -d /csp-intercept 2>/dev/null; then
    echo ">>> Restarting $CTR with the csp-intercept mount"
    NEED_START=1
fi
if [ "$NEED_START" = 1 ]; then
    docker rm -f "$CTR" >/dev/null 2>&1 || true
    echo ">>> Starting container $CTR"
    docker run -d --name "$CTR" --init --cap-add=NET_ADMIN "${MOUNTS[@]}" "$IMAGE" sleep infinity >/dev/null
fi

docker exec "$CTR" /satdeploy/scripts/demo.sh --inner-setup plain "$LOSS_PCT" "$DEMO_SEED"

case "$MODE" in
    auto)
        docker exec "$CTR" /satdeploy/scripts/demo.sh --inner-auto
        ;;
    interactive)
        exec docker exec -it "$CTR" /satdeploy/scripts/demo.sh --inner-tmux "$DEMO_PACE_US"
        ;;
    *)
        echo "Usage: scripts/demo.sh [auto]   (no argument = the interactive lab)" >&2
        exit 1
        ;;
esac

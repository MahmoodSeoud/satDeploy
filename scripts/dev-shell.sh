# Functions and aliases for the satdeploy dev container.
#
# This file is volume-mounted at /satdeploy/scripts/dev-shell.sh inside the
# container, so editing it on the host updates the helpers on the next shell
# start — no docker rebuild needed.
#
# Sourced from /etc/profile.d/satdeploy.sh (login shells) and
# /etc/bash.bashrc (interactive shells).

# Don't load twice
[ "${_SATDEPLOY_SHELL_LOADED:-0}" = 1 ] && return
_SATDEPLOY_SHELL_LOADED=1

export PS1='\[\e[1;32m\]satdeploy-dev\[\e[0m\]:\[\e[1;34m\]\w\[\e[0m\]$ '
export EDITOR=vim
alias ll='ls -lah --color=auto'
alias g='git'

# Path where csh dlopens APM .so files from. CSH searches:
#   $HOME/.local/lib/csh
#   /opt/csh/builddir
#   /usr/lib/csh
# Use the first one — it doesn't require root and is per-user.
SATDEPLOY_APM_DIR="${SATDEPLOY_APM_DIR:-/root/.local/lib/csh}"

# Build dirs live on the CONTAINER filesystem, not the repo bind mount.
# On macOS Docker the mount is virtiofs, whose mtimes can sit a few ms
# ahead of the container clock; meson stats its own freshly written
# coredata.dat, sees a timestamp "in the future", and aborts with
# "Clock skew detected". /root is native overlayfs, so timestamps are
# coherent there. The repo mount stays the SOURCE of truth; only build
# artifacts move.
SATDEPLOY_BUILD_ROOT="${SATDEPLOY_BUILD_ROOT:-/root/builds}"
export AGENT_BIN="${AGENT_BIN:-$SATDEPLOY_BUILD_ROOT/agent/satdeploy-agent}"
export AGENT_NAIVE_BIN="${AGENT_NAIVE_BIN:-$SATDEPLOY_BUILD_ROOT/agent-naive/satdeploy-agent}"

agent() { "$SATDEPLOY_BUILD_ROOT/agent/satdeploy-agent" "$@"; }

# meson setup wants --reconfigure on an existing build dir and refuses it
# on a fresh one; branch so both paths work.
_meson-setup() {
    local dir="$1"; shift
    if [ -f "$dir/build.ninja" ]; then
        meson setup "$dir" "$@" --reconfigure
    else
        meson setup "$dir" "$@"
    fi
}

build-agent() {
    _meson-setup "$SATDEPLOY_BUILD_ROOT/agent" satdeploy-agent \
        && ninja -C "$SATDEPLOY_BUILD_ROOT/agent"
}

# The thesis control build: same source, recovery and resume compiled out.
build-agent-naive() {
    _meson-setup "$SATDEPLOY_BUILD_ROOT/agent-naive" satdeploy-agent -Dnaive_baseline=true \
        && ninja -C "$SATDEPLOY_BUILD_ROOT/agent-naive"
}

# Builds the APM and installs the .so to where csh expects it. Auto-install
# is the default because forgetting to copy and then hitting "No APMs found
# in ..." is the most common dev-flow papercut.
build-apm() {
    _meson-setup "$SATDEPLOY_BUILD_ROOT/apm" satdeploy-apm \
        && ninja -C "$SATDEPLOY_BUILD_ROOT/apm" \
        && mkdir -p "$SATDEPLOY_APM_DIR" \
        && cp "$SATDEPLOY_BUILD_ROOT/apm/libcsh_satdeploy_apm.so" "$SATDEPLOY_APM_DIR/" \
        && echo "installed: $SATDEPLOY_APM_DIR/libcsh_satdeploy_apm.so"
}

build-all() { build-agent && build-apm; }

# Backward-compat: install-apm is now identical to build-apm.
install-apm() { build-apm; }

# `csh` is intentionally NOT overridden — it stays the plain spaceinventor
# binary so the operator can launch with any init file:
#   csh                          # bare shell, no init
#   csh -i init/zmq.csh          # ZMQ ground transport
#   csh -i init/can.csh          # (future) CAN transport
# The auto-launched pane on container entry uses the ZMQ init script.

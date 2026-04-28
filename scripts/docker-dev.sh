#!/usr/bin/env bash
# Build and run the satdeploy dev container interactively.
#
# Mounts the repo at /satdeploy so edits on the host flow through. Builds
# inside the container land in satdeploy-agent/build-native/ and
# satdeploy-apm/build/ on the host — that's why those paths are gitignored.
#
# Usage:
#   ./scripts/docker-dev.sh                        # default: bash shell
#   ./scripts/docker-dev.sh tmux                   # start tmux directly
#   ./scripts/docker-dev.sh ./scripts/foo.sh       # run a one-off command
#
# Once running, attach a second pane from another terminal with:
#   docker exec -it satdeploy-dev bash
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE_TAG="${IMAGE_TAG:-satdeploy-dev}"
CONTAINER_NAME="${CONTAINER_NAME:-satdeploy-dev}"

echo ">>> Building $IMAGE_TAG (first run is slow; reuses layer cache after)"
docker build -f Dockerfile.dev -t "$IMAGE_TAG" .

# Clean up any existing container with the same name. --rm on the run below
# means it'll auto-clean on exit, but a previous crash could leave a stale
# container — remove defensively.
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

echo ">>> Starting container $CONTAINER_NAME (repo mounted at /satdeploy)"
echo ">>> Use 'docker exec -it $CONTAINER_NAME bash' from another terminal for a 2nd pane"
docker run --rm -it \
    --name "$CONTAINER_NAME" \
    -v "$(pwd):/satdeploy" \
    "$IMAGE_TAG" "$@"

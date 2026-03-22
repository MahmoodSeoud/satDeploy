#!/bin/bash
set -e

# Build CSH
echo "=== Building CSH ==="
cd /csh
if [ ! -d builddir ]; then
    ./configure
else
    echo "builddir exists, skipping configure"
fi
ninja -C builddir
meson install -C builddir --skip-subprojects

# Build satdeploy-apm
echo "=== Building satdeploy-apm ==="
cd /satbuild/satdeploy-apm
if [ ! -d build ]; then
    meson setup build
fi
ninja -C build
cp build/libcsh_satdeploy_apm.so /root/.local/lib/csh/

# Build satdeploy-agent
echo "=== Building satdeploy-agent ==="
cd /satbuild/satdeploy-agent
if [ ! -d build ]; then
    meson setup build
fi
ninja -C build

echo ""
echo "=== Ready! ==="
echo ""
echo "  Ground station (sender):"
echo "    docker exec -it cshdev zmqproxy"
echo "    docker exec -it cshdev env HOME=/root csh"
echo "    In CSH: apm load"
echo ""
echo "  Target (receiver):"
echo "    docker exec -it cshdev /satbuild/satdeploy-agent/build/satdeploy-agent"
echo ""

exec bash

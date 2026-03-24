SUMMARY = "OTA deployment agent for embedded Linux satellites"
DESCRIPTION = "Runs on the satellite target, handles deploy/rollback/status \
commands over CSP (CubeSat Space Protocol). Communicates with the satdeploy \
ground station CLI via libcsp over ZMQ, CAN, or KISS serial."
HOMEPAGE = "https://github.com/MahmoodSeoud/satBuild"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${WORKDIR}/git/LICENSE;md5=5805f80cc7dfab9af68410863c49ae47"

# Fetch the main repo and all submodules explicitly.
# Using individual git:// entries instead of gitsm:// for reliable builds
# and proper source mirroring/license tracking.
SRC_URI = " \
    git://github.com/MahmoodSeoud/satBuild.git;protocol=https;branch=main;name=main \
    git://github.com/spaceinventor/libcsp.git;protocol=https;branch=master;name=csp;destsuffix=git/satdeploy-agent/lib/csp \
    git://github.com/spaceinventor/libparam.git;protocol=https;branch=master;name=param;destsuffix=git/satdeploy-agent/lib/param \
    git://github.com/MahmoodSeoud/libdtp.git;protocol=https;branch=main;name=dtp;destsuffix=git/satdeploy-agent/lib/dtp \
    git://github.com/spaceinventor/libossi.git;protocol=https;branch=master;name=ossi;destsuffix=git/satdeploy-agent/lib/dtp/extern/ossi \
"

SRCREV_FORMAT = "main"
SRCREV_main = "${AUTOREV}"
SRCREV_csp = "${AUTOREV}"
SRCREV_param = "${AUTOREV}"
SRCREV_dtp = "${AUTOREV}"
SRCREV_ossi = "${AUTOREV}"

PV = "0.1.0+git${SRCPV}"

S = "${WORKDIR}/git/satdeploy-agent"

inherit meson pkgconfig

DEPENDS = " \
    libzmq \
    libsocketcan \
    protobuf-c \
    protobuf-c-native \
    openssl \
    libbsd \
"

# Agent installs a single binary
FILES:${PN} = "${bindir}/satdeploy-agent"

# Meson options matching the project defaults
EXTRA_OEMESON = " \
    -Ddefault_library=static \
    -Dcsp:packet_padding_bytes=42 \
    -Dcsp:buffer_count=1000 \
    -Dcsp:buffer_size=2048 \
    -Dcsp:conn_max=20 \
    -Dcsp:conn_rxqueue_len=1000 \
    -Dcsp:qfifo_len=1000 \
    -Dcsp:rdp_max_window=1000 \
    -Dcsp:port_max_bind=32 \
    -Dcsp:use_rtable=true \
    -Dparam:have_fopen=true \
    -Dparam:list_dynamic=true \
    -Dparam:vmem_fram=false \
"

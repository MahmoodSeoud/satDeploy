project(
    'libsatpush',
    'c',
    version: '0.1.0',
    license: 'MIT',
    subproject_dir: 'lib',
    default_options: [
        'c_std=gnu11',
        'b_lto=true',
        'b_ndebug=if-release',
        'default_library=static',
        'warning_level=2',
    ],
    meson_version: '>=0.56.0',
)

# Public headers (exported to consumers via satpush_dep).
satpush_inc = include_directories('include')

# Private internal headers (sha256.h, session_state_internal.h,
# satpush_internal.h). These are visible inside libsatpush's compilation
# but never exposed via satpush_dep.
satpush_inc_internal = include_directories('src')

# Dependencies. libsatpush sits on top of libcsp (transport) and libdtp
# (selective-repeat ARQ). When built as a subproject of satdeploy-agent or
# satdeploy-apm, those parent projects already resolve csp and dtp_client.
# When built standalone, the consumer must provide them either via system
# pkg-config or via meson wrap files in libsatpush/lib/.
#
# The dependency names ('csp', 'dtp_client') and fallback subproject targets
# match the conventions used by satdeploy-agent/meson.build for cross-project
# consistency.
csp_dep = dependency('csp', fallback: ['csp', 'csp_dep'])
dtp_client_dep = dependency('dtp_client', fallback: ['dtp', 'dtp_client_dep'])

# Library sources. Step 2 of the extract moved dtp_client.c, session_state.c,
# and sha256.c from satdeploy-agent/src/, plus extracted the payload-
# registration wrapper from satdeploy-apm/src/satdeploy_apm.c into
# satpush_serve.c. Internal logic in satpush_pull.c is bit-identical to the
# pre-extract dtp_download_file - the F3.b regression baseline must not
# shift due to this move.
sources = files(
    'src/satpush_version.c',
    'src/satpush_ctx.c',
    'src/satpush_pull.c',
    'src/satpush_serve.c',
    'src/session_state.c',
    'src/sha256.c',
)

satpush_lib = static_library('satpush',
    sources,
    include_directories: [satpush_inc, satpush_inc_internal],
    dependencies: [csp_dep, dtp_client_dep],
    install: true,
    c_args: [
        '-Wall', '-Wextra',
        # Note: not setting -fvisibility=hidden here. If libsatpush is ever
        # built as a shared library, public symbols need explicit annotation
        # (a SATPUSH_API macro) - that is a follow-up if/when shared builds
        # are needed. For default_library=static the flag is effectively a
        # no-op anyway.
    ],
)

# Declared dependency for consumers that use libsatpush as a meson subproject.
# satdeploy-agent and satdeploy-apm will switch to consuming this in Step 4 of
# the extract.
satpush_dep = declare_dependency(
    link_with: satpush_lib,
    include_directories: satpush_inc,
    dependencies: [csp_dep, dtp_client_dep],
)

# Install public headers so pkg-config consumers can include <satpush/satpush.h>.
install_subdir('include/satpush', install_dir: get_option('includedir'))

# pkg-config file (per /plan-eng-review D3: meson subproject + pkg-config install).
# Consumers built outside the satdeploy repo discover libsatpush via
# `pkg-config --cflags --libs libsatpush`.
pkg = import('pkgconfig')
pkg.generate(satpush_lib,
    name: 'libsatpush',
    description: 'Cross-pass resumable file delivery over libcsp / libdtp',
    version: meson.project_version(),
    filebase: 'libsatpush',
    url: 'https://github.com/MahmoodSeoud/satdeploy/tree/main/libsatpush',
)

# Tests and examples land in Steps 5-6 of the extract. Uncomment when present.
# subdir('tests')
# subdir('examples')

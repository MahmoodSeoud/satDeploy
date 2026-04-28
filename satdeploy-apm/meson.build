project('satdeploy-apm', 'c', subproject_dir: 'lib', version: '0.4.0', default_options: [
  'default_library=static',
  'b_lto=false',
  'b_lundef=false',
  'csp:packet_padding_bytes=42',
  'csp:buffer_count=1000',
  'csp:buffer_size=2048',
  'csp:conn_max=20',
  'csp:conn_rxqueue_len=1000',
  'csp:qfifo_len=1000',
  'csp:rdp_max_window=1000',
  'csp:port_max_bind=16',
  'csp:use_rtable=true',
])

includes = include_directories('src')

# version.h is regenerated whenever meson reconfigures, so the embedded git rev
# reflects the source tree at setup time. Run `meson setup --reconfigure` after
# committing to refresh it before building a release artifact.
git_rev_result = run_command('git', 'rev-parse', '--short', 'HEAD', check: false)
git_rev = git_rev_result.returncode() == 0 ? git_rev_result.stdout().strip() : 'unknown'

version_h = configure_file(
    input: 'src/version.h.in',
    output: 'version.h',
    configuration: {
        'SATDEPLOY_VERSION': meson.project_version(),
        'SATDEPLOY_GIT_REV': git_rev,
    },
)

apm_csh_dep = dependency('apm_csh', fallback: ['apm_csh', 'apm_csh_dep'], required: true)
# CSP: headers + compile_args only — actual CSP functions come from CSH at runtime.
# Linking the static CSP lib would embed uninitialized CSP globals that shadow CSH's.
csp_dep = dependency('csp', fallback: ['csp', 'csp_dep'], required: true).partial_dependency(includes: true, compile_args: true)
slash_dep = dependency('slash', fallback: ['slash', 'slash_dep'], required: false).partial_dependency(links: false, includes: true)
# Param: headers only — actual param functions come from CSH at runtime.
# Same rationale as CSP above: linking static libparam embeds uninitialized
# globals that shadow CSH's initialized copies, causing segfault on dlopen.
param_dep = dependency('param', fallback: ['param', 'param_dep']).partial_dependency(includes: true, compile_args: true)
proto_c_dep = dependency('libprotobuf-c', fallback: ['protobuf-c', 'libprotobuf_c_dep'])
yaml_dep = dependency('yaml-0.1', fallback: ['libyaml', 'yaml_dep'], required: true)
sqlite3_dep = dependency('sqlite3', fallback: ['sqlite3', 'sqlite3_dep'], required: true)
# DTP: use as a proper subproject. The DTP subproject's meson.build detects
# is_subproject() and switches CSP/param to headers-only, so no static copies
# of those libraries get embedded. Their symbols resolve at dlopen time against
# CSH's initialized copies.
dtp_server_dep = dependency('dtp_server', fallback: ['dtp', 'dtp_server_dep'])

satdeploy_apm = shared_library(
    'csh_satdeploy_apm',
    [
      'src/satdeploy_apm.c',
      'src/sha256.c',
      'src/deploy.pb-c.c',
      'src/config.c',
      'src/output.c',
      'src/history.c',
      # dtp_log_quiet.c provides dbg_log_impl/dbg_warn_impl/dbg_enable_colors
      # before the linker reaches libdtp's dtp_log.o, so libdtp's unconditional
      # [LOG]/[WARN] printfs get short-circuited. See the file header for the
      # SATDEPLOY_DTP_VERBOSE escape hatch.
      'src/dtp_log_quiet.c',
      version_h,
    ],
    include_directories: [includes],
    dependencies: [apm_csh_dep, slash_dep, param_dep, csp_dep, proto_c_dep, yaml_dep, sqlite3_dep, dtp_server_dep],
    link_args: ['-lm'],
    install: true
)

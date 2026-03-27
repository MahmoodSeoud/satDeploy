project('satdeploy-apm', 'c', subproject_dir: 'lib', version: '0.1.0', default_options: [
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
apm_csh_dep = dependency('apm_csh', fallback: ['apm_csh', 'apm_csh_dep'], required: true)
csp_dep = dependency('csp', fallback: ['csp', 'csp_dep'], required: true)
slash_dep = dependency('slash', fallback: ['slash', 'slash_dep'], required: false).partial_dependency(links: false, includes: true)
param_dep = dependency('param', fallback: ['param', 'param_dep']).as_link_whole()
proto_c_dep = dependency('libprotobuf-c', fallback: ['protobuf-c', 'libprotobuf_c_dep'])
yaml_dep = dependency('yaml-0.1', fallback: ['libyaml', 'yaml_dep'], required: true)
sqlite3_dep = dependency('sqlite3', fallback: ['sqlite3', 'sqlite3_dep'], required: true)
dtp_dep = dependency('dtp', fallback: ['dtp', 'dtp_server_lib_dep'], required: true)

satdeploy_apm = shared_library(
    'csh_satdeploy_apm',
    [
      'src/satdeploy_apm.c',
      'src/sha256.c',
      'src/deploy.pb-c.c',
      'src/config.c',
      'src/output.c',
      'src/history.c',
    ],
    include_directories: includes,
    dependencies: [apm_csh_dep, slash_dep, param_dep, csp_dep, proto_c_dep, yaml_dep, sqlite3_dep, dtp_dep],
    link_args: ['-Wl,-Bsymbolic'],
    install: true
)

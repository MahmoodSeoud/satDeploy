project(
	'libdtp-bare-test',
	'c',
	version: '0.1.0',
	# Reuse the agent's submodules so we test the exact libdtp + libcsp + libparam
	# shipped by satdeploy. The point of the bare test is to exercise the same
	# libdtp the deployment uses, without satdeploy's wrapping.
	subproject_dir: '../../satdeploy-agent/lib',
	default_options: [
		'c_std=gnu11',
		'default_library=static',
		'csp:packet_padding_bytes=42',
		'csp:buffer_count=8192',
		'csp:buffer_size=2048',
		'csp:conn_max=20',
		'csp:conn_rxqueue_len=1000',
		'csp:qfifo_len=1000',
		'csp:rdp_max_window=1000',
		'csp:port_max_bind=32',
		'csp:use_rtable=true',
		'param:have_fopen=true',
		'param:list_dynamic=true',
		'param:vmem_fram=false',
	],
)

csp_dep      = dependency('csp',        fallback: ['csp',   'csp_dep'])
param_dep    = dependency('param',      fallback: ['param', 'param_dep'])
dtp_client_dep = dependency('dtp_client', fallback: ['dtp', 'dtp_client_dep'])
dtp_server_dep = dependency('dtp_server', fallback: ['dtp', 'dtp_server_dep'])
pthread_dep  = meson.get_compiler('c').find_library('pthread', required: true)
crypto_dep   = dependency('libcrypto', required: true)

common_deps = [csp_dep, param_dep, pthread_dep, crypto_dep]

executable(
	'bare-dtp-server',
	'src/server.c',
	'src/csp_init.c',
	dependencies: common_deps + [dtp_server_dep],
)

executable(
	'bare-dtp-client',
	'src/client.c',
	'src/csp_init.c',
	dependencies: common_deps + [dtp_client_dep],
)

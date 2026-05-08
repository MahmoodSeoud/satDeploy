project(
	'libdtp-bare-test',
	'c',
	version: '0.1.0',
	# subprojects/{csp,param,dtp} are symlinks to ../../satdeploy-agent/lib/*
	# so we test the exact libdtp + libcsp + libparam shipped by satdeploy
	# without satdeploy's wrapping. Meson doesn't allow relative
	# subproject_dir, so symlinks are the cheapest way to share the trees.
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

# Minimal CSP-over-ZMQ broker. We don't use the libcsp-bundled examples/zmqproxy
# because its capture thread has a heap-corruption bug (malloc(1024) too small
# for the CSP packet layout once buffer_size > 1024). That crash kills the
# broker mid-transfer and produces results that look like libdtp loss when
# they're actually a transport-broker bug. Our proxy only does zmq_proxy().
zmq_dep = dependency('libzmq', required: true)
executable(
	'mini-zmqproxy',
	'src/mini_zmqproxy.c',
	dependencies: [zmq_dep],
)

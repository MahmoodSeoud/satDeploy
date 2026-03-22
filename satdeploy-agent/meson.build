project(
	'satdeploy-agent',
	'c',
	version: '0.1.0',
	subproject_dir: 'lib',
	default_options: [
		'c_std=gnu11',
		'b_lto=false',
		'default_library=static',
		'csp:packet_padding_bytes=42',
		'csp:buffer_count=1000',
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

sources = files(
	'src/main.c',
	'src/deploy_handler.c',
	'src/backup_manager.c',
	'src/app_metadata.c',
	'src/dtp_client.c',
	'proto/deploy.pb-c.c',
)

dirs = include_directories(
	'include',
	'proto',
)

csp_dep = dependency('csp', fallback: ['csp', 'csp_dep'])
param_dep = dependency('param', fallback: ['param', 'param_dep'])
dtp_client_dep = dependency('dtp_client', fallback: ['dtp', 'dtp_client_dep'])
proto_c_dep = dependency('libprotobuf-c')
openssl_dep = dependency('openssl')
m_dep = meson.get_compiler('c').find_library('m', required: false)
pthread_dep = meson.get_compiler('c').find_library('pthread', required: true)

deps = [csp_dep, param_dep, dtp_client_dep, proto_c_dep, openssl_dep, m_dep, pthread_dep]

satdeploy_agent = executable(
	'satdeploy-agent',
	sources,
	include_directories: dirs,
	dependencies: deps,
	install: true,
	c_args: ['-g2', '-O0', '-Wall', '-Wextra'],
	link_args: ['-ldl'],
)

project(
	'satdeploy-agent',
	'c',
	version: '0.4.0',
	subproject_dir: 'lib',
	default_options: [
		'c_std=gnu11',
		'b_lto=true',
		'b_ndebug=if-release',
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

sources = files(
	'src/main.c',
	'src/deploy_handler.c',
	'src/backup_manager.c',
	'src/app_metadata.c',
	'src/dtp_client.c',
	'src/session_state.c',
	'src/sha256.c',
	# Same trick as the APM: providing dbg_log_impl/dbg_warn_impl ourselves
	# stops libdtp's dtp_log.o from leaking [LOG]/[WARN] into operator output
	# during a deploy. SATDEPLOY_DTP_VERBOSE=1 turns it back on for debugging.
	'src/dtp_log_quiet.c',
	'proto/deploy.pb-c.c',
)

# Test-only loss_filter scaffolding for thesis trace-driven experiments.
# See include/loss_filter.h and include/loss_filter.integration.md.
#
# Architecture (since v0.4.0): the filter is hooked at the libcsp router
# entry point via linker `--wrap=csp_qfifo_write`. That single interception
# sees every CSP packet on every interface (ZMQ, CAN, KISS, etc.) regardless
# of which subsystem is reading from the resulting connection. The earlier
# per-call-site hooks (deploy_handler.c, libdtp's recv loop) are no longer
# needed and the SATDEPLOY_TEST_LOSS_FILTER macro is NOT defined globally —
# only the agent's own sources see it. libdtp builds byte-identical to
# upstream regardless of this option.
#
# Flight builds: `-Dtest_loss_filter=false` (the default) keeps the wrap
# link arg unset, so the agent links against the real csp_qfifo_write
# directly. Zero overhead at runtime, zero footprint on disk.
test_loss_filter = get_option('test_loss_filter')
test_loss_filter_args = []
test_loss_filter_link_args = []
if test_loss_filter
	sources += files(
		'src/loss_filter.c',
		'src/loss_filter_iface_hook.c',
	)
	test_loss_filter_args += '-DSATDEPLOY_TEST_LOSS_FILTER'
	# Route every csp_qfifo_write call through __wrap_csp_qfifo_write.
	# The original symbol becomes __real_csp_qfifo_write for the wrapper
	# to call after the drop / latency decision.
	test_loss_filter_link_args += '-Wl,--wrap=csp_qfifo_write'
endif

# F3.b naive-baseline gate. Local to the agent — the change is in
# src/dtp_client.c (the agent's wrapper around libdtp's transfer call),
# not in libdtp itself. So no add_global_arguments — flight libdtp stays
# byte-identical, only the agent's retry/resume scaffolding gets disabled.
naive_baseline = get_option('naive_baseline')
naive_baseline_args = []
if naive_baseline
	naive_baseline_args += '-DSATDEPLOY_NAIVE_BASELINE'
endif

# version.h is regenerated whenever meson reconfigures, so the embedded git rev
# reflects the source tree at setup time. Run `meson setup --reconfigure` after
# committing to refresh it before building a release artifact.
git_rev_result = run_command('git', 'rev-parse', '--short', 'HEAD', check: false)
git_rev = git_rev_result.returncode() == 0 ? git_rev_result.stdout().strip() : 'unknown'

version_h = configure_file(
	input: 'include/version.h.in',
	output: 'version.h',
	configuration: {
		'SATDEPLOY_VERSION': meson.project_version(),
		'SATDEPLOY_GIT_REV': git_rev,
	},
)

dirs = include_directories(
	'include',
	'proto',
)

csp_dep = dependency('csp', fallback: ['csp', 'csp_dep'])
param_dep = dependency('param', fallback: ['param', 'param_dep'])
dtp_client_dep = dependency('dtp_client', fallback: ['dtp', 'dtp_client_dep'])
proto_c_dep = dependency('libprotobuf-c')
m_dep = meson.get_compiler('c').find_library('m', required: false)
pthread_dep = meson.get_compiler('c').find_library('pthread', required: true)

deps = [csp_dep, param_dep, dtp_client_dep, proto_c_dep, m_dep, pthread_dep]

satdeploy_agent = executable(
	'satdeploy-agent',
	sources,
	version_h,
	include_directories: dirs,
	dependencies: deps,
	install: true,
	c_args: [
		'-Os',
		'-Wall', '-Wextra',
		'-ffunction-sections',
		'-fdata-sections',
		'-fvisibility=hidden',
		'-fno-asynchronous-unwind-tables',
		'-fno-unwind-tables',
	] + test_loss_filter_args + naive_baseline_args,
	link_args: [
		'-ldl',
		'-Wl,--gc-sections',
		'-Wl,--build-id=none',
		'-Wl,-s',
	] + test_loss_filter_link_args,
)

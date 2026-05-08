/*
 * bare-dtp-server — minimal libdtp server.
 *
 * Registers one file as a payload and runs dtp_server_main until SIGTERM.
 * No protobuf, no slash module, no retry-loop awareness — purely the libdtp
 * server-side primitives that the satdeploy APM also uses, with everything
 * else stripped away.
 *
 * Usage: bare-dtp-server <node> <payload_id> <file>
 *
 * Defaults zmqproxy ports to 6000 (sub) / 7000 (pub) — override with
 * BARE_ZMQ_SUB_PORT / BARE_ZMQ_PUB_PORT env vars.
 */

#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <dtp/dtp.h>
#include <dtp/dtp_file_payload.h>
#include <dtp/dtp_protocol.h>

#include "csp_init.h"

/*
 * libdtp's dtp_server_main resolves payload metadata via this hook. The
 * file-payload helper is the canonical implementation — same override the
 * APM installs.
 */
bool get_payload_meta(dtp_payload_meta_t *meta, uint8_t payload_id) {
	return dtp_file_payload_get_meta(meta, payload_id);
}

static volatile bool exit_flag = false;

static void on_signal(int sig) {
	(void)sig;
	exit_flag = true;
}

static uint16_t env_port(const char *name, uint16_t fallback) {
	const char *s = getenv(name);
	if (!s || !*s) return fallback;
	long v = strtol(s, NULL, 10);
	if (v <= 0 || v > 65535) return fallback;
	return (uint16_t)v;
}

int main(int argc, char **argv) {
	if (argc != 4) {
		fprintf(stderr, "usage: %s <node> <payload_id> <file>\n", argv[0]);
		return 2;
	}

	int node = atoi(argv[1]);
	int payload_id_int = atoi(argv[2]);
	const char *path = argv[3];

	if (node <= 0 || payload_id_int < 0 || payload_id_int > 255) {
		fprintf(stderr, "bad node or payload_id\n");
		return 2;
	}
	uint8_t payload_id = (uint8_t)payload_id_int;

	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);

	uint16_t sub_port = env_port("BARE_ZMQ_SUB_PORT", 6000);
	uint16_t pub_port = env_port("BARE_ZMQ_PUB_PORT", 7000);

	if (bare_csp_bringup("bare-dtp-server", node, 8, sub_port, pub_port) != 0) {
		return 1;
	}

	/* Register the file. The APM uses del-then-add to refresh; for a fresh
	 * server we just add. */
	if (!dtp_file_payload_add(payload_id, path)) {
		fprintf(stderr, "dtp_file_payload_add(%u, %s) failed\n", payload_id, path);
		return 1;
	}

	fprintf(stderr, "bare-dtp-server: node=%d payload_id=%u file=%s sub=%u pub=%u\n",
	        node, payload_id, path, sub_port, pub_port);

	/* Block until exit_flag flips. dtp_server_main is the same call the APM
	 * runs in its server thread — single function, no internal retry. */
	dtp_server_main((bool *)&exit_flag);

	dtp_file_payload_del(payload_id);
	return 0;
}

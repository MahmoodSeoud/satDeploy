/*
 * mini-zmqproxy — minimal CSP-over-ZMQ broker for the bare libdtp test.
 *
 * The libcsp-bundled examples/zmqproxy has a heap-corruption bug in its
 * unconditional packet-logging thread (malloc(1024) too small for our CSP
 * packet layout). That bug brings down the whole process and breaks the
 * pub/sub broker, which is unrelated to anything we're trying to measure.
 *
 * This binary is the proxy logic with nothing else: bind XSUB on 6000,
 * XPUB on 7000, run zmq_proxy. No capture, no allocation of csp_packet_t.
 */

#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <zmq.h>

static void *g_ctx = NULL;

static void on_signal(int sig) {
	(void)sig;
	if (g_ctx) zmq_ctx_shutdown(g_ctx);
}

int main(void) {
	const char *sub_addr = "tcp://0.0.0.0:6000";
	const char *pub_addr = "tcp://0.0.0.0:7000";

	signal(SIGINT,  on_signal);
	signal(SIGTERM, on_signal);

	g_ctx = zmq_ctx_new();
	if (!g_ctx) { perror("zmq_ctx_new"); return 1; }

	void *frontend = zmq_socket(g_ctx, ZMQ_XSUB);
	if (!frontend) { perror("zmq_socket XSUB"); return 1; }
	if (zmq_bind(frontend, sub_addr) < 0) { perror("zmq_bind sub"); return 1; }

	void *backend = zmq_socket(g_ctx, ZMQ_XPUB);
	if (!backend) { perror("zmq_socket XPUB"); return 1; }
	if (zmq_bind(backend, pub_addr) < 0) { perror("zmq_bind pub"); return 1; }

	fprintf(stderr, "mini-zmqproxy: sub=%s pub=%s\n", sub_addr, pub_addr);

	zmq_proxy(frontend, backend, NULL);

	zmq_close(frontend);
	zmq_close(backend);
	zmq_ctx_destroy(g_ctx);
	return 0;
}

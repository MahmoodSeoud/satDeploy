/*
 * bare-dtp-client — minimal libdtp client.
 *
 * Single dtp_start_transfer, no retry loop, no resume, no patching of
 * request_meta.intervals[]. Default intervals = [(0, UINT32_MAX)] meaning
 * "send the whole file." This is libdtp's API at its plainest — exactly
 * what a first-time integrator would write before adding any of the
 * workarounds that sit in satdeploy-agent/src/dtp_client.c.
 *
 * The point is to falsify or confirm the tail-race claim independently
 * of satdeploy's wrapping.
 *
 * Usage: bare-dtp-client <server_node> <payload_id> <expected_size> <out_file>
 *
 * Stdout: one JSON line on a successful run, e.g.
 *   {"rc":0,"got":1024,"expected":1024,"gap":0,"bytes_written":1048576,
 *    "duration_ms":42,"sha256":"...","first_gap_seq":-1,"last_gap_seq":-1}
 * rc != 0 means dtp_start_transfer returned non-OK; gap != 0 means the
 * receive bitmap had holes after the single transfer attempt.
 *
 * No stdout output is produced on internal failure — the harness watches
 * the exit code in that case.
 */

#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <openssl/sha.h>

#include <csp/csp.h>
#include <dtp/dtp.h>
#include <dtp/dtp_session.h>
#include <dtp/dtp_protocol.h>

#include "csp_init.h"

/* Defaults match satdeploy-agent's DTP_DEFAULT_*. Throughput is non-zero to
 * avoid div-by-zero in libdtp's compute_dtp_metrics. */
#define BARE_MTU         1024
#define BARE_THROUGHPUT  10000000u
#define BARE_TIMEOUT_S   30

typedef struct {
	FILE *fp;
	uint32_t expected_size;
	uint32_t bytes_written;
	uint32_t nof_packets;
	uint16_t effective_mtu;
	uint8_t *bitmap;          /* 1 bit per packet seq */
	size_t   bitmap_bytes;
	int      error;
} client_ctx_t;

static void mark_seq(client_ctx_t *ctx, uint32_t seq) {
	if (!ctx->bitmap) return;
	if (seq >= ctx->nof_packets) return;
	ctx->bitmap[seq >> 3] |= (uint8_t)(1u << (seq & 7));
}

static uint32_t count_received(const client_ctx_t *ctx) {
	if (!ctx->bitmap) return 0;
	uint32_t n = 0;
	for (size_t i = 0; i < ctx->bitmap_bytes; i++) {
		uint8_t b = ctx->bitmap[i];
		while (b) { n += (b & 1); b >>= 1; }
	}
	return n;
}

/* Find the first and last missing seqs in [0, nof_packets). Helps the
 * harness classify where gaps actually land — tail vs middle vs head. */
static void find_gap_extents(const client_ctx_t *ctx, int64_t *first, int64_t *last) {
	*first = -1; *last = -1;
	if (!ctx->bitmap || ctx->nof_packets == 0) return;
	for (uint32_t s = 0; s < ctx->nof_packets; s++) {
		bool got = (ctx->bitmap[s >> 3] >> (s & 7)) & 1;
		if (!got) {
			if (*first < 0) *first = (int64_t)s;
			*last = (int64_t)s;
		}
	}
}

static bool on_data(dtp_t *session, csp_packet_t *packet) {
	client_ctx_t *ctx = (client_ctx_t *)dtp_session_get_user_ctx(session);
	if (!ctx || !ctx->fp) return false;
	if (!packet || packet->length < 2 * sizeof(uint32_t)) return true;

	dtp_on_data_info_t info = dtp_get_data_info(session, packet);

	if (fseek(ctx->fp, info.data_offset, SEEK_SET) != 0) {
		ctx->error = -1;
		return false;
	}
	size_t written = fwrite(info.data, 1, info.data_length, ctx->fp);
	if (written != info.data_length) {
		ctx->error = -1;
		return false;
	}
	ctx->bytes_written += (uint32_t)written;
	mark_seq(ctx, info.packet_sequence_number);
	return true;
}

static int sha256_of_file(const char *path, char out_hex[65]) {
	FILE *f = fopen(path, "rb");
	if (!f) return -1;
	SHA256_CTX h;
	SHA256_Init(&h);
	uint8_t buf[8192];
	size_t n;
	while ((n = fread(buf, 1, sizeof buf, f)) > 0) {
		SHA256_Update(&h, buf, n);
	}
	uint8_t md[SHA256_DIGEST_LENGTH];
	SHA256_Final(md, &h);
	fclose(f);
	for (int i = 0; i < SHA256_DIGEST_LENGTH; i++) {
		snprintf(out_hex + i * 2, 3, "%02x", md[i]);
	}
	out_hex[64] = '\0';
	return 0;
}

static long now_ms(void) {
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

static uint16_t env_port(const char *name, uint16_t fallback) {
	const char *s = getenv(name);
	if (!s || !*s) return fallback;
	long v = strtol(s, NULL, 10);
	if (v <= 0 || v > 65535) return fallback;
	return (uint16_t)v;
}

int main(int argc, char **argv) {
	if (argc != 5) {
		fprintf(stderr, "usage: %s <server_node> <payload_id> <expected_size> <out_file>\n",
		        argv[0]);
		return 2;
	}

	uint32_t server_node   = (uint32_t)strtoul(argv[1], NULL, 10);
	int payload_id_int     = atoi(argv[2]);
	uint32_t expected_size = (uint32_t)strtoul(argv[3], NULL, 10);
	const char *out_path   = argv[4];

	if (payload_id_int < 0 || payload_id_int > 255 || expected_size == 0) {
		fprintf(stderr, "bad args\n");
		return 2;
	}
	uint8_t payload_id = (uint8_t)payload_id_int;

	int client_node = atoi(getenv("BARE_CLIENT_NODE") ?: "5425");
	uint16_t sub_port = env_port("BARE_ZMQ_SUB_PORT", 6000);
	uint16_t pub_port = env_port("BARE_ZMQ_PUB_PORT", 7000);

	if (bare_csp_bringup("bare-dtp-client", client_node, 8, sub_port, pub_port) != 0) {
		return 1;
	}

	uint16_t eff_mtu = BARE_MTU - 8;  /* DTP header is 2x uint32 */
	uint32_t nof_packets = (expected_size + eff_mtu - 1) / eff_mtu;
	size_t bitmap_bytes = (nof_packets + 7) / 8;
	uint8_t *bitmap = calloc(1, bitmap_bytes);
	if (!bitmap) return 1;

	FILE *fp = fopen(out_path, "wb");
	if (!fp) { free(bitmap); return 1; }

	client_ctx_t ctx = {
		.fp = fp,
		.expected_size = expected_size,
		.nof_packets = nof_packets,
		.effective_mtu = eff_mtu,
		.bitmap = bitmap,
		.bitmap_bytes = bitmap_bytes,
	};

	long t0 = now_ms();

	/* Plain libdtp client: no resume, no hooks beyond on_data, no retry,
	 * no patching of request_meta. The default intervals after
	 * dtp_prepare_session ask for the whole file. */
	dtp_t *session = dtp_prepare_session(
		(uint16_t)server_node,
		0xBA8E7E57,         /* arbitrary fixed session id */
		BARE_THROUGHPUT,
		BARE_TIMEOUT_S,
		payload_id,
		NULL,
		BARE_MTU,
		false,              /* libdtp resume off */
		0                   /* keep_alive_interval */
	);
	if (!session) {
		fprintf(stderr, "dtp_prepare_session returned NULL\n");
		fclose(fp); free(bitmap);
		return 1;
	}

	dtp_session_set_user_ctx(session, &ctx);
	dtp_params hooks = {
		.hooks = {
			.on_data_packet = on_data,
			.hook_ctx = &ctx,
		}
	};
	dtp_set_opt(session, DTP_SESSION_HOOKS_CFG, &hooks);

	int rc = dtp_start_transfer(session);
	dtp_release_session(session);
	fclose(fp);

	long duration_ms = now_ms() - t0;

	uint32_t got = count_received(&ctx);
	int64_t first_gap = -1, last_gap = -1;
	find_gap_extents(&ctx, &first_gap, &last_gap);
	uint32_t gap = (got >= nof_packets) ? 0 : (nof_packets - got);

	char digest[65] = {0};
	(void)sha256_of_file(out_path, digest);

	printf("{\"rc\":%d,\"got\":%u,\"expected\":%u,\"gap\":%u,"
	       "\"bytes_written\":%u,\"duration_ms\":%ld,\"sha256\":\"%s\","
	       "\"first_gap_seq\":%" PRId64 ",\"last_gap_seq\":%" PRId64 "}\n",
	       rc, got, nof_packets, gap,
	       ctx.bytes_written, duration_ms, digest,
	       first_gap, last_gap);
	fflush(stdout);

	free(bitmap);
	return 0;
}

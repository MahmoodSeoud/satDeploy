/**
 * DTP client wrapper - Downloads files via DTP protocol
 *
 * Uses the lower-level DTP API (prepare + hooks + start) instead of
 * dtp_client_main to ensure hooks are set before the transfer begins.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <time.h>
#include <unistd.h>  /* fsync */

#include <csp/csp.h>
#include <dtp/dtp.h>
#include <dtp/dtp_session.h>
#include <dtp/dtp_protocol.h>

#include "satdeploy_agent.h"
#include "session_state.h"

/* DTP configuration defaults */
#define DTP_DEFAULT_TIMEOUT_S    60
#define DTP_DEFAULT_MTU          1024
#define DTP_DEFAULT_THROUGHPUT   10000000  /* bytes/s — must be non-zero to avoid div-by-zero in compute_dtp_metrics */

/* Maximum retry rounds for missing-interval re-requests.
 * DTP's data path is connectionless (by design — see lib/dtp/README.rst).
 * The protocol's reliability story is "ask again for what's missing," using
 * request_meta.intervals[]. The protocol caps that at 8 intervals per request,
 * so fragmented loss may need several rounds to fully patch.
 *
 * SATDEPLOY_NAIVE_BASELINE is the F3.b thesis-experiment build: caps retries
 * to 0 so the very first dtp_start_transfer is the only attempt. Combined with
 * the resume gates further down, this models a non-DTP single-shot CSP upload
 * — the comparison curve the F3 chart needs. */
#ifdef SATDEPLOY_NAIVE_BASELINE
#  define DTP_MAX_RETRY_ROUNDS   0
#else
#  define DTP_MAX_RETRY_ROUNDS   8
#endif

/* Context for file download */
typedef struct {
    FILE *fp;
    uint32_t bytes_written;     /* total fwrite bytes (may double-count overwrites) */
    uint32_t expected_size;
    int error;
    /* Receive bitmap indexed by DTP packet sequence number. 1 bit per packet.
     * Allocated in dtp_download_file() based on expected_size and effective MTU. */
    uint8_t *recv_bitmap;
    uint32_t nof_packets;       /* total packets expected (== bitmap_bits) */
    uint16_t effective_mtu;     /* mtu - 8 (DTP header is 2 × uint32_t) */
} download_ctx_t;

static inline void mark_seq_received(download_ctx_t *ctx, uint32_t seq) {
    if (seq >= ctx->nof_packets) return;
    ctx->recv_bitmap[seq >> 3] |= (uint8_t)(1u << (seq & 7));
}

static inline int seq_is_received(const download_ctx_t *ctx, uint32_t seq) {
    if (seq >= ctx->nof_packets) return 1;
    return (ctx->recv_bitmap[seq >> 3] >> (seq & 7)) & 1;
}

/* Fill `out` with up to 8 missing-seq ranges. Returns count.
 * Each interval is inclusive on both ends (matches DTP server semantics). */
static uint8_t compute_missing_intervals(const download_ctx_t *ctx,
                                         interval_t out[8]) {
    uint8_t count = 0;
    uint32_t i = 0;
    while (count < 8 && i < ctx->nof_packets) {
        while (i < ctx->nof_packets && seq_is_received(ctx, i)) i++;
        if (i >= ctx->nof_packets) break;
        uint32_t gap_start = i;
        while (i < ctx->nof_packets && !seq_is_received(ctx, i)) i++;
        out[count].start = gap_start;
        out[count].end   = i - 1;
        count++;
    }
    return count;
}

static uint32_t count_received_packets(const download_ctx_t *ctx) {
    uint32_t n = 0;
    for (uint32_t i = 0; i < ctx->nof_packets; i++) {
        if (seq_is_received(ctx, i)) n++;
    }
    return n;
}

/**
 * DTP on_start callback - called when transfer begins.
 */
static void on_download_start(dtp_t *session) {
    download_ctx_t *ctx = (download_ctx_t *)dtp_session_get_user_ctx(session);
    if (ctx) {
        printf("[dtp]    downloading %u bytes...\n", ctx->expected_size);
        fflush(stdout);
    }
}

/**
 * DTP on_data_packet callback - called for each received data packet.
 * Writes data to file.
 */
static bool on_download_data(dtp_t *session, csp_packet_t *packet) {
    download_ctx_t *ctx = (download_ctx_t *)dtp_session_get_user_ctx(session);
    if (ctx == NULL || ctx->fp == NULL) {
        return false;  /* Abort transfer */
    }

    if (packet == NULL || packet->length == 0) {
        return true;  /* Continue, empty packet */
    }

    /* Guard against integer underflow in dtp_get_data_info():
       packet must have at least 2 × uint32_t (8 bytes) for the header */
    if (packet->length < 2 * sizeof(uint32_t)) {
        return true;  /* Skip malformed packet, continue transfer */
    }

    /* Use DTP helper to extract data info */
    dtp_on_data_info_t info = dtp_get_data_info(session, packet);

    /* Seek to correct position and write. fseek+fwrite at info.data_offset
     * makes re-receiving the same packet a harmless no-op overwrite, which
     * is what we want when re-requesting missing intervals. */
    if (fseek(ctx->fp, info.data_offset, SEEK_SET) != 0) {
        ctx->error = -1;
        return false;
    }

    size_t written = fwrite(info.data, 1, info.data_length, ctx->fp);
    if (written != info.data_length) {
        ctx->error = -1;
        return false;
    }

    ctx->bytes_written += written;
    mark_seq_received(ctx, info.packet_sequence_number);
    return true;  /* Continue transfer */
}

/**
 * DTP on_end callback - called when transfer completes or fails.
 */
static void on_download_end(dtp_t *session) {
    download_ctx_t *ctx = (download_ctx_t *)dtp_session_get_user_ctx(session);
    if (ctx) {
        /* logged in dtp_download_file after cleanup */
        (void)ctx;
    }
}

/**
 * DTP on_release callback - called when session is released.
 */
static void on_download_release(dtp_t *session) {
    (void)session;
    /* Nothing to clean up - context is on stack */
}

int dtp_download_file(uint32_t server_node, uint8_t payload_id,
                      const char *dest_path, uint32_t expected_size,
                      const char *expected_hash, const char *app_name,
                      uint16_t mtu, uint32_t throughput, uint8_t timeout) {
    if (dest_path == NULL) {
        return -1;
    }

    /* Apply defaults for zero values */
    if (mtu == 0)        mtu = DTP_DEFAULT_MTU;
    if (throughput == 0) throughput = DTP_DEFAULT_THROUGHPUT;
    if (timeout == 0)    timeout = DTP_DEFAULT_TIMEOUT_S;

    /* Setup download context, including the receive bitmap.
     * Effective payload per packet is (mtu - 8) — DTP prepends two 32-bit
     * words (sequence + offset) to the CSP packet payload. */
    uint16_t eff_mtu = (mtu > 8) ? (uint16_t)(mtu - 8) : 1;
    uint32_t nof_packets = expected_size ? ((expected_size + eff_mtu - 1) / eff_mtu) : 0;
    size_t bitmap_bytes = (nof_packets + 7) / 8;
    uint8_t *bitmap = NULL;
    if (bitmap_bytes > 0) {
        bitmap = calloc(1, bitmap_bytes);
        if (!bitmap) {
            printf("\033[31m[dtp]    error: bitmap alloc failed (%zu bytes)\033[0m\n", bitmap_bytes);
            return -1;
        }
    }

    /* Resolve the cross-pass state sidecar path. We attempt resume only when
     * we have all of (app_name, expected_hash, non-zero size). Strict-equality
     * load means a re-staged binary (different SHA256 for the same app) will
     * blow away stale state instead of inheriting a poisoned bitmap. */
    char state_path[640];
    bool have_state_path = (app_name != NULL && expected_hash != NULL &&
                            expected_size > 0 && bitmap_bytes > 0 &&
                            session_state_path(app_name, state_path, sizeof(state_path)) == 0);

    bool resumed = false;
#ifndef SATDEPLOY_NAIVE_BASELINE
    /* F3.b naive baseline skips the sidecar entirely — no cross-pass resume,
     * every push starts at byte 0. Leaving this loop in would let the naive
     * curve "cheat" via state from a prior trial. */
    if (have_state_path) {
        if (session_state_load(state_path, expected_size, expected_hash,
                               nof_packets, eff_mtu,
                               bitmap, bitmap_bytes) == 1) {
            resumed = true;
        }
    }
#endif

    /* Open destination file. On resume we must NOT truncate — the previously
     * received packet payloads at their byte offsets are the whole reason we
     * persisted the bitmap. fseek+fwrite at the recorded offsets only works
     * if the underlying bytes survive across the open(). */
    FILE *fp = NULL;
    if (resumed) {
        fp = fopen(dest_path, "r+b");
        if (fp == NULL) {
            /* Sidecar referenced a temp file that no longer exists (operator
             * cleaned /tmp, agent rebooted on tmpfs, etc.). Discard the state
             * and start fresh. */
            printf("[dtp]    sidecar present but %s missing — starting fresh\n", dest_path);
            session_state_unlink(state_path);
            memset(bitmap, 0, bitmap_bytes);
            resumed = false;
            fp = fopen(dest_path, "wb");
        }
    } else {
        fp = fopen(dest_path, "wb");
    }
    if (fp == NULL) {
        printf("\033[31m[dtp]    error: failed to open %s\033[0m\n", dest_path);
        free(bitmap);
        return -1;
    }

    download_ctx_t ctx = {
        .fp = fp,
        .bytes_written = 0,
        .expected_size = expected_size,
        .error = 0,
        .recv_bitmap = bitmap,
        .nof_packets = nof_packets,
        .effective_mtu = eff_mtu,
    };

    /* Deterministic session_id: SHA256(app_name || ":" || expected_hash)[0:4].
     * Stable across processes and reboots, so ground and agent agree without
     * a negotiation step. Falls back to a time-based id when caller didn't
     * supply identification (legacy callers). */
    uint32_t session_id;
    if (app_name && expected_hash) {
        session_id = session_state_compute_id(app_name, expected_hash);
    } else {
        static uint32_t session_counter = 0;
        session_id = (uint32_t)time(NULL) ^ (++session_counter << 16);
    }

    dtp_t *session = dtp_prepare_session(
        server_node,
        session_id,
        throughput,
        timeout,
        payload_id,
        NULL,              /* ctx - set below */
        mtu,
        false,             /* libdtp resume — we manage state ourselves via the bitmap */
        0                  /* keep_alive_interval */
    );

    if (session == NULL) {
        printf("\033[31m[dtp]    error: failed to create session\033[0m\n");
        fclose(fp);
        free(ctx.recv_bitmap);
        return -1;
    }

    /* Set user context and hooks BEFORE starting the transfer */
    dtp_session_set_user_ctx(session, &ctx);

    dtp_params hooks = {
        .hooks = {
            .on_start = on_download_start,
            .on_data_packet = on_download_data,
            .on_end = on_download_end,
            .on_release = on_download_release,
            .hook_ctx = &ctx
        }
    };
    dtp_set_opt(session, DTP_SESSION_HOOKS_CFG, &hooks);

    /* If we resumed and already have full coverage from disk, skip the
     * transfer entirely. Otherwise pre-patch request_meta so the very first
     * dtp_start_transfer asks only for the gaps — without this, the default
     * intervals[0]=(0, UINT32_MAX) would re-request the whole file and waste
     * a pass-window's worth of bandwidth. */
    uint32_t got_initial = ctx.recv_bitmap ? count_received_packets(&ctx) : 0;
    bool already_complete = (ctx.nof_packets == 0) || (got_initial >= ctx.nof_packets);

    if (resumed && !already_complete) {
        interval_t gaps[8];
        uint8_t n = compute_missing_intervals(&ctx, gaps);
        if (n > 0) {
            for (uint8_t i = 0; i < n; i++) {
                session->request_meta.intervals[i] = gaps[i];
            }
            session->request_meta.nof_intervals = n;
            printf("[dtp]    resume session=%08x: %u/%u packets from sidecar, requesting %u gap(s)\n",
                   session_id, got_initial, ctx.nof_packets, n);
            fflush(stdout);
        }
    }

    int result = DTP_OK;
    int round = 0;
    int hard_error = 0;

    if (!already_complete) {
        result = dtp_start_transfer(session);

        while (ctx.recv_bitmap && ctx.error == 0 &&
               round < DTP_MAX_RETRY_ROUNDS) {
            if (result != DTP_OK && result != DTP_CANCELLED) {
                /* Connection-level failure (no server, route gone, etc.).
                 * Retrying the same intervals on a transport that just rejected us
                 * is unlikely to help and risks an infinite loop, so bail. */
                hard_error = result;
                break;
            }

            uint32_t got = count_received_packets(&ctx);
            if (got >= ctx.nof_packets) {
                break;  /* full coverage */
            }

            interval_t gaps[8];
            uint8_t n = compute_missing_intervals(&ctx, gaps);
            if (n == 0) {
                break;  /* shouldn't happen if got < nof_packets, but defensive */
            }

            printf("[dtp]    round %d: %u/%u packets received, re-requesting %u gap(s)\n",
                   round + 1, got, ctx.nof_packets, n);
            for (uint8_t i = 0; i < n; i++) {
                printf("[dtp]      gap %u: seq [%u..%u]\n", i, gaps[i].start, gaps[i].end);
            }
            fflush(stdout);

            /* Patch the request_meta in place. dtp_start_transfer re-sends it on
             * the META control socket each call, so the server picks up the new
             * interval list for this round. */
            for (uint8_t i = 0; i < n; i++) {
                session->request_meta.intervals[i] = gaps[i];
            }
            session->request_meta.nof_intervals = n;

            result = dtp_start_transfer(session);
            round++;
        }
    }

    /* Final coverage check from the bitmap — this is the source of truth,
     * not bytes_written (which double-counts overlapping retransmissions). */
    uint32_t got = ctx.recv_bitmap ? count_received_packets(&ctx) : 0;
    bool fully_received = (ctx.nof_packets == 0) || (got >= ctx.nof_packets);

    /* Cleanup (after all retries — we needed the session alive between rounds). */
    dtp_release_session(session);

    /* fflush+fsync before persisting the bitmap so the on-disk file actually
     * contains the bytes the bitmap claims. Otherwise resume could return
     * "yes you already received seq N" while the destination still holds
     * page-cache or zero-fill at that offset. */
    if (fp != NULL) {
        fflush(fp);
        int fd = fileno(fp);
        if (fd >= 0) {
            (void)fsync(fd);
        }
        fclose(fp);
    }

    /* Sidecar lifecycle:
     *   - write error (ctx.error) → corrupt on-disk file, do nothing; the
     *     temp will be unlinked by the deploy_handler error path
     *   - full success → unlink (next deploy starts clean)
     *   - partial coverage (with or without hard_error) → save the bitmap
     *     so the next pass for the same (app, hash) picks up the gaps. We
     *     deliberately persist on hard_error too: a UHF link drop mid-pass
     *     is the canonical case this whole feature exists for. */
#ifndef SATDEPLOY_NAIVE_BASELINE
    if (have_state_path && ctx.error == 0) {
        if (fully_received) {
            session_state_unlink(state_path);
        } else if (ctx.recv_bitmap && got > 0) {
            int rc = session_state_save(state_path, expected_size, expected_hash,
                                        ctx.nof_packets, ctx.effective_mtu,
                                        ctx.recv_bitmap, bitmap_bytes);
            if (rc == 0) {
                printf("[dtp]    persisted %u/%u packets to %s for next pass\n",
                       got, ctx.nof_packets, state_path);
                fflush(stdout);
            }
        }
    }
#else
    /* F3.b naive baseline: never persist sidecars. A failed naive trial
     * must NOT leave breadcrumbs that help the next trial — that would
     * smuggle satdeploy's resume back in and contaminate the comparison. */
    if (have_state_path) {
        session_state_unlink(state_path);
    }
#endif

    free(ctx.recv_bitmap);

    if (ctx.error != 0) {
        printf("\033[31m[dtp]    error: write failed\033[0m\n");
        return -1;
    }
    if (hard_error) {
        printf("\033[31m[dtp]    error: download failed (status=%d)\033[0m\n", hard_error);
        return -1;
    }
    if (!fully_received) {
#ifdef SATDEPLOY_NAIVE_BASELINE
        printf("\033[31m[dtp]    error: incomplete after %d retry round(s) (%u/%u packets) — naive baseline, no resume\033[0m\n",
               round, got, ctx.nof_packets);
#else
        printf("\033[31m[dtp]    error: incomplete after %d retry round(s) (%u/%u packets) — state saved for resume\033[0m\n",
               round, got, ctx.nof_packets);
#endif
        return -1;
    }

    printf("[dtp]    complete (%u packets, %d retry round(s)%s)\n",
           got, round, resumed ? ", resumed" : "");
    fflush(stdout);
    return 0;
}

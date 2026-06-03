/*
 * satpush_pull.c - cross-pass resumable file pull over libcsp / libdtp.
 *
 * Adapted from satdeploy-agent/src/dtp_client.c during Step 2 of the
 * libsatpush extract (/plan-eng-review 2026-05-21). The internal pull
 * function satpush_pull_file_impl is intentionally bit-identical to the
 * pre-extract dtp_download_file - the F3.b smart-vs-naive regression
 * baseline must not shift due to this move. The only structural changes
 * are:
 *   - state_dir flows in as a parameter (was implicit via SESSION_STATE_DIR)
 *   - resume_key replaces app_name (same role, library-neutral name)
 *   - progress callback hook added (NULL = no callback, same as before)
 *   - SHA256 verification of the assembled file moved inside the call so
 *     callers get SATPUSH_OK only on verified-correct bytes
 *
 * Public satpush_pull_file() is a thin wrapper that unpacks satpush_pull_opts
 * and forwards to the impl with the ctx-supplied state_dir and the per-call
 * throughput override.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <time.h>
#include <unistd.h>

#include <csp/csp.h>
#include <dtp/dtp.h>
#include <dtp/dtp_session.h>
#include <dtp/dtp_protocol.h>

#include "satpush/satpush.h"
#include "satpush_internal.h"
#include "session_state_internal.h"

/* DTP configuration defaults. Bit-identical to satdeploy-agent's pre-extract
 * defaults so the F3.b regression baseline is preserved when callers pass
 * zero/default values. */
#define SATPUSH_DEFAULT_TIMEOUT_S   60
#define SATPUSH_DEFAULT_MTU         1024
#define SATPUSH_DEFAULT_THROUGHPUT  10000000  /* bytes/s - must be non-zero */
#define SATPUSH_DEFAULT_MAX_ROUNDS  8

/* Per-transfer context handed to the libdtp callbacks. Stack-allocated by
 * the impl; lifetime is the duration of one satpush_pull_file_impl call. */
typedef struct {
    FILE *fp;
    uint32_t bytes_written;
    uint32_t expected_size;
    int error;
    uint8_t *recv_bitmap;
    uint32_t nof_packets;
    uint16_t effective_mtu;

    /* Progress reporting */
    satpush_progress_cb on_progress;
    void *progress_user_ctx;
    uint32_t progress_round;
} pull_ctx_t;

static inline void mark_seq_received(pull_ctx_t *ctx, uint32_t seq) {
    if (seq >= ctx->nof_packets) return;
    ctx->recv_bitmap[seq >> 3] |= (uint8_t)(1u << (seq & 7));
}

static inline int seq_is_received(const pull_ctx_t *ctx, uint32_t seq) {
    if (seq >= ctx->nof_packets) return 1;
    return (ctx->recv_bitmap[seq >> 3] >> (seq & 7)) & 1;
}

/* Fill `out` with up to 8 missing-seq ranges. Returns count. Each interval
 * is inclusive on both ends (matches DTP server semantics). */
static uint8_t compute_missing_intervals(const pull_ctx_t *ctx,
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

static uint32_t count_received_packets(const pull_ctx_t *ctx) {
    uint32_t n = 0;
    for (uint32_t i = 0; i < ctx->nof_packets; i++) {
        if (seq_is_received(ctx, i)) n++;
    }
    return n;
}

/* libdtp on_start hook */
static void on_pull_start(dtp_t *session) {
    pull_ctx_t *ctx = (pull_ctx_t *)dtp_session_get_user_ctx(session);
    if (ctx) {
        printf("[satpush] pulling %u bytes...\n", ctx->expected_size);
        fflush(stdout);
    }
}

/* libdtp on_data_packet hook. Writes data to file at offset, marks bitmap. */
static bool on_pull_data(dtp_t *session, csp_packet_t *packet) {
    pull_ctx_t *ctx = (pull_ctx_t *)dtp_session_get_user_ctx(session);
    if (ctx == NULL || ctx->fp == NULL) {
        return false;
    }

    if (packet == NULL || packet->length == 0) {
        return true;
    }

    /* Guard against integer underflow in dtp_get_data_info(): packet must
     * have at least 2 * uint32_t (8 bytes) for the header. */
    if (packet->length < 2 * sizeof(uint32_t)) {
        return true;
    }

    dtp_on_data_info_t info = dtp_get_data_info(session, packet);

    /* fseek + fwrite at info.data_offset makes re-receiving the same packet
     * a harmless overwrite, which is what we want when re-requesting missing
     * intervals. */
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

    if (ctx->on_progress) {
        bool keep_going = ctx->on_progress(ctx->progress_user_ctx,
                                           ctx->bytes_written,
                                           ctx->expected_size,
                                           ctx->progress_round);
        if (!keep_going) {
            ctx->error = -2;  /* cancellation sentinel */
            return false;
        }
    }
    return true;
}

static void on_pull_end(dtp_t *session) {
    (void)session;
}

static void on_pull_release(dtp_t *session) {
    (void)session;
}

/* Compute SHA256 of a file as 64-hex lowercase. Returns 0 on success. */
static int sha256_file_hex(const char *path, char out[65]) {
    /* Delegate to the public helper to keep one implementation. */
    return satpush_sha256_file(path, out) == SATPUSH_OK ? 0 : -1;
}

/*
 * Internal pull implementation. Bit-identical core to satdeploy-agent's
 * dtp_download_file. Returns:
 *    SATPUSH_OK            - fully received and SHA256 verified
 *    SATPUSH_PARTIAL       - incomplete coverage, state saved for resume
 *    SATPUSH_HASH_MISMATCH - all bytes received but SHA256 wrong
 *    SATPUSH_CANCELLED     - on_progress returned false
 *    SATPUSH_HARD_ERROR    - connection/IO failure
 *    SATPUSH_INVALID       - bad arguments
 */
static satpush_result satpush_pull_file_impl(const char *state_dir,
                                             uint32_t server_node,
                                             uint8_t payload_id,
                                             const char *dest_path,
                                             uint32_t expected_size,
                                             const char *expected_hash,
                                             const char *resume_key,
                                             uint16_t mtu,
                                             uint32_t throughput,
                                             uint8_t timeout,
                                             uint8_t max_retry_rounds,
                                             int single_pass,
                                             satpush_progress_cb on_progress,
                                             void *progress_user_ctx) {
    if (dest_path == NULL) {
        return SATPUSH_INVALID;
    }

    /* Apply defaults for zero values */
    if (mtu == 0)               mtu = SATPUSH_DEFAULT_MTU;
    if (throughput == 0)        throughput = SATPUSH_DEFAULT_THROUGHPUT;
    if (timeout == 0)           timeout = SATPUSH_DEFAULT_TIMEOUT_S;
    if (single_pass) {
        /* Naive baseline (F3.b): the first dtp_start_transfer is the only
         * attempt, no gap-filling retry rounds. Overrides max_retry_rounds.
         * Resume is disabled separately by the caller passing resume_key=NULL,
         * so a naive trial can neither load nor save a sidecar. */
        max_retry_rounds = 0;
    } else if (max_retry_rounds == 0) {
        max_retry_rounds = SATPUSH_DEFAULT_MAX_ROUNDS;
    }

    /* Effective payload per packet is (mtu - 8) - DTP prepends two 32-bit
     * words (sequence + offset) to the CSP packet payload. */
    uint16_t eff_mtu = (mtu > 8) ? (uint16_t)(mtu - 8) : 1;
    uint32_t nof_packets = expected_size ? ((expected_size + eff_mtu - 1) / eff_mtu) : 0;
    size_t bitmap_bytes = (nof_packets + 7) / 8;
    uint8_t *bitmap = NULL;
    if (bitmap_bytes > 0) {
        bitmap = calloc(1, bitmap_bytes);
        if (!bitmap) {
            printf("\033[31m[satpush] error: bitmap alloc failed (%zu bytes)\033[0m\n",
                   bitmap_bytes);
            return SATPUSH_HARD_ERROR;
        }
    }

    /* Resolve the cross-pass state sidecar path. We attempt resume only when
     * we have all of (resume_key, expected_hash, non-zero size). Strict-
     * equality load means a re-staged binary (different SHA256 for the same
     * resume_key) wipes stale state instead of inheriting a poisoned bitmap. */
    char state_path[640];
    bool have_state_path = (state_dir != NULL && resume_key != NULL &&
                            expected_hash != NULL && expected_size > 0 &&
                            bitmap_bytes > 0 &&
                            session_state_path(state_dir, resume_key,
                                               state_path, sizeof(state_path)) == 0);

    bool resumed = false;
    if (have_state_path) {
        if (session_state_load(state_path, expected_size, expected_hash,
                               nof_packets, eff_mtu,
                               bitmap, bitmap_bytes) == 1) {
            resumed = true;
        }
    }

    /* Open destination file. On resume we must NOT truncate - the previously
     * received packet payloads at their byte offsets are the whole reason we
     * persisted the bitmap. fseek+fwrite at the recorded offsets only works
     * if the underlying bytes survive across the open(). */
    FILE *fp = NULL;
    if (resumed) {
        fp = fopen(dest_path, "r+b");
        if (fp == NULL) {
            printf("[satpush] sidecar present but %s missing - starting fresh\n",
                   dest_path);
            session_state_unlink(state_path);
            memset(bitmap, 0, bitmap_bytes);
            resumed = false;
            fp = fopen(dest_path, "wb");
        }
    } else {
        fp = fopen(dest_path, "wb");
    }
    if (fp == NULL) {
        printf("\033[31m[satpush] error: failed to open %s\033[0m\n", dest_path);
        free(bitmap);
        return SATPUSH_HARD_ERROR;
    }

    pull_ctx_t ctx = {
        .fp = fp,
        .bytes_written = 0,
        .expected_size = expected_size,
        .error = 0,
        .recv_bitmap = bitmap,
        .nof_packets = nof_packets,
        .effective_mtu = eff_mtu,
        .on_progress = on_progress,
        .progress_user_ctx = progress_user_ctx,
        .progress_round = 0,
    };

    /* Deterministic session_id: first 4 bytes of SHA256(resume_key || ":" ||
     * expected_hash). Stable across processes and reboots, so puller and
     * server agree without a negotiation step. Falls back to a time-based
     * id when caller did not supply identification. */
    uint32_t session_id;
    if (resume_key && expected_hash) {
        session_id = session_state_compute_id(resume_key, expected_hash);
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
        NULL,
        mtu,
        false,             /* libdtp resume - we manage state ourselves */
        0                  /* keep_alive_interval */
    );

    if (session == NULL) {
        printf("\033[31m[satpush] error: failed to create session\033[0m\n");
        fclose(fp);
        free(ctx.recv_bitmap);
        return SATPUSH_HARD_ERROR;
    }

    dtp_session_set_user_ctx(session, &ctx);

    dtp_params hooks = {
        .hooks = {
            .on_start = on_pull_start,
            .on_data_packet = on_pull_data,
            .on_end = on_pull_end,
            .on_release = on_pull_release,
            .hook_ctx = &ctx
        }
    };
    dtp_set_opt(session, DTP_SESSION_HOOKS_CFG, &hooks);

    /* If we resumed and already have full coverage from disk, skip the
     * transfer entirely. Otherwise pre-patch request_meta so the first
     * dtp_start_transfer asks only for the gaps. */
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
            printf("[satpush] resume session=%08x: %u/%u packets from sidecar, requesting %u gap(s)\n",
                   session_id, got_initial, ctx.nof_packets, n);
            fflush(stdout);
        }
    }

    int dtp_rc = DTP_OK;
    int round = 0;
    int hard_error = 0;

    if (!already_complete) {
        dtp_rc = dtp_start_transfer(session);

        while (ctx.recv_bitmap && ctx.error == 0 &&
               round < max_retry_rounds) {
            if (dtp_rc != DTP_OK && dtp_rc != DTP_CANCELLED) {
                /* Connection-level failure. Retrying the same intervals on a
                 * transport that just rejected us is unlikely to help and
                 * risks an infinite loop, so bail. */
                hard_error = dtp_rc;
                break;
            }

            uint32_t got = count_received_packets(&ctx);
            if (got >= ctx.nof_packets) {
                break;
            }

            interval_t gaps[8];
            uint8_t n = compute_missing_intervals(&ctx, gaps);
            if (n == 0) {
                break;
            }

            printf("[satpush] round %d: %u/%u packets received, re-requesting %u gap(s)\n",
                   round + 1, got, ctx.nof_packets, n);
            for (uint8_t i = 0; i < n; i++) {
                printf("[satpush]   gap %u: seq [%u..%u]\n",
                       i, gaps[i].start, gaps[i].end);
            }
            fflush(stdout);

            for (uint8_t i = 0; i < n; i++) {
                session->request_meta.intervals[i] = gaps[i];
            }
            session->request_meta.nof_intervals = n;

            round++;
            ctx.progress_round = (uint32_t)round;
            dtp_rc = dtp_start_transfer(session);
        }
    }

    uint32_t got = ctx.recv_bitmap ? count_received_packets(&ctx) : 0;
    bool fully_received = (ctx.nof_packets == 0) || (got >= ctx.nof_packets);

    dtp_release_session(session);

    /* fflush + fsync before persisting the bitmap so the on-disk file
     * actually contains the bytes the bitmap claims. */
    if (fp != NULL) {
        fflush(fp);
        int fd = fileno(fp);
        if (fd >= 0) {
            (void)fsync(fd);
        }
        fclose(fp);
    }

    /* Sidecar lifecycle:
     *   - write error (ctx.error > 0) -> corrupt on-disk file, do nothing
     *   - cancellation (ctx.error == -2) -> save state so resume picks up
     *   - full success -> unlink (next pull starts clean)
     *   - partial coverage (with or without hard_error) -> save the bitmap
     */
    bool was_cancelled = (ctx.error == -2);
    bool had_write_error = (ctx.error != 0 && ctx.error != -2);

    if (have_state_path && !had_write_error) {
        if (fully_received) {
            session_state_unlink(state_path);
        } else if (ctx.recv_bitmap && got > 0) {
            int rc = session_state_save(state_path, expected_size, expected_hash,
                                        ctx.nof_packets, ctx.effective_mtu,
                                        ctx.recv_bitmap, bitmap_bytes);
            if (rc == 0) {
                printf("[satpush] persisted %u/%u packets to %s for next pass\n",
                       got, ctx.nof_packets, state_path);
                fflush(stdout);
            }
        }
    }

    free(ctx.recv_bitmap);

    if (was_cancelled) {
        return SATPUSH_CANCELLED;
    }
    if (had_write_error) {
        printf("\033[31m[satpush] error: write failed\033[0m\n");
        return SATPUSH_HARD_ERROR;
    }
    if (hard_error) {
        printf("\033[31m[satpush] error: pull failed (dtp status=%d)\033[0m\n",
               hard_error);
        return SATPUSH_HARD_ERROR;
    }
    if (!fully_received) {
        printf("\033[31m[satpush] error: incomplete after %d retry round(s) "
               "(%u/%u packets) - state saved for resume\033[0m\n",
               round, got, ctx.nof_packets);
        return SATPUSH_PARTIAL;
    }

    /* Bytes are all here. Now verify SHA256 against expected_hash before
     * declaring success - this is the load-bearing application-layer
     * guarantee. Either we return SATPUSH_OK and the dest_file is good,
     * or we return SATPUSH_HASH_MISMATCH and the caller knows not to
     * install. */
    if (expected_hash != NULL) {
        char actual[65];
        if (sha256_file_hex(dest_path, actual) != 0) {
            printf("\033[31m[satpush] error: failed to re-read %s for verify\033[0m\n",
                   dest_path);
            return SATPUSH_HARD_ERROR;
        }
        if (strcmp(actual, expected_hash) != 0) {
            printf("\033[31m[satpush] hash mismatch: got %.8s, expected %.8s\033[0m\n",
                   actual, expected_hash);
            return SATPUSH_HASH_MISMATCH;
        }
    }

    printf("[satpush] complete (%u packets, %d retry round(s)%s)\n",
           got, round, resumed ? ", resumed" : "");
    fflush(stdout);
    return SATPUSH_OK;
}

/*
 * Public pull entry point. Unpacks opts, applies ctx defaults, forwards to
 * the impl above.
 */
satpush_result satpush_pull_file(satpush_ctx_t *ctx,
                                 const satpush_pull_opts *opts) {
    if (ctx == NULL || opts == NULL) {
        return SATPUSH_INVALID;
    }
    if (opts->dest_file == NULL || opts->expected_sha256_hex == NULL) {
        return SATPUSH_INVALID;
    }

    uint32_t throughput = opts->throughput_bps != 0
        ? opts->throughput_bps
        : ctx->throughput_bps;

    return satpush_pull_file_impl(
        ctx->state_dir,
        opts->server_node,
        opts->payload_id,
        opts->dest_file,
        opts->expected_size,
        opts->expected_sha256_hex,
        opts->resume_key,
        opts->mtu,
        throughput,
        opts->timeout_s,
        opts->max_retry_rounds,
        opts->single_pass,
        opts->on_progress,
        opts->user_ctx);
}

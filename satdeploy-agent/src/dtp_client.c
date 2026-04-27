/**
 * DTP client wrapper - downloads files via DTP protocol with cross-pass resume.
 *
 * Flow per call:
 *   1. Compute deterministic session_id from (app_name, expected_checksum) so
 *      ground and agent agree without negotiation.
 *   2. Open temp file "r+b" if a sidecar state exists, else "wb".
 *   3. dtp_prepare_session(resume=false) sets fresh defaults; we drive resume
 *      manually via dtp_deserialize_session() after hooks are wired (libdtp's
 *      built-in resume path inside prepare runs before hooks can be set).
 *   4. dtp_set_opt(DTP_SESSION_HOOKS_CFG) attaches our serialize/deserialize
 *      callbacks; on_deserialize validates the on-disk hash matches what we
 *      expect, refusing resume if a ground rebuild changed the bytes.
 *   5. dtp_start_transfer drives the actual receive loop. on_data_packet
 *      writes packets at info.data_offset directly into the temp file.
 *   6. On DTP_OK: full transfer, unlink the sidecar (no resume needed).
 *      On DTP_CANCELLED with bytes < payload: pass ended mid-flight,
 *      dtp_serialize_session() persists state for the next pass.
 *
 * On-disk artifacts:
 *   <dest_path>.tmp                     partial bytes
 *   /var/lib/satdeploy/state/<app>.dtpstate   session state (intervals, hash)
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
#include <unistd.h>
#include <sys/stat.h>

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

/* Context for file download */
typedef struct {
    FILE *fp;
    uint32_t bytes_written;     /* bytes this process wrote, not total */
    uint32_t expected_size;
    int error;
} download_ctx_t;

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
 * Writes data to file at the offset libdtp reports, so out-of-order arrivals
 * and resume-from-mid-stream both land in the right bytes.
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

    /* Seek to correct position and write */
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
    return true;  /* Continue transfer */
}

static void on_download_end(dtp_t *session) {
    (void)session;
}

static void on_download_release(dtp_t *session) {
    (void)session;
}

int dtp_download_file(uint32_t server_node, uint8_t payload_id,
                      const char *dest_path, uint32_t expected_size,
                      const char *app_name, const char *expected_checksum,
                      uint16_t mtu, uint32_t throughput, uint8_t timeout) {
    if (dest_path == NULL || app_name == NULL || expected_checksum == NULL) {
        return -1;
    }

    /* Apply defaults for zero values */
    if (mtu == 0)        mtu = DTP_DEFAULT_MTU;
    if (throughput == 0) throughput = DTP_DEFAULT_THROUGHPUT;
    if (timeout == 0)    timeout = DTP_DEFAULT_TIMEOUT_S;

    /* Resolve sidecar state path. Failure here is non-fatal: we just lose
     * resume capability for this app and degrade to fresh-every-time. */
    session_state_ctx_t state_ctx;
    memset(&state_ctx, 0, sizeof(state_ctx));
    int has_state_path = (session_state_path(app_name, state_ctx.path,
                                             sizeof(state_ctx.path)) == 0);
    if (has_state_path) {
        strncpy(state_ctx.expected_hash, expected_checksum,
                sizeof(state_ctx.expected_hash) - 1);
        (void)session_state_dir_ensure();  /* best-effort */
    }
    int state_file_present = has_state_path && session_state_exists(state_ctx.path);

    /* Open destination file. "r+b" preserves any partial bytes from a prior
     * pass; "wb" truncates. We can only safely open r+b if BOTH the temp file
     * and a state file exist — otherwise we have bytes with no idea where the
     * gaps are. */
    int temp_file_present = 0;
    {
        struct stat st;
        if (stat(dest_path, &st) == 0 && S_ISREG(st.st_mode)) {
            temp_file_present = 1;
        }
    }
    const char *open_mode = (state_file_present && temp_file_present) ? "r+b" : "wb";
    FILE *fp = fopen(dest_path, open_mode);
    if (fp == NULL) {
        printf("\033[31m[dtp]    error: failed to open %s (mode=%s)\033[0m\n",
               dest_path, open_mode);
        return -1;
    }

    /* Setup download context */
    download_ctx_t ctx = {
        .fp = fp,
        .bytes_written = 0,
        .expected_size = expected_size,
        .error = 0
    };

    /* Deterministic session_id: same content always resumes on the same id,
     * so ground and agent don't need to renegotiate after a reboot. */
    uint32_t session_id = session_state_compute_id(app_name, expected_checksum);

    /* Use lower-level API: prepare(resume=false) sets fresh defaults; we then
     * set hooks and explicitly call dtp_deserialize_session() to overwrite
     * with on-disk state. This ordering is required because hooks must be
     * attached BEFORE deserialize runs. */
    dtp_t *session = dtp_prepare_session(
        server_node,
        session_id,
        throughput,
        timeout,
        payload_id,
        NULL,              /* user ctx — set below */
        mtu,
        false,             /* resume — driven manually below */
        0                  /* keep_alive_interval */
    );

    if (session == NULL) {
        printf("\033[31m[dtp]    error: failed to create session\033[0m\n");
        fclose(fp);
        return -1;
    }

    /* Set user context and hooks BEFORE start (and before manual deserialize). */
    dtp_session_set_user_ctx(session, &ctx);

    dtp_params hooks = {
        .hooks = {
            .on_start = on_download_start,
            .on_data_packet = on_download_data,
            .on_end = on_download_end,
            .on_release = on_download_release,
            .on_serialize = session_state_on_serialize,
            .on_deserialize = session_state_on_deserialize,
            .hook_ctx = &state_ctx
        }
    };
    dtp_set_opt(session, DTP_SESSION_HOOKS_CFG, &hooks);

    /* If a sidecar state file exists, restore session state from it. The
     * deserialize hook validates format/version/hash and unlinks the file on
     * any mismatch (caller's "r+b" then writes from byte 0 normally). */
    if (state_file_present) {
        dtp_deserialize_session(session, &state_ctx);
        if (state_ctx.resumed) {
            printf("[dtp]    resuming %s from %u bytes\n", app_name,
                   session->bytes_received);
        }
    }

    /* Now start the transfer — hooks are active, state is restored if any. */
    int result = dtp_start_transfer(session);

    /* On a partial/cancelled transfer where we have not yet received the full
     * payload, persist state so the next pass can resume. The serialize hook
     * is called via libdtp; we just trigger it. */
    bool partial = (result == DTP_CANCELLED &&
                    expected_size > 0 &&
                    session->bytes_received < session->payload_size);
    if (partial && has_state_path) {
        dtp_serialize_session(session, &state_ctx);
    }

    dtp_release_session(session);
    fclose(fp);

    if (ctx.error != 0) {
        printf("\033[31m[dtp]    error: write failed\033[0m\n");
        return -1;
    }
    /* Accept DTP_CANCELLED when all bytes were written — the DTP library
       may report cancelled if bytes_received tracking diverges from
       payload_size, even when the actual file data was fully written. */
    if (result == DTP_CANCELLED && expected_size > 0 && ctx.bytes_written == expected_size) {
        /* full set written this pass — proceed to checksum */
    } else if (result != DTP_OK) {
        printf("\033[31m[dtp]    error: incomplete (%u/%u bytes, status=%d)\033[0m\n",
               ctx.bytes_written, expected_size, result);
        return -1;
    }

    /* Verify size if expected */
    if (expected_size > 0 && ctx.bytes_written != expected_size) {
        printf("\033[33m[dtp]    warning: expected %u bytes, got %u\033[0m\n",
               expected_size, ctx.bytes_written);
        return -1;
    }

    /* Full transfer complete — drop the resume sidecar so next deploy of a
     * different binary doesn't try to fast-forward against stale state. */
    if (has_state_path) {
        session_state_unlink(state_ctx.path);
    }

    printf("[dtp]    complete (%u bytes)\n", ctx.bytes_written);
    fflush(stdout);
    return 0;
}

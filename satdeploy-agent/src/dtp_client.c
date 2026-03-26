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
#include <time.h>

#include <csp/csp.h>
#include <dtp/dtp.h>

#include "satdeploy_agent.h"

/* DTP configuration defaults */
#define DTP_DEFAULT_TIMEOUT_S    60
#define DTP_DEFAULT_MTU          1024
#define DTP_DEFAULT_THROUGHPUT   10000000  /* bytes/s — must be non-zero to avoid div-by-zero in compute_dtp_metrics */

/* Context for file download */
typedef struct {
    FILE *fp;
    uint32_t bytes_written;
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
                      uint16_t mtu, uint32_t throughput, uint8_t timeout) {
    if (dest_path == NULL) {
        return -1;
    }

    /* Apply defaults for zero values */
    if (mtu == 0)        mtu = DTP_DEFAULT_MTU;
    if (throughput == 0) throughput = DTP_DEFAULT_THROUGHPUT;
    if (timeout == 0)    timeout = DTP_DEFAULT_TIMEOUT_S;

    /* Open destination file */
    FILE *fp = fopen(dest_path, "wb");
    if (fp == NULL) {
        printf("\033[31m[dtp]    error: failed to open %s\033[0m\n", dest_path);
        return -1;
    }

    /* Setup download context */
    download_ctx_t ctx = {
        .fp = fp,
        .bytes_written = 0,
        .expected_size = expected_size,
        .error = 0
    };

    /* Use lower-level API: prepare session, set hooks, then start transfer.
       This ensures hooks are active during the transfer (unlike dtp_client_main
       which calls dtp_start_transfer before hooks can be set). */
    /* Generate unique session_id from time + counter to avoid stale packet confusion */
    static uint32_t session_counter = 0;
    uint32_t session_id = (uint32_t)time(NULL) ^ (++session_counter << 16);

    dtp_t *session = dtp_prepare_session(
        server_node,
        session_id,
        throughput,
        timeout,
        payload_id,
        NULL,              /* ctx - set below */
        mtu,
        false,             /* resume */
        0                  /* keep_alive_interval */
    );

    if (session == NULL) {
        printf("\033[31m[dtp]    error: failed to create session\033[0m\n");
        fclose(fp);
        return -1;
    }

    /* Debug: verify params passed to session */
    {
        dtp_params check;
        dtp_get_opt(session, DTP_PAYLOAD_ID_CFG, &check);
        printf("[dtp-debug] payload_id=%u (expected %u)\n", check.payload_id.value, payload_id);
        dtp_get_opt(session, DTP_MTU_CFG, &check);
        printf("[dtp-debug] mtu=%u\n", check.mtu.value);
        dtp_get_opt(session, DTP_THROUGHPUT_CFG, &check);
        printf("[dtp-debug] throughput=%u\n", check.throughput.value);
        fflush(stdout);
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

    /* Now start the transfer — hooks are active */
    int result = dtp_start_transfer(session);

    /* Cleanup */
    dtp_release_session(session);
    fclose(fp);

    if (ctx.error != 0) {
        printf("\033[31m[dtp]    error: write failed\033[0m\n");
        return -1;
    }
    /* Accept DTP_CANCELLED when all bytes were written — the DTP library
       may report cancelled if bytes_received tracking diverges from
       payload_size, even when the actual file data was fully written. */
    if (result != DTP_OK && result != DTP_CANCELLED) {
        printf("\033[31m[dtp]    error: download failed (status=%d)\033[0m\n", result);
        return -1;
    }
    if (result == DTP_CANCELLED && expected_size > 0 && ctx.bytes_written == expected_size) {
        /* transfer cancelled but byte count matches — proceed to checksum */
        (void)0;
    } else if (result != DTP_OK) {
        printf("\033[31m[dtp]    error: incomplete (%u/%u bytes)\033[0m\n",
               ctx.bytes_written, expected_size);
        return -1;
    }

    /* Verify size if expected */
    if (expected_size > 0 && ctx.bytes_written != expected_size) {
        printf("\033[33m[dtp]    warning: expected %u bytes, got %u\033[0m\n",
               expected_size, ctx.bytes_written);
        return -1;
    }

    printf("[dtp]    complete (%u bytes)\n", ctx.bytes_written);
    fflush(stdout);
    return 0;
}

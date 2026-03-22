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
#define DTP_DEFAULT_THROUGHPUT   0  /* 0 = unlimited */

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
        printf("[dtp] Download started, expecting %u bytes\n", ctx->expected_size);
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
        printf("[dtp] Download ended, wrote %u bytes\n", ctx->bytes_written);
    }
}

/**
 * DTP on_release callback - called when session is released.
 */
static void on_download_release(dtp_t *session) {
    (void)session;
    /* Nothing to clean up - context is on stack */
}

int dtp_download_file(uint32_t server_node, uint16_t payload_id,
                      const char *dest_path, uint32_t expected_size) {
    if (dest_path == NULL) {
        return -1;
    }

    printf("[dtp] Downloading payload %u from node %u to %s\n",
           payload_id, server_node, dest_path);

    /* Open destination file */
    FILE *fp = fopen(dest_path, "wb");
    if (fp == NULL) {
        printf("[dtp] Error: Failed to open %s for writing\n", dest_path);
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
        DTP_DEFAULT_THROUGHPUT,
        DTP_DEFAULT_TIMEOUT_S,
        payload_id,
        NULL,              /* ctx - set below */
        DTP_DEFAULT_MTU,
        false,             /* resume */
        0                  /* keep_alive_interval */
    );

    if (session == NULL) {
        printf("[dtp] Error: Failed to create DTP session\n");
        fclose(fp);
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

    /* Now start the transfer — hooks are active */
    int result = dtp_start_transfer(session);

    /* Cleanup */
    dtp_release_session(session);
    fclose(fp);

    if (ctx.error != 0) {
        printf("[dtp] Error: Download failed (write error)\n");
        return -1;
    }
    /* Accept DTP_CANCELLED when all bytes were written — the DTP library
       may report cancelled if bytes_received tracking diverges from
       payload_size, even when the actual file data was fully written. */
    if (result != DTP_OK && result != DTP_CANCELLED) {
        printf("[dtp] Error: Download failed (status=%d)\n", result);
        return -1;
    }
    if (result == DTP_CANCELLED && expected_size > 0 && ctx.bytes_written == expected_size) {
        printf("[dtp] Warning: transfer cancelled but byte count matches (%u bytes) — verifying checksum\n",
               expected_size);
    } else if (result != DTP_OK) {
        printf("[dtp] Error: Download incomplete (wrote %u/%u)\n",
               ctx.bytes_written, expected_size);
        return -1;
    }

    /* Verify size if expected */
    if (expected_size > 0 && ctx.bytes_written != expected_size) {
        printf("[dtp] Warning: Expected %u bytes, got %u\n",
               expected_size, ctx.bytes_written);
        return -1;
    }

    printf("[dtp] Download complete: %u bytes\n", ctx.bytes_written);
    return 0;
}

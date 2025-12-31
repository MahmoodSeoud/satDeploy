/**
 * DTP client wrapper - Downloads files via DTP protocol
 *
 * Wraps the DTP client API to provide a simple interface for
 * downloading files from a DTP server.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

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

    /* Use simplified client API to create and configure session */
    dtp_t *session = NULL;
    int result = dtp_client_main(
        server_node,
        DTP_DEFAULT_THROUGHPUT,
        DTP_DEFAULT_TIMEOUT_S,
        payload_id,
        DTP_DEFAULT_MTU,
        false,  /* Don't resume */
        &session
    );

    if (result != DTP_OK || session == NULL) {
        printf("[dtp] Error: Failed to create DTP session\n");
        fclose(fp);
        return -1;
    }

    /* Set user context */
    dtp_session_set_user_ctx(session, &ctx);

    /* Configure session hooks */
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

    /* Start the transfer - this blocks until complete or timeout */
    result = dtp_start_transfer(session);

    /* Cleanup */
    dtp_release_session(session);
    fclose(fp);

    if (result != DTP_OK || ctx.error != 0) {
        printf("[dtp] Error: Download failed\n");
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

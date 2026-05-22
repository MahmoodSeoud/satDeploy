/*
 * satpush_ctx.c - context lifecycle, error strings, public helpers.
 *
 * Implements satpush_create / satpush_destroy and the small helper functions
 * that callers reach for around a transfer: SHA256 of a file, sidecar unlink,
 * result-code stringification.
 *
 * Created during Step 2 of the libsatpush extract (/plan-eng-review
 * 2026-05-21). The internal satpush_ctx struct is defined in this file and
 * exposed to other libsatpush sources via satpush_internal.h.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>

#include "satpush/satpush.h"
#include "satpush_internal.h"
#include "session_state_internal.h"
#include "sha256.h"

satpush_ctx_t* satpush_create(uint16_t local_node,
                              const char* state_dir,
                              uint32_t throughput_bps) {
    if (state_dir == NULL || *state_dir == '\0') {
        return NULL;
    }

    satpush_ctx_t* ctx = (satpush_ctx_t*)calloc(1, sizeof(*ctx));
    if (ctx == NULL) {
        return NULL;
    }

    ctx->local_node = local_node;
    ctx->throughput_bps = throughput_bps;

    size_t n = strlen(state_dir);
    if (n >= sizeof(ctx->state_dir)) {
        free(ctx);
        return NULL;
    }
    memcpy(ctx->state_dir, state_dir, n + 1);

    if (session_state_dir_ensure(ctx->state_dir) != 0) {
        printf("[libsatpush] warning: could not ensure state_dir=%s: %s\n",
               ctx->state_dir, strerror(errno));
        /* Non-fatal: pulls without resume still work, and the directory may
         * become writable later. Operators see the warning and act. */
    }

    return ctx;
}

void satpush_destroy(satpush_ctx_t* ctx) {
    if (ctx == NULL) {
        return;
    }
    free(ctx);
}

const char* satpush_strerror(satpush_result r) {
    switch (r) {
        case SATPUSH_OK:            return "ok";
        case SATPUSH_PARTIAL:       return "partial transfer, state saved for resume";
        case SATPUSH_HASH_MISMATCH: return "sha256 mismatch";
        case SATPUSH_CANCELLED:     return "cancelled";
        case SATPUSH_HARD_ERROR:    return "hard error (see errno or printed log)";
        case SATPUSH_INVALID:       return "invalid argument";
    }
    return "unknown";
}

void satpush_resume_unlink(satpush_ctx_t* ctx, const char* resume_key) {
    if (ctx == NULL || resume_key == NULL) {
        return;
    }
    char path[640];
    if (session_state_path(ctx->state_dir, resume_key, path, sizeof(path)) != 0) {
        return;
    }
    session_state_unlink(path);
}

satpush_result satpush_sha256_file(const char* path, char out[65]) {
    if (path == NULL || out == NULL) {
        return SATPUSH_INVALID;
    }

    FILE* f = fopen(path, "rb");
    if (f == NULL) {
        return SATPUSH_HARD_ERROR;
    }

    sha256_ctx hctx;
    sha256_init(&hctx);

    uint8_t buf[8192];
    size_t got;
    while ((got = fread(buf, 1, sizeof(buf), f)) > 0) {
        sha256_update(&hctx, buf, got);
    }

    int io_err = ferror(f);
    fclose(f);
    if (io_err) {
        return SATPUSH_HARD_ERROR;
    }

    uint8_t digest[SHA256_DIGEST_SIZE];
    sha256_final(&hctx, digest);

    static const char hex[] = "0123456789abcdef";
    for (int i = 0; i < SHA256_DIGEST_SIZE; ++i) {
        out[i * 2]     = hex[(digest[i] >> 4) & 0x0f];
        out[i * 2 + 1] = hex[digest[i] & 0x0f];
    }
    out[64] = '\0';

    return SATPUSH_OK;
}

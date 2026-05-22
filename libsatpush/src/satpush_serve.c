/*
 * satpush_serve.c - server-side payload registration over libdtp.
 *
 * Extracted from satdeploy-apm/src/satdeploy_apm.c during Step 2 of the
 * libsatpush extract (/plan-eng-review 2026-05-21). The pre-extract APM
 * called dtp_file_payload_add / dtp_file_payload_del directly; this module
 * wraps those calls with the stdout-suppression trick the APM used to hide
 * libdtp's spurious "ERROR: Payload id: X does not exist" prints during
 * the del-then-add cycle.
 *
 * The server itself (the listener that responds to incoming pull requests)
 * is libdtp's dtp_server_main(). libsatpush does not start that task; the
 * caller is expected to have a running DTP server context. satdeploy-apm
 * does this via dtp_server_session_start(); standalone adopters can do the
 * same after csp_init.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/stat.h>

#include <dtp/dtp.h>
#include <dtp/dtp_file_payload.h>

#include "satpush/satpush.h"

/* Serve operations do not read any ctx fields - the ctx parameter is kept
 * for API symmetry with satpush_pull_file but accepts NULL when callers
 * only use server-side registration. This keeps satpush_serve.c
 * compilable into shared libraries that do not want the full libsatpush
 * dependency surface (notably satdeploy-apm, which uses a different
 * SHA256 implementation and therefore cannot link the libsatpush ctx.c
 * helpers cleanly). */

/* Swap stdout to /dev/null for the duration of a single libdtp call. libdtp's
 * dtp_file_payload_del prints to stdout when the slot is empty (the common
 * case on a fresh register), which looks scary but is expected. Hiding it
 * keeps operator output clean.
 *
 * Returns the saved stdout fd (caller restores via restore_stdout) or -1 if
 * the suppression could not be installed (caller just calls libdtp normally
 * in that case). */
static int suppress_stdout(void) {
    int devnull = open("/dev/null", O_WRONLY);
    if (devnull < 0) {
        return -1;
    }
    int saved = dup(STDOUT_FILENO);
    if (saved < 0) {
        close(devnull);
        return -1;
    }
    dup2(devnull, STDOUT_FILENO);
    close(devnull);
    return saved;
}

static void restore_stdout(int saved_fd) {
    if (saved_fd < 0) {
        return;
    }
    fflush(stdout);
    dup2(saved_fd, STDOUT_FILENO);
    close(saved_fd);
}

satpush_result satpush_serve_register(satpush_ctx_t *ctx,
                                      const satpush_serve_opts *opts) {
    (void)ctx;  /* Allowed to be NULL for serve-only callers. */
    if (opts == NULL || opts->local_file == NULL) {
        return SATPUSH_INVALID;
    }

    /* If the caller passed file_size, sanity-check it against the actual
     * file on disk and warn on mismatch. libdtp stats the file itself, so
     * the file_size in opts is informational only. */
    if (opts->file_size > 0) {
        struct stat st;
        if (stat(opts->local_file, &st) == 0 &&
            (uint32_t)st.st_size != opts->file_size) {
            printf("[satpush] warning: serve_register file_size mismatch "
                   "(caller=%u, on-disk=%lld) for %s\n",
                   opts->file_size, (long long)st.st_size, opts->local_file);
        }
    }

    /* Del-then-add ensures the slot reflects the latest filename for this
     * payload_id. A re-staged binary at the same payload_id correctly
     * overwrites stale registry contents on the same slot. */
    int saved = suppress_stdout();
    dtp_file_payload_del(opts->payload_id);
    restore_stdout(saved);

    if (!dtp_file_payload_add(opts->payload_id, opts->local_file)) {
        printf("\033[31m[satpush] error: serve_register failed for "
               "payload_id=%u file=%s\033[0m\n",
               opts->payload_id, opts->local_file);
        return SATPUSH_HARD_ERROR;
    }

    return SATPUSH_OK;
}

satpush_result satpush_serve_unregister(satpush_ctx_t *ctx, uint8_t payload_id) {
    (void)ctx;  /* Allowed to be NULL for serve-only callers. */

    int saved = suppress_stdout();
    dtp_file_payload_del(payload_id);
    restore_stdout(saved);

    return SATPUSH_OK;
}

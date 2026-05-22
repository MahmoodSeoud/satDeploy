/*
 * satpush_internal.h - private types shared across libsatpush sources.
 *
 * Not part of the public API. Defines the concrete shape of the opaque
 * satpush_ctx_t exposed by <satpush/satpush.h>.
 */

#ifndef SATPUSH_INTERNAL_H
#define SATPUSH_INTERNAL_H

#include <stdint.h>

/* Path buffer matching satdeploy-agent's existing 640-byte state_path bound
 * in dtp_client.c (covers ~/.satpush/state plus a 64-char hash + suffix). */
#define SATPUSH_STATE_DIR_MAX 512

/**
 * Concrete shape of the public opaque satpush_ctx_t. Sources that need to
 * read ctx fields (state_dir, throughput_bps, local_node) include this
 * header. Adopters never see it.
 */
struct satpush_ctx {
    uint16_t local_node;
    uint32_t throughput_bps;
    char     state_dir[SATPUSH_STATE_DIR_MAX];
};

#endif /* SATPUSH_INTERNAL_H */

/*
 * svu_transfer.c - satdeploy's transfer layer, on SVU.
 *
 * This replaces dtp_client.c's role. The deployment shell above it (backup,
 * install, rollback, version history) is untouched: deploy_handler calls one
 * function, gets a file on disk or an error, and proceeds exactly as before.
 *
 * Why the swap. libdtp decides completion from the receiver's packet counter,
 * which duplicates advance, and its resume path re-requests only a prefix of
 * what it is missing before reporting success. satdeploy v1 compensated with a
 * whole-artifact SHA-256 and a retry loop: correct, but the unit of recovery
 * was the artifact, so a single missing fragment cost a re-verification of
 * everything and the reverse channel scaled with retransmission.
 *
 * SVU moves the completion decision to the receiver and makes the unit of
 * recovery a block. The manifest is a per-block SHA-256 exchanged over the
 * reliable control channel; the body rides connectionless. The receiver
 * declares completion only once every block verifies, so no sender-side byte
 * count can stand in for delivery, and a recovery round re-requests only the
 * byte ranges that failed -- one reverse packet per round.
 *
 * The whole-artifact digest is NOT dropped. SVU guarantees every block matches
 * the manifest; the caller still gates the install on the artifact digest
 * recorded on the ground, which is what catches a manifest for the wrong file.
 * The two checks answer different questions and both are cheap.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/stat.h>

#include <csp/csp.h>

#include "satdeploy_agent.h"
#include "session_state.h"
#include "svu_session.h"

/* Manifest granularity. 4096 keeps the manifest small (a 256 KiB artifact is 64
 * blocks = 2 KiB of SHA-256) while making recovery fine enough that one lost
 * fragment does not re-send a megabyte. Matches the block size swept in the
 * thesis evaluation, so agent cells are comparable with the host-arm cells. */
#define SVU_BLOCK_SIZE 4096u

/*
 * Recovery budget. The flight build allows enough rounds to converge at the
 * heaviest loss level swept (12 rounds at 30 % on the matched host arm), with
 * headroom.
 *
 * SATDEPLOY_NAIVE_BASELINE is the thesis control build: the same source with
 * recovery compiled out. svu_client_run's loop runs `while (round <
 * max_rounds)` and spends round 1 on the initial whole-file request, so a
 * budget of 1 is exactly one blast and no re-request. That is the SVU
 * equivalent of v1's DTP_MAX_RETRY_ROUNDS = 0, and it preserves the ablation:
 * identical source, identical verification, recovery the only difference.
 */
#ifdef SATDEPLOY_NAIVE_BASELINE
#define SVU_MAX_ROUNDS 1u
#else
#define SVU_MAX_ROUNDS 24u
#endif

/*
 * Cross-pass sidecar: <state dir>/<app>.svupart, a 16-byte header plus the
 * raw partial reassembly buffer, pwritten at fragment offsets as data
 * arrives. A SIGKILL at any moment leaves everything received so far on
 * disk; the next pass restores it, fetches a fresh manifest (one empty
 * interval -- nothing blasted), and re-requests only the blocks that fail
 * verification.
 *
 * Deliberately NOT persisted: the manifest and any verified-block bitmap.
 * Data is the only state worth keeping -- verification is recomputed against
 * the next pass's manifest, so a ground-side rebuild between passes
 * self-heals instead of resuming into a stale artifact. This is what the
 * DTP bitmap sidecar could not do: its packet bitmap was only meaningful
 * for one exact (size, mtu, hash) shape and carried no content check.
 */
#define SVU_SIDECAR_MAGIC   0x53565550u /* "SVUP" */
#define SVU_SIDECAR_VERSION 1u
#define SVU_SIDECAR_HDR     16u

typedef struct {
    int fd; /* -1 = persistence off (bad app name, unwritable state dir) */
    uint32_t total;
    char path[640];
} svu_sidecar_t;

#ifndef SATDEPLOY_NAIVE_BASELINE
static void sidecar_put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24); p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);  p[3] = (uint8_t)v;
}

static uint32_t sidecar_get32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

static int sidecar_write_header(int fd, uint32_t total, uint32_t block)
{
    uint8_t hdr[SVU_SIDECAR_HDR];
    sidecar_put32(hdr + 0, SVU_SIDECAR_MAGIC);
    sidecar_put32(hdr + 4, SVU_SIDECAR_VERSION);
    sidecar_put32(hdr + 8, total);
    sidecar_put32(hdr + 12, block);
    return (pwrite(fd, hdr, sizeof(hdr), 0) == (ssize_t)sizeof(hdr)) ? 0 : -1;
}

static uint8_t *sidecar_restore(void *ctx, uint32_t *total_out)
{
    svu_sidecar_t *sc = (svu_sidecar_t *)ctx;
    if (sc->path[0] == '\0') {
        return NULL;
    }
    int fd = open(sc->path, O_RDWR);
    if (fd < 0) {
        return NULL; /* no prior pass */
    }
    uint8_t hdr[SVU_SIDECAR_HDR];
    if (pread(fd, hdr, sizeof(hdr), 0) != (ssize_t)sizeof(hdr) ||
        sidecar_get32(hdr + 0) != SVU_SIDECAR_MAGIC ||
        sidecar_get32(hdr + 4) != SVU_SIDECAR_VERSION ||
        sidecar_get32(hdr + 8) == 0u) {
        csp_print("svu_transfer: sidecar %s unreadable, starting fresh\n",
                  sc->path);
        close(fd);
        unlink(sc->path);
        return NULL;
    }
    uint32_t total = sidecar_get32(hdr + 8);
    uint8_t *buf = calloc(1u, total);
    if (buf == NULL) {
        close(fd);
        return NULL;
    }
    /* The file may be shorter than header+total (tail never arrived, sparse
     * writes): whatever is missing stays zero and fails its block hash. */
    ssize_t got = pread(fd, buf, total, SVU_SIDECAR_HDR);
    if (got < 0) {
        free(buf);
        close(fd);
        unlink(sc->path);
        return NULL;
    }
    sc->fd = fd; /* keep writing into the same sidecar this pass */
    sc->total = total;
    *total_out = total;
    return buf;
}

static void sidecar_meta(void *ctx, uint32_t total, uint32_t block)
{
    svu_sidecar_t *sc = (svu_sidecar_t *)ctx;
    if (sc->fd < 0) {
        if (sc->path[0] == '\0') {
            return;
        }
        sc->fd = open(sc->path, O_RDWR | O_CREAT | O_TRUNC, 0600);
        if (sc->fd < 0) {
            csp_print("svu_transfer: cannot create sidecar %s\n", sc->path);
            return;
        }
    } else if (sc->total != total) {
        /* Restored a stale artifact's data (the transfer loop already
         * discarded it); reset the file to this transfer's shape. */
        if (ftruncate(sc->fd, 0) != 0) { /* best effort */ }
    }
    sc->total = total;
    if (sidecar_write_header(sc->fd, total, block) != 0) {
        close(sc->fd);
        sc->fd = -1;
    }
}

static void sidecar_data(void *ctx, uint32_t offset, const uint8_t *buf,
                         uint32_t len)
{
    svu_sidecar_t *sc = (svu_sidecar_t *)ctx;
    if (sc->fd < 0 || offset > sc->total || len > sc->total - offset) {
        return;
    }
    if (pwrite(sc->fd, buf, len, (off_t)SVU_SIDECAR_HDR + offset) !=
        (ssize_t)len) {
        /* A failing disk should not fail the in-memory transfer; just stop
         * persisting so a later pass starts fresh instead of resuming from
         * a torn file. */
        close(sc->fd);
        sc->fd = -1;
        unlink(sc->path);
    }
}

static void sidecar_complete(void *ctx)
{
    svu_sidecar_t *sc = (svu_sidecar_t *)ctx;
    if (sc->fd >= 0) {
        close(sc->fd);
        sc->fd = -1;
    }
    if (sc->path[0] != '\0') {
        unlink(sc->path);
    }
}
#endif /* !SATDEPLOY_NAIVE_BASELINE */

int svu_download_file(uint32_t server_node, const char *dest_path,
                      uint32_t expected_size, const char *app_name,
                      uint16_t mtu)
{
    if (dest_path == NULL || dest_path[0] == '\0') {
        csp_print("svu_transfer: no destination path\n");
        return -1;
    }
    if (server_node > UINT16_MAX) {
        csp_print("svu_transfer: server node %u out of CSP address range\n",
                  server_node);
        return -1;
    }

    /* 0 means "use the build default", matching dtp_download_file's contract so
     * callers that passed 0 keep working. */
    const uint32_t use_mtu = (mtu != 0u) ? (uint32_t)mtu : 1024u;

    csp_print("svu_transfer: pulling from node %u -> %s (mtu %u, block %u, "
              "max %u round(s))\n",
              server_node, dest_path, use_mtu, SVU_BLOCK_SIZE, SVU_MAX_ROUNDS);

    /* Cross-pass persistence, keyed by app name. Disabled when the name is
     * unusable as a filename or the state dir cannot be made -- the transfer
     * still runs, it just cannot resume across an agent restart.
     *
     * The naive thesis control compiles this out along with recovery: v1's
     * naive arm (DTP, zero retry rounds, no sidecar) had neither, and the
     * ablation only isolates the mechanism if the control loses both. */
    svu_sidecar_t sidecar = { .fd = -1, .total = 0u, .path = {0} };
    const svu_client_hooks_t *hooks = NULL;
#ifndef SATDEPLOY_NAIVE_BASELINE
    svu_client_hooks_t sidecar_hooks = {
        .ctx = &sidecar,
        .restore = sidecar_restore,
        .meta = sidecar_meta,
        .data = sidecar_data,
        .complete = sidecar_complete,
    };
    if (app_name != NULL && session_state_dir_ensure() == 0 &&
        session_state_svu_path(app_name, sidecar.path,
                               sizeof(sidecar.path)) == 0) {
        hooks = &sidecar_hooks;
    } else {
        sidecar.path[0] = '\0';
        csp_print("svu_transfer: no usable sidecar path, resume disabled\n");
    }
#else
    (void)app_name;
#endif

    int rc = svu_client_run_hooked((uint16_t)server_node, SVU_BLOCK_SIZE,
                                   use_mtu, SVU_MAX_ROUNDS, dest_path, hooks);

    /* On anything but verified completion the sidecar file STAYS -- it is the
     * resume record -- but the descriptor must not leak. */
    if (sidecar.fd >= 0) {
        close(sidecar.fd);
        sidecar.fd = -1;
    }

    if (rc != 0) {
        /* Not a silent failure: SVU returns non-zero precisely when it could
         * not verify every block, which is the outcome v1's transport could
         * not report at all. */
        csp_print("svu_transfer: transfer did not verify\n");
        return -1;
    }

    /* Size is advisory -- the manifest already fixed the artifact length -- but
     * a mismatch means the ground served a different file than the one the
     * deploy request described, so it is worth catching here rather than at the
     * digest check. */
    if (expected_size != 0u) {
        struct stat st;
        if (stat(dest_path, &st) != 0) {
            csp_print("svu_transfer: cannot stat %s after transfer\n", dest_path);
            return -1;
        }
        if ((uint32_t)st.st_size != expected_size) {
            csp_print("svu_transfer: size mismatch for %s: got %ld, expected %u\n",
                      dest_path, (long)st.st_size, expected_size);
            return -1;
        }
    }

    return 0;
}

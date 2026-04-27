/**
 * session_state.c - cross-pass DTP session persistence.
 *
 * See session_state.h for the on-disk format and design rationale.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>

#include <openssl/evp.h>

#include "session_state.h"
#include "satdeploy_agent.h"

#include <dtp/dtp.h>
#include <dtp/dtp_session.h>
#include <dtp/dtp_protocol.h>

/* Reject app names that would escape SESSION_STATE_DIR or contain path
 * separators. We don't try to be clever with encoding — if the name has
 * anything funny in it, the deploy fails loudly. */
static bool app_name_is_safe(const char *app_name) {
    if (app_name == NULL || *app_name == '\0') {
        return false;
    }
    if (strstr(app_name, "..") != NULL) {
        return false;
    }
    for (const char *p = app_name; *p; p++) {
        if (*p == '/' || *p == '\\' || *p < 0x20) {
            return false;
        }
    }
    return true;
}

int session_state_path(const char *app_name, char *out, size_t out_size) {
    if (out == NULL || out_size == 0 || !app_name_is_safe(app_name)) {
        return -1;
    }
    int n = snprintf(out, out_size, "%s/%s%s",
                     SESSION_STATE_DIR, app_name, SESSION_STATE_EXT);
    if (n < 0 || (size_t)n >= out_size) {
        return -1;
    }
    return 0;
}

int session_state_dir_ensure(void) {
    /* mkdir_p sets 0755; tighten to 0700 once present so the state files are
     * not world-readable (deploy hashes are not secrets but there's no reason
     * to expose them). */
    if (mkdir_p(SESSION_STATE_DIR) != 0) {
        return -1;
    }
    if (chmod(SESSION_STATE_DIR, 0700) != 0 && errno != ENOENT) {
        /* Non-fatal: directory exists but we can't tighten perms (e.g., not
         * owner). Continue. */
        return 0;
    }
    return 0;
}

uint32_t session_state_compute_id(const char *app_name, const char *expected_hash) {
    EVP_MD_CTX *md_ctx = EVP_MD_CTX_new();
    if (md_ctx == NULL) {
        return 0;
    }
    if (EVP_DigestInit_ex(md_ctx, EVP_sha256(), NULL) != 1) {
        EVP_MD_CTX_free(md_ctx);
        return 0;
    }
    EVP_DigestUpdate(md_ctx, app_name, strlen(app_name));
    EVP_DigestUpdate(md_ctx, ":", 1);
    EVP_DigestUpdate(md_ctx, expected_hash, strlen(expected_hash));

    unsigned char digest[EVP_MAX_MD_SIZE];
    unsigned int digest_len = 0;
    EVP_DigestFinal_ex(md_ctx, digest, &digest_len);
    EVP_MD_CTX_free(md_ctx);

    /* First 4 bytes of digest, big-endian to little-endian conversion not
     * needed (the value is opaque; only equality across calls matters). */
    uint32_t id = ((uint32_t)digest[0] << 24) |
                  ((uint32_t)digest[1] << 16) |
                  ((uint32_t)digest[2] << 8)  |
                  ((uint32_t)digest[3]);
    /* Avoid 0 as session_id — libdtp uses 0 as "stop all" in dtp_stop_transfer
     * and might treat it as sentinel elsewhere. */
    return id == 0 ? 1 : id;
}

int session_state_exists(const char *path) {
    if (path == NULL) {
        return 0;
    }
    struct stat st;
    if (stat(path, &st) != 0) {
        return 0;
    }
    return S_ISREG(st.st_mode) ? 1 : 0;
}

void session_state_unlink(const char *path) {
    if (path == NULL) {
        return;
    }
    if (unlink(path) != 0 && errno != ENOENT) {
        /* Non-fatal: log and move on. The next attempt may try to read a
         * stale file but version checks should catch it. */
        printf("[session_state] warning: unlink(%s) failed: %s\n",
               path, strerror(errno));
    }
}

/* --- libdtp hooks --- */

void session_state_on_serialize(dtp_t *session, void *ctx) {
    if (session == NULL || ctx == NULL) {
        return;
    }
    session_state_ctx_t *sc = (session_state_ctx_t *)ctx;

    if (session_state_dir_ensure() != 0) {
        printf("[session_state] warning: cannot ensure %s, skipping checkpoint\n",
               SESSION_STATE_DIR);
        return;
    }

    /* Atomic write: temp file + rename(2). Avoids torn state on crash. */
    char tmp_path[640];
    int n = snprintf(tmp_path, sizeof(tmp_path), "%s.tmp", sc->path);
    if (n < 0 || (size_t)n >= sizeof(tmp_path)) {
        return;
    }

    FILE *f = fopen(tmp_path, "wb");
    if (f == NULL) {
        printf("[session_state] warning: fopen(%s) failed: %s\n",
               tmp_path, strerror(errno));
        return;
    }

    uint32_t format_version = SESSION_STATE_FORMAT_VERSION;
    uint32_t dtp_version = DTP_SESSION_VERSION;

    int ok = 1;
    ok &= (fwrite(&format_version, sizeof(uint32_t), 1, f) == 1);
    ok &= (fwrite(&dtp_version, sizeof(uint32_t), 1, f) == 1);
    ok &= (fwrite(sc->expected_hash, sizeof(sc->expected_hash), 1, f) == 1);
    ok &= (fwrite(&session->bytes_received, sizeof(uint32_t), 1, f) == 1);
    ok &= (fwrite(&session->payload_size, sizeof(uint32_t), 1, f) == 1);
    ok &= (fwrite(&session->request_meta, sizeof(dtp_meta_req_t), 1, f) == 1);
    ok &= (fwrite(&session->remote_cfg, sizeof(dtp_opt_remote_cfg), 1, f) == 1);

    /* Flush + fsync before rename so the bytes are durable. */
    if (fflush(f) != 0) ok = 0;
    int fd = fileno(f);
    if (fd >= 0 && fsync(fd) != 0) {
        /* fsync can fail on tmpfs; treat as warning, not abort. */
    }
    fclose(f);

    if (!ok) {
        printf("[session_state] warning: short write to %s, discarding\n", tmp_path);
        unlink(tmp_path);
        return;
    }

    if (rename(tmp_path, sc->path) != 0) {
        printf("[session_state] warning: rename(%s -> %s) failed: %s\n",
               tmp_path, sc->path, strerror(errno));
        unlink(tmp_path);
        return;
    }

    printf("[session_state] checkpoint: %u/%u bytes -> %s\n",
           session->bytes_received, session->payload_size, sc->path);
}

void session_state_on_deserialize(dtp_t *session, void *ctx) {
    if (session == NULL || ctx == NULL) {
        return;
    }
    session_state_ctx_t *sc = (session_state_ctx_t *)ctx;
    sc->resumed = false;

    FILE *f = fopen(sc->path, "rb");
    if (f == NULL) {
        /* No state file = no resume. Caller treats as fresh transfer. */
        return;
    }

    uint32_t format_version = 0;
    uint32_t dtp_version = 0;
    char on_disk_hash[65] = {0};
    uint32_t bytes_received = 0;
    uint32_t payload_size = 0;
    dtp_meta_req_t request_meta;
    dtp_opt_remote_cfg remote_cfg;
    memset(&request_meta, 0, sizeof(request_meta));
    memset(&remote_cfg, 0, sizeof(remote_cfg));

    int ok = 1;
    ok &= (fread(&format_version, sizeof(uint32_t), 1, f) == 1);
    ok &= (fread(&dtp_version, sizeof(uint32_t), 1, f) == 1);
    ok &= (fread(on_disk_hash, sizeof(on_disk_hash), 1, f) == 1);
    ok &= (fread(&bytes_received, sizeof(uint32_t), 1, f) == 1);
    ok &= (fread(&payload_size, sizeof(uint32_t), 1, f) == 1);
    ok &= (fread(&request_meta, sizeof(dtp_meta_req_t), 1, f) == 1);
    ok &= (fread(&remote_cfg, sizeof(dtp_opt_remote_cfg), 1, f) == 1);
    fclose(f);

    if (!ok) {
        printf("[session_state] %s short/corrupt, discarding\n", sc->path);
        session_state_unlink(sc->path);
        return;
    }

    if (format_version != SESSION_STATE_FORMAT_VERSION) {
        printf("[session_state] %s format version %u != expected %u, discarding\n",
               sc->path, format_version, SESSION_STATE_FORMAT_VERSION);
        session_state_unlink(sc->path);
        return;
    }

    if (dtp_version != DTP_SESSION_VERSION) {
        printf("[session_state] %s DTP version %u != current %u, discarding\n",
               sc->path, dtp_version, DTP_SESSION_VERSION);
        session_state_unlink(sc->path);
        return;
    }

    /* Strict equality: ground rebuild between passes (different SHA256) means
     * the on-disk partial bytes are now garbage. Discard the state and the
     * caller's "r+b" temp-file open will be silently truncated by re-fetch. */
    on_disk_hash[sizeof(on_disk_hash) - 1] = '\0';
    if (strcmp(on_disk_hash, sc->expected_hash) != 0) {
        printf("[session_state] %s hash mismatch (on-disk=%.8s expected=%.8s), discarding\n",
               sc->path, on_disk_hash, sc->expected_hash);
        session_state_unlink(sc->path);
        return;
    }

    /* All checks pass — restore session. */
    session->bytes_received = bytes_received;
    session->payload_size = payload_size;
    session->request_meta = request_meta;
    session->remote_cfg = remote_cfg;
    sc->resumed = true;

    printf("[session_state] resumed %u/%u bytes from %s\n",
           bytes_received, payload_size, sc->path);
}

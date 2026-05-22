/*
 * session_state.c - cross-pass DTP transfer persistence (bitmap-based).
 *
 * See session_state_internal.h for the on-disk format and design rationale.
 *
 * Adapted from satdeploy-agent/src/session_state.c during the libsatpush
 * extract (Step 2 of /plan-eng-review 2026-05-21). Changes from the
 * pre-extract version:
 *   - state_dir flows in from caller; SESSION_STATE_DIR constant is gone.
 *   - mkdir_p inlined here so we no longer depend on satdeploy_agent.h.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>

#include "sha256.h"
#include "session_state_internal.h"

/* Reject resume keys that would escape state_dir or contain path
 * separators. We don't try to be clever with encoding - any unsafe character
 * fails resolution at the path step rather than risking writes outside the
 * state dir. */
static bool resume_key_is_safe(const char *resume_key) {
    if (resume_key == NULL || *resume_key == '\0') {
        return false;
    }
    if (strstr(resume_key, "..") != NULL) {
        return false;
    }
    for (const char *p = resume_key; *p; p++) {
        if (*p == '/' || *p == '\\' || (unsigned char)*p < 0x20) {
            return false;
        }
    }
    return true;
}

/* Inlined mkdir -p equivalent. Walks the path creating each directory
 * component with mode 0700. Idempotent: existing directories are accepted
 * without error. Returns 0 on success, -1 on I/O failure. */
static int mkdir_p_local(const char *path) {
    if (path == NULL || *path == '\0') {
        return -1;
    }

    char buf[640];
    size_t len = strlen(path);
    if (len >= sizeof(buf)) {
        return -1;
    }
    memcpy(buf, path, len + 1);

    /* Strip trailing slash so the loop below doesn't try to mkdir("") */
    if (len > 1 && buf[len - 1] == '/') {
        buf[len - 1] = '\0';
    }

    for (char *p = buf + 1; *p; p++) {
        if (*p == '/') {
            *p = '\0';
            if (mkdir(buf, 0700) != 0 && errno != EEXIST) {
                return -1;
            }
            *p = '/';
        }
    }
    if (mkdir(buf, 0700) != 0 && errno != EEXIST) {
        return -1;
    }
    return 0;
}

int session_state_path(const char *state_dir,
                       const char *resume_key,
                       char *out, size_t out_size) {
    if (out == NULL || out_size == 0 || state_dir == NULL ||
        !resume_key_is_safe(resume_key)) {
        return -1;
    }
    int n = snprintf(out, out_size, "%s/%s%s",
                     state_dir, resume_key, SESSION_STATE_EXT);
    if (n < 0 || (size_t)n >= out_size) {
        return -1;
    }
    return 0;
}

int session_state_dir_ensure(const char *state_dir) {
    if (state_dir == NULL || *state_dir == '\0') {
        return -1;
    }
    if (mkdir_p_local(state_dir) != 0) {
        return -1;
    }
    /* Tighten perms if possible. Non-fatal if we don't own the dir. */
    (void)chmod(state_dir, 0700);
    return 0;
}

uint32_t session_state_compute_id(const char *resume_key, const char *expected_hash) {
    if (resume_key == NULL || expected_hash == NULL) {
        return 1;
    }
    sha256_ctx ctx;
    sha256_init(&ctx);
    sha256_update(&ctx, (const uint8_t *)resume_key, strlen(resume_key));
    sha256_update(&ctx, (const uint8_t *)":", 1);
    sha256_update(&ctx, (const uint8_t *)expected_hash, strlen(expected_hash));

    uint8_t digest[SHA256_DIGEST_SIZE];
    sha256_final(&ctx, digest);

    uint32_t id = ((uint32_t)digest[0] << 24) |
                  ((uint32_t)digest[1] << 16) |
                  ((uint32_t)digest[2] << 8)  |
                  ((uint32_t)digest[3]);
    /* Avoid 0 - libdtp uses 0 as "all sessions" in dtp_stop_transfer. */
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
        printf("[libsatpush] warning: unlink(%s) failed: %s\n",
               path, strerror(errno));
    }
}

int session_state_save(const char *path,
                       uint32_t expected_size,
                       const char *expected_hash,
                       uint32_t nof_packets,
                       uint16_t effective_mtu,
                       const uint8_t *bitmap,
                       size_t bitmap_bytes) {
    if (path == NULL || expected_hash == NULL ||
        (bitmap == NULL && bitmap_bytes > 0)) {
        return -1;
    }

    /* state_dir was created at satpush_create() time; if it has gone missing
     * since (operator wiped /var, tmpfs unmount, etc.) skip the checkpoint
     * rather than recreate without the caller's knowledge. */

    char tmp_path[640];
    int n = snprintf(tmp_path, sizeof(tmp_path), "%s.tmp", path);
    if (n < 0 || (size_t)n >= sizeof(tmp_path)) {
        return -1;
    }

    FILE *f = fopen(tmp_path, "wb");
    if (f == NULL) {
        printf("[libsatpush] warning: fopen(%s) failed: %s\n",
               tmp_path, strerror(errno));
        return -1;
    }

    uint32_t format_version = SESSION_STATE_FORMAT_VERSION;
    char hash_buf[65];
    memset(hash_buf, 0, sizeof(hash_buf));
    strncpy(hash_buf, expected_hash, sizeof(hash_buf) - 1);
    uint16_t reserved = 0;

    int ok = 1;
    ok &= (fwrite(&format_version, sizeof(uint32_t), 1, f) == 1);
    ok &= (fwrite(&expected_size, sizeof(uint32_t), 1, f) == 1);
    ok &= (fwrite(hash_buf, sizeof(hash_buf), 1, f) == 1);
    ok &= (fwrite(&nof_packets, sizeof(uint32_t), 1, f) == 1);
    ok &= (fwrite(&effective_mtu, sizeof(uint16_t), 1, f) == 1);
    ok &= (fwrite(&reserved, sizeof(uint16_t), 1, f) == 1);
    if (bitmap_bytes > 0) {
        ok &= (fwrite(bitmap, 1, bitmap_bytes, f) == bitmap_bytes);
    }

    if (fflush(f) != 0) ok = 0;
    int fd = fileno(f);
    if (fd >= 0) {
        /* Best-effort fsync; tmpfs etc. may refuse and that's OK. */
        (void)fsync(fd);
    }
    fclose(f);

    if (!ok) {
        printf("[libsatpush] warning: short write to %s, discarding\n", tmp_path);
        unlink(tmp_path);
        return -1;
    }

    if (rename(tmp_path, path) != 0) {
        printf("[libsatpush] warning: rename(%s -> %s) failed: %s\n",
               tmp_path, path, strerror(errno));
        unlink(tmp_path);
        return -1;
    }

    return 0;
}

int session_state_load(const char *path,
                       uint32_t expected_size,
                       const char *expected_hash,
                       uint32_t nof_packets,
                       uint16_t effective_mtu,
                       uint8_t *bitmap_out,
                       size_t bitmap_bytes) {
    if (path == NULL || expected_hash == NULL ||
        (bitmap_out == NULL && bitmap_bytes > 0)) {
        return 0;
    }

    FILE *f = fopen(path, "rb");
    if (f == NULL) {
        return 0;
    }

    uint32_t format_version = 0;
    uint32_t on_disk_size = 0;
    char on_disk_hash[65] = {0};
    uint32_t on_disk_nof_packets = 0;
    uint16_t on_disk_eff_mtu = 0;
    uint16_t reserved = 0;

    int ok = 1;
    ok &= (fread(&format_version, sizeof(uint32_t), 1, f) == 1);
    ok &= (fread(&on_disk_size, sizeof(uint32_t), 1, f) == 1);
    ok &= (fread(on_disk_hash, sizeof(on_disk_hash), 1, f) == 1);
    ok &= (fread(&on_disk_nof_packets, sizeof(uint32_t), 1, f) == 1);
    ok &= (fread(&on_disk_eff_mtu, sizeof(uint16_t), 1, f) == 1);
    ok &= (fread(&reserved, sizeof(uint16_t), 1, f) == 1);

    if (!ok) {
        fclose(f);
        printf("[libsatpush] %s header corrupt, discarding\n", path);
        session_state_unlink(path);
        return 0;
    }

    if (format_version != SESSION_STATE_FORMAT_VERSION) {
        fclose(f);
        printf("[libsatpush] %s format version %u != expected %u, discarding\n",
               path, format_version, SESSION_STATE_FORMAT_VERSION);
        session_state_unlink(path);
        return 0;
    }

    on_disk_hash[sizeof(on_disk_hash) - 1] = '\0';
    if (strcmp(on_disk_hash, expected_hash) != 0) {
        fclose(f);
        printf("[libsatpush] %s hash mismatch (on-disk=%.8s expected=%.8s), discarding\n",
               path, on_disk_hash, expected_hash);
        session_state_unlink(path);
        return 0;
    }

    if (on_disk_size != expected_size ||
        on_disk_nof_packets != nof_packets ||
        on_disk_eff_mtu != effective_mtu) {
        fclose(f);
        printf("[libsatpush] %s shape mismatch (size %u/%u, packets %u/%u, "
               "mtu %u/%u), discarding\n",
               path, on_disk_size, expected_size,
               on_disk_nof_packets, nof_packets,
               on_disk_eff_mtu, effective_mtu);
        session_state_unlink(path);
        return 0;
    }

    if (bitmap_bytes > 0) {
        size_t got = fread(bitmap_out, 1, bitmap_bytes, f);
        if (got != bitmap_bytes) {
            fclose(f);
            printf("[libsatpush] %s bitmap short (%zu/%zu), discarding\n",
                   path, got, bitmap_bytes);
            session_state_unlink(path);
            memset(bitmap_out, 0, bitmap_bytes);
            return 0;
        }
    }
    fclose(f);

    return 1;
}

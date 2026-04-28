/**
 * session_state.c - cross-pass DTP transfer persistence (bitmap-based).
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

#include "sha256.h"
#include "session_state.h"
#include "satdeploy_agent.h"

/* Reject app names that would escape SESSION_STATE_DIR or contain path
 * separators. We don't try to be clever with encoding — any unsafe character
 * fails the deploy at the state-path resolution step rather than risking
 * writes outside the state dir. */
static bool app_name_is_safe(const char *app_name) {
    if (app_name == NULL || *app_name == '\0') {
        return false;
    }
    if (strstr(app_name, "..") != NULL) {
        return false;
    }
    for (const char *p = app_name; *p; p++) {
        if (*p == '/' || *p == '\\' || (unsigned char)*p < 0x20) {
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
    if (mkdir_p(SESSION_STATE_DIR) != 0) {
        return -1;
    }
    /* Tighten perms if possible. Non-fatal if we don't own the dir. */
    (void)chmod(SESSION_STATE_DIR, 0700);
    return 0;
}

uint32_t session_state_compute_id(const char *app_name, const char *expected_hash) {
    if (app_name == NULL || expected_hash == NULL) {
        return 1;
    }
    sha256_ctx ctx;
    sha256_init(&ctx);
    sha256_update(&ctx, (const uint8_t *)app_name, strlen(app_name));
    sha256_update(&ctx, (const uint8_t *)":", 1);
    sha256_update(&ctx, (const uint8_t *)expected_hash, strlen(expected_hash));

    uint8_t digest[SHA256_DIGEST_SIZE];
    sha256_final(&ctx, digest);

    uint32_t id = ((uint32_t)digest[0] << 24) |
                  ((uint32_t)digest[1] << 16) |
                  ((uint32_t)digest[2] << 8)  |
                  ((uint32_t)digest[3]);
    /* Avoid 0 — libdtp uses 0 as "all sessions" in dtp_stop_transfer. */
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
        printf("[session_state] warning: unlink(%s) failed: %s\n",
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

    if (session_state_dir_ensure() != 0) {
        printf("[session_state] warning: cannot ensure %s, skipping checkpoint\n",
               SESSION_STATE_DIR);
        return -1;
    }

    char tmp_path[640];
    int n = snprintf(tmp_path, sizeof(tmp_path), "%s.tmp", path);
    if (n < 0 || (size_t)n >= sizeof(tmp_path)) {
        return -1;
    }

    FILE *f = fopen(tmp_path, "wb");
    if (f == NULL) {
        printf("[session_state] warning: fopen(%s) failed: %s\n",
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
        printf("[session_state] warning: short write to %s, discarding\n", tmp_path);
        unlink(tmp_path);
        return -1;
    }

    if (rename(tmp_path, path) != 0) {
        printf("[session_state] warning: rename(%s -> %s) failed: %s\n",
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
        printf("[session_state] %s header corrupt, discarding\n", path);
        session_state_unlink(path);
        return 0;
    }

    if (format_version != SESSION_STATE_FORMAT_VERSION) {
        fclose(f);
        printf("[session_state] %s format version %u != expected %u, discarding\n",
               path, format_version, SESSION_STATE_FORMAT_VERSION);
        session_state_unlink(path);
        return 0;
    }

    on_disk_hash[sizeof(on_disk_hash) - 1] = '\0';
    if (strcmp(on_disk_hash, expected_hash) != 0) {
        fclose(f);
        printf("[session_state] %s hash mismatch (on-disk=%.8s expected=%.8s), discarding\n",
               path, on_disk_hash, expected_hash);
        session_state_unlink(path);
        return 0;
    }

    if (on_disk_size != expected_size ||
        on_disk_nof_packets != nof_packets ||
        on_disk_eff_mtu != effective_mtu) {
        fclose(f);
        printf("[session_state] %s shape mismatch (size %u/%u, packets %u/%u, "
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
            printf("[session_state] %s bitmap short (%zu/%zu), discarding\n",
                   path, got, bitmap_bytes);
            session_state_unlink(path);
            memset(bitmap_out, 0, bitmap_bytes);
            return 0;
        }
    }
    fclose(f);

    return 1;
}

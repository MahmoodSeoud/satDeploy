/**
 * Backup manager - handles file backup, restore, and listing
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/stat.h>
#include <dirent.h>
#include <errno.h>
#include <unistd.h>

#include "satdeploy_agent.h"

#include <stdint.h>
#include "sha256.h"

int compute_file_checksum(const char *path, char *hash_out, size_t hash_size) {
    if (hash_out == NULL || hash_size < HASH_BUF_LEN) {
        return -1;
    }

    FILE *f = fopen(path, "rb");
    if (f == NULL) {
        return -1;
    }

    sha256_ctx ctx;
    sha256_init(&ctx);

    uint8_t buffer[8192];
    size_t n;
    while ((n = fread(buffer, 1, sizeof(buffer), f)) > 0) {
        sha256_update(&ctx, buffer, n);
    }

    if (ferror(f)) {
        fclose(f);
        return -1;
    }

    fclose(f);

    uint8_t digest[SHA256_DIGEST_SIZE];
    sha256_final(&ctx, digest);

    /* Full 32-byte SHA256 → 64 hex chars + NUL.
     * Truncating to 8 chars on the wire was unsafe for cross-pass resume:
     * an 8-char prefix collision lets a re-staged binary inherit a stale
     * receive bitmap. Display still uses %.8s for readability. */
    for (unsigned int i = 0; i < 32; i++) {
        snprintf(hash_out + (i * 2), 3, "%02x", digest[i]);
    }
    hash_out[HASH_HEX_LEN] = '\0';
    return 0;
}

int mkdir_p(const char *path) {
    char tmp[MAX_PATH_LEN];
    char *p = NULL;
    size_t len;

    snprintf(tmp, sizeof(tmp), "%s", path);
    len = strlen(tmp);
    if (len > 0 && tmp[len - 1] == '/')
        tmp[len - 1] = 0;

    for (p = tmp + 1; *p; p++) {
        if (*p == '/') {
            *p = 0;
            if (mkdir(tmp, 0755) != 0 && errno != EEXIST)
                return -1;
            *p = '/';
        }
    }
    if (mkdir(tmp, 0755) != 0 && errno != EEXIST)
        return -1;

    return 0;
}

int copy_file(const char *src, const char *dst) {
    FILE *fin = fopen(src, "rb");
    if (fin == NULL) {
        return -1;
    }

    /* Remove destination first (required for running binaries - ETXTBSY) */
    unlink(dst);

    FILE *fout = fopen(dst, "wb");
    if (fout == NULL) {
        fclose(fin);
        return -1;
    }

    uint8_t buffer[8192];
    size_t n;
    int result = 0;

    while ((n = fread(buffer, 1, sizeof(buffer), fin)) > 0) {
        if (fwrite(buffer, 1, n, fout) != n) {
            result = -1;
            break;
        }
    }

    fclose(fin);
    fclose(fout);

    /* Preserve executable bit */
    if (result == 0) {
        struct stat st;
        if (stat(src, &st) == 0) {
            chmod(dst, st.st_mode);
        }
    }

    return result;
}

/* Backup retention knob (opt-in; default = unlimited, i.e. shipped behavior).
 *   SATDEPLOY_BACKUP_KEEP unset/empty -> -1 (no cap, original behavior)
 *   SATDEPLOY_BACKUP_KEEP=0           -> backups disabled (rollback feature off)
 *   SATDEPLOY_BACKUP_KEEP=N (N>0)     -> keep only the N newest .bak GLOBALLY
 * Used to bound /opt during loss-sweep measurement runs that deploy many
 * unique app_names (each app_name is a separate backup dir, so a per-app cap
 * would not bound total disk). Orthogonal to transfer recovery (ARQ/sha256). */
static int backup_keep_limit(void) {
    const char *v = getenv("SATDEPLOY_BACKUP_KEEP");
    if (v == NULL || *v == '\0') {
        return -1;
    }
    int n = atoi(v);
    return n < 0 ? -1 : n;
}

struct bak_ent { char path[MAX_PATH_LEN]; time_t mtime; };

/* Prune oldest .bak files across ALL app subdirs of BACKUP_DIR until at most
 * `keep` remain. Runs after each backup_create, so steady state stays bounded. */
static void backup_prune_global(int keep) {
    if (keep < 0) {
        return;
    }
    DIR *root = opendir(BACKUP_DIR);
    if (root == NULL) {
        return;
    }

    /* Pass 1: count .bak files. */
    size_t cap = 0;
    struct dirent *ade;
    while ((ade = readdir(root)) != NULL) {
        if (ade->d_name[0] == '.') continue;
        char appdir[MAX_PATH_LEN];
        snprintf(appdir, sizeof(appdir), "%s/%s", BACKUP_DIR, ade->d_name);
        struct stat ast;
        if (stat(appdir, &ast) != 0 || !S_ISDIR(ast.st_mode)) continue;
        DIR *ad = opendir(appdir);
        if (ad == NULL) continue;
        struct dirent *fe;
        while ((fe = readdir(ad)) != NULL) {
            size_t l = strlen(fe->d_name);
            if (l >= 4 && strcmp(fe->d_name + l - 4, ".bak") == 0) cap++;
        }
        closedir(ad);
    }
    if (cap == 0 || cap <= (size_t)keep) {
        closedir(root);
        return;
    }

    struct bak_ent *ents = malloc(cap * sizeof(*ents));
    if (ents == NULL) {
        closedir(root);
        return;
    }

    /* Pass 2: collect path + mtime. */
    rewinddir(root);
    size_t n = 0;
    while ((ade = readdir(root)) != NULL && n < cap) {
        if (ade->d_name[0] == '.') continue;
        char appdir[MAX_PATH_LEN];
        snprintf(appdir, sizeof(appdir), "%s/%s", BACKUP_DIR, ade->d_name);
        struct stat ast;
        if (stat(appdir, &ast) != 0 || !S_ISDIR(ast.st_mode)) continue;
        DIR *ad = opendir(appdir);
        if (ad == NULL) continue;
        struct dirent *fe;
        while ((fe = readdir(ad)) != NULL && n < cap) {
            size_t l = strlen(fe->d_name);
            if (!(l >= 4 && strcmp(fe->d_name + l - 4, ".bak") == 0)) continue;
            char p[MAX_PATH_LEN * 2];
            snprintf(p, sizeof(p), "%s/%s", appdir, fe->d_name);
            struct stat fst;
            if (stat(p, &fst) != 0) continue;
            strncpy(ents[n].path, p, MAX_PATH_LEN - 1);
            ents[n].path[MAX_PATH_LEN - 1] = '\0';
            ents[n].mtime = fst.st_mtime;
            n++;
        }
        closedir(ad);
    }
    closedir(root);

    if (n > (size_t)keep) {
        /* Insertion sort ascending by mtime (n is small in steady state). */
        for (size_t i = 1; i < n; i++) {
            struct bak_ent key = ents[i];
            size_t j = i;
            while (j > 0 && ents[j - 1].mtime > key.mtime) {
                ents[j] = ents[j - 1];
                j--;
            }
            ents[j] = key;
        }
        size_t to_remove = n - (size_t)keep;
        for (size_t i = 0; i < to_remove; i++) {
            unlink(ents[i].path);
        }
    }
    free(ents);
}

int backup_create(const char *app_name, const char *src_path,
                  char *backup_path_out, size_t backup_path_size) {
    if (app_name == NULL || src_path == NULL) {
        return -1;
    }

    /* Retention disabled entirely (KEEP=0): skip backup, deploy still succeeds. */
    int keep = backup_keep_limit();
    if (keep == 0) {
        return 0;
    }

    /* Check source file exists */
    struct stat st;
    if (stat(src_path, &st) != 0) {
        return -1;
    }

    /* Compute checksum of source */
    char hash[HASH_BUF_LEN];
    if (compute_file_checksum(src_path, hash, sizeof(hash)) != 0) {
        return -1;
    }

    /* Create backup directory */
    char backup_dir[MAX_PATH_LEN];
    snprintf(backup_dir, sizeof(backup_dir), "%s/%s", BACKUP_DIR, app_name);

    if (mkdir_p(BACKUP_DIR) != 0 || mkdir_p(backup_dir) != 0) {
        return -1;
    }

    /* Generate backup filename: YYYYMMDD-HHMMSS-hash.bak (matches ground station) */
    time_t now = time(NULL);
    struct tm tm_info;
    localtime_r(&now, &tm_info);
    char timestamp[16];
    strftime(timestamp, sizeof(timestamp), "%Y%m%d-%H%M%S", &tm_info);

    char backup_path[MAX_PATH_LEN];
    snprintf(backup_path, sizeof(backup_path), "%s/%s-%s.bak",
             backup_dir, timestamp, hash);

    /* Check if this hash already backed up - search by hash suffix */
    DIR *dir = opendir(backup_dir);
    if (dir != NULL) {
        char hash_suffix[HASH_BUF_LEN + 8]; /* "-<64hex>.bak" + NUL */
        snprintf(hash_suffix, sizeof(hash_suffix), "-%s.bak", hash);
        struct dirent *entry;
        while ((entry = readdir(dir)) != NULL) {
            size_t name_len = strlen(entry->d_name);
            size_t suffix_len = strlen(hash_suffix);
            if (name_len >= suffix_len &&
                strcmp(entry->d_name + name_len - suffix_len, hash_suffix) == 0) {
                /* Found existing backup with same hash */
                char existing_path[MAX_PATH_LEN];
                snprintf(existing_path, sizeof(existing_path), "%s/%s",
                         backup_dir, entry->d_name);
                /* hash already backed up — skip */
                if (backup_path_out != NULL && backup_path_size > 0) {
                    strncpy(backup_path_out, existing_path, backup_path_size - 1);
                    backup_path_out[backup_path_size - 1] = '\0';
                }
                closedir(dir);
                return 0;
            }
        }
        closedir(dir);
    }

    /* Copy file to backup */
    if (copy_file(src_path, backup_path) != 0) {
        return -1;
    }

    /* Return backup path */
    if (backup_path_out != NULL && backup_path_size > 0) {
        strncpy(backup_path_out, backup_path, backup_path_size - 1);
        backup_path_out[backup_path_size - 1] = '\0';
    }

    printf("[backup] backed up → %.8s\n", hash);
    fflush(stdout);

    /* Enforce global retention cap (no-op when unset/-1). */
    backup_prune_global(keep);
    return 0;
}

int backup_restore(const char *backup_path, const char *dest_path) {
    if (backup_path == NULL || dest_path == NULL) {
        return -1;
    }

    /* Check backup file exists */
    struct stat st;
    if (stat(backup_path, &st) != 0) {
        return -1;
    }

    /* Copy backup to destination */
    if (copy_file(backup_path, dest_path) != 0) {
        return -1;
    }

    /* Preserve the backup file's permissions */
    struct stat bst;
    if (stat(backup_path, &bst) == 0) {
        chmod(dest_path, bst.st_mode);
    } else {
        chmod(dest_path, 0755);  /* fallback */
    }

    printf("\033[32m[backup] restored → %s\033[0m\n", dest_path);
    fflush(stdout);
    return 0;
}

/**
 * Parse backup filename to extract version, timestamp, and hash.
 * Current format: YYYYMMDD-HHMMSS-<hash>.bak (matches ground station)
 * Legacy format: <hash>.bak (hash-only, pre-unification)
 */
static int parse_backup_filename(const char *filename, const char *full_path,
                                  char *version, size_t version_size,
                                  char *timestamp, size_t timestamp_size,
                                  char *hash, size_t hash_size) {
    /* Check for .bak extension */
    size_t len = strlen(filename);
    if (len < 5 || strcmp(filename + len - 4, ".bak") != 0) {
        return -1;
    }

    /* Copy without extension */
    char name[MAX_PATH_LEN];
    strncpy(name, filename, len - 4);
    name[len - 4] = '\0';

    /* Try legacy format first: just hash (8 hex chars) */
    if (len == 12) {  /* 8 chars hash + 4 chars ".bak" */
        /* Legacy format: {hash}.bak */
        if (hash != NULL && hash_size > 0) {
            strncpy(hash, name, hash_size - 1);
            hash[hash_size - 1] = '\0';
        }

        if (version != NULL && version_size > 0) {
            strncpy(version, name, version_size - 1);
            version[version_size - 1] = '\0';
        }

        /* Get timestamp from file mtime (ISO 8601 format) */
        if (timestamp != NULL && timestamp_size > 0 && full_path != NULL) {
            struct stat st;
            if (stat(full_path, &st) == 0) {
                struct tm tm;
                localtime_r(&st.st_mtime, &tm);
                snprintf(timestamp, timestamp_size, "%04d-%02d-%02dT%02d:%02d:%02d",
                         tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday,
                         tm.tm_hour, tm.tm_min, tm.tm_sec);
            } else {
                strncpy(timestamp, "unknown", timestamp_size - 1);
            }
        }
        return 0;
    }

    /* Current format: YYYYMMDD-HHMMSS-hash (full 64-hex SHA256) */
    int year, mon, day, hour, min, sec;
    char hash_buf[HASH_BUF_LEN];

    if (sscanf(name, "%4d%2d%2d-%2d%2d%2d-%64s",
               &year, &mon, &day, &hour, &min, &sec, hash_buf) == 7) {
        if (version != NULL && version_size > 0) {
            snprintf(version, version_size, "%s", name);
        }

        if (timestamp != NULL && timestamp_size > 0) {
            snprintf(timestamp, timestamp_size, "%04d-%02d-%02dT%02d:%02d:%02d",
                     year, mon, day, hour, min, sec);
        }

        if (hash != NULL && hash_size > 0) {
            strncpy(hash, hash_buf, hash_size - 1);
            hash[hash_size - 1] = '\0';
        }
        return 0;
    }

    return -1;  /* Unknown format */
}

int backup_list(const char *app_name, backup_list_callback callback, void *user_data) {
    if (app_name == NULL || callback == NULL) {
        return -1;
    }

    char backup_dir[MAX_PATH_LEN];
    snprintf(backup_dir, sizeof(backup_dir), "%s/%s", BACKUP_DIR, app_name);

    DIR *dir = opendir(backup_dir);
    if (dir == NULL) {
        return 0;  /* No backups directory = 0 backups */
    }

    int count = 0;
    struct dirent *entry;

    while ((entry = readdir(dir)) != NULL) {
        if (entry->d_type != DT_REG) {
            continue;
        }

        char version[96], timestamp[32], hash[HASH_BUF_LEN], path[MAX_PATH_LEN];
        snprintf(path, sizeof(path), "%s/%s", backup_dir, entry->d_name);

        if (parse_backup_filename(entry->d_name, path, version, sizeof(version),
                                   timestamp, sizeof(timestamp),
                                   hash, sizeof(hash)) == 0) {
            callback(version, timestamp, hash, path, user_data);
            count++;
        }
    }

    closedir(dir);
    return count;
}

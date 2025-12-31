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

/* Simple SHA256 implementation using OpenSSL-style interface */
/* For now, we use a simple hash. In production, use OpenSSL or similar. */

#include <stdint.h>

/* Simplified hash for testing - in production use proper SHA256 */
static void simple_hash(const uint8_t *data, size_t len, uint8_t *out) {
    uint32_t h = 0x811c9dc5;  /* FNV-1a offset basis */
    for (size_t i = 0; i < len; i++) {
        h ^= data[i];
        h *= 0x01000193;  /* FNV-1a prime */
    }
    /* Extend to 32 bytes (we only use first 4 for the 8-char hex) */
    memcpy(out, &h, 4);
    memset(out + 4, 0, 28);
}

int compute_file_checksum(const char *path, char *hash_out, size_t hash_size) {
    if (hash_size < 9) {
        return -1;
    }

    FILE *f = fopen(path, "rb");
    if (f == NULL) {
        return -1;
    }

    /* Read file and compute hash */
    uint8_t buffer[8192];
    uint8_t hash[32];
    uint32_t h = 0x811c9dc5;

    size_t n;
    while ((n = fread(buffer, 1, sizeof(buffer), f)) > 0) {
        for (size_t i = 0; i < n; i++) {
            h ^= buffer[i];
            h *= 0x01000193;
        }
    }

    fclose(f);

    /* Convert to hex (first 8 chars) */
    snprintf(hash_out, hash_size, "%08x", h);
    return 0;
}

/**
 * Ensure directory exists, creating if necessary.
 */
static int ensure_dir(const char *path) {
    struct stat st;
    if (stat(path, &st) == 0) {
        return S_ISDIR(st.st_mode) ? 0 : -1;
    }
    if (mkdir(path, 0755) != 0 && errno != EEXIST) {
        return -1;
    }
    return 0;
}

/**
 * Copy a file.
 */
static int copy_file(const char *src, const char *dst) {
    FILE *fin = fopen(src, "rb");
    if (fin == NULL) {
        return -1;
    }

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

int backup_create(const char *app_name, const char *src_path,
                  char *backup_path_out, size_t backup_path_size) {
    if (app_name == NULL || src_path == NULL) {
        return -1;
    }

    /* Check source file exists */
    struct stat st;
    if (stat(src_path, &st) != 0) {
        return -1;
    }

    /* Compute checksum of source */
    char hash[16];
    if (compute_file_checksum(src_path, hash, sizeof(hash)) != 0) {
        return -1;
    }

    /* Create backup directory */
    char backup_dir[MAX_PATH_LEN];
    snprintf(backup_dir, sizeof(backup_dir), "%s/%s", BACKUP_DIR, app_name);

    if (ensure_dir(BACKUP_DIR) != 0 || ensure_dir(backup_dir) != 0) {
        return -1;
    }

    /* Generate backup filename: YYYYMMDD-HHMMSS-<hash>.bak */
    time_t now = time(NULL);
    struct tm *tm = localtime(&now);

    char backup_path[MAX_PATH_LEN];
    snprintf(backup_path, sizeof(backup_path),
             "%s/%04d%02d%02d-%02d%02d%02d-%s.bak",
             backup_dir,
             tm->tm_year + 1900, tm->tm_mon + 1, tm->tm_mday,
             tm->tm_hour, tm->tm_min, tm->tm_sec,
             hash);

    /* Copy file to backup */
    if (copy_file(src_path, backup_path) != 0) {
        return -1;
    }

    /* Return backup path */
    if (backup_path_out != NULL && backup_path_size > 0) {
        strncpy(backup_path_out, backup_path, backup_path_size - 1);
        backup_path_out[backup_path_size - 1] = '\0';
    }

    printf("[backup] Created: %s\n", backup_path);
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

    /* Make executable */
    chmod(dest_path, 0755);

    printf("[backup] Restored: %s -> %s\n", backup_path, dest_path);
    return 0;
}

/**
 * Parse backup filename to extract version, timestamp, and hash.
 * Format: YYYYMMDD-HHMMSS-<hash>.bak
 */
static int parse_backup_filename(const char *filename,
                                  char *version, size_t version_size,
                                  char *timestamp, size_t timestamp_size,
                                  char *hash, size_t hash_size) {
    /* Check for .bak extension */
    size_t len = strlen(filename);
    if (len < 24 || strcmp(filename + len - 4, ".bak") != 0) {
        return -1;
    }

    /* Copy without extension */
    char name[MAX_PATH_LEN];
    strncpy(name, filename, len - 4);
    name[len - 4] = '\0';

    /* Parse: YYYYMMDD-HHMMSS-hash */
    int year, mon, day, hour, min, sec;
    char hash_buf[32];

    if (sscanf(name, "%4d%2d%2d-%2d%2d%2d-%s",
               &year, &mon, &day, &hour, &min, &sec, hash_buf) != 7) {
        return -1;
    }

    if (version != NULL && version_size > 0) {
        snprintf(version, version_size, "%s", name);
    }

    if (timestamp != NULL && timestamp_size > 0) {
        snprintf(timestamp, timestamp_size, "%04d-%02d-%02d %02d:%02d:%02d",
                 year, mon, day, hour, min, sec);
    }

    if (hash != NULL && hash_size > 0) {
        strncpy(hash, hash_buf, hash_size - 1);
        hash[hash_size - 1] = '\0';
    }

    return 0;
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

        char version[64], timestamp[32], hash[16], path[MAX_PATH_LEN];

        if (parse_backup_filename(entry->d_name, version, sizeof(version),
                                   timestamp, sizeof(timestamp),
                                   hash, sizeof(hash)) == 0) {
            snprintf(path, sizeof(path), "%s/%s", backup_dir, entry->d_name);
            callback(version, timestamp, hash, path, user_data);
            count++;
        }
    }

    closedir(dir);
    return count;
}

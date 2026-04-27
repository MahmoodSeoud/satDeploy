/**
 * App metadata manager - tracks deployed apps and their state
 *
 * Stores metadata in /opt/satdeploy/apps.json with format:
 * {
 *   "app_name": {
 *     "remote_path": "/path/to/file",
 *     "file_hash": "083fa1c0",
 *     "deployed_at": "2026-01-02T11:34:23"
 *   }
 * }
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/stat.h>
#include <errno.h>

#include "satdeploy_agent.h"

/* Metadata file path */
#define METADATA_FILE "/opt/satdeploy/apps.json"
#define METADATA_DIR  "/opt/satdeploy"

/* Maximum number of apps to track */
#define MAX_APPS 64

/* App metadata entry */
typedef struct {
    char app_name[MAX_APP_NAME_LEN];
    char remote_path[MAX_PATH_LEN];
    char file_hash[HASH_BUF_LEN];
    char deployed_at[32];
} app_entry_t;

/* In-memory cache of app metadata */
static app_entry_t app_cache[MAX_APPS];
static int app_count = 0;
static int cache_loaded = 0;

/* Helper: ensure directory exists */
static int ensure_dir(const char *path) {
    struct stat st;
    if (stat(path, &st) == 0) {
        return S_ISDIR(st.st_mode) ? 0 : -1;
    }
    return mkdir(path, 0755);
}

/* Helper: skip whitespace in JSON */
static const char *skip_ws(const char *p) {
    while (*p && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) p++;
    return p;
}

/* Helper: parse quoted string, returns pointer after closing quote */
static const char *parse_string(const char *p, char *out, size_t out_size) {
    p = skip_ws(p);
    if (*p != '"') return NULL;
    p++;

    size_t i = 0;
    while (*p && *p != '"' && i < out_size - 1) {
        if (*p == '\\' && *(p + 1)) {
            p++;  /* Skip escape */
        }
        out[i++] = *p++;
    }
    out[i] = '\0';

    if (*p == '"') p++;
    return p;
}

/* Load metadata from JSON file */
static int load_metadata(void) {
    if (cache_loaded) return 0;

    app_count = 0;
    cache_loaded = 1;

    FILE *f = fopen(METADATA_FILE, "r");
    if (!f) {
        return 0;  /* File doesn't exist yet - that's OK */
    }

    /* Read entire file */
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);

    if (size <= 0 || size > 1024 * 1024) {
        fclose(f);
        return -1;
    }

    char *json = malloc(size + 1);
    if (!json) {
        fclose(f);
        return -1;
    }

    if (fread(json, 1, size, f) != (size_t)size) {
        free(json);
        fclose(f);
        return -1;
    }
    json[size] = '\0';
    fclose(f);

    /* Simple JSON parser for our specific format */
    const char *p = skip_ws(json);
    if (*p != '{') {
        free(json);
        return -1;
    }
    p++;

    while (*p && app_count < MAX_APPS) {
        p = skip_ws(p);
        if (*p == '}') break;
        if (*p == ',') { p++; continue; }

        /* Parse app name */
        app_entry_t *entry = &app_cache[app_count];
        memset(entry, 0, sizeof(*entry));

        p = parse_string(p, entry->app_name, sizeof(entry->app_name));
        if (!p) break;

        p = skip_ws(p);
        if (*p != ':') break;
        p++;

        p = skip_ws(p);
        if (*p != '{') break;
        p++;

        /* Parse fields */
        while (*p && *p != '}') {
            p = skip_ws(p);
            if (*p == ',') { p++; continue; }
            if (*p == '}') break;

            char key[64];
            p = parse_string(p, key, sizeof(key));
            if (!p) break;

            p = skip_ws(p);
            if (*p != ':') break;
            p++;

            char value[MAX_PATH_LEN];
            p = parse_string(p, value, sizeof(value));
            if (!p) break;

            if (strcmp(key, "remote_path") == 0) {
                strncpy(entry->remote_path, value, sizeof(entry->remote_path) - 1);
            } else if (strcmp(key, "file_hash") == 0) {
                strncpy(entry->file_hash, value, sizeof(entry->file_hash) - 1);
            } else if (strcmp(key, "deployed_at") == 0) {
                strncpy(entry->deployed_at, value, sizeof(entry->deployed_at) - 1);
            }
        }

        if (*p == '}') p++;
        app_count++;
    }

    free(json);
    return 0;
}

/* Save metadata to JSON file */
static int save_metadata(void) {
    if (ensure_dir(METADATA_DIR) != 0) {
        return -1;
    }

    FILE *f = fopen(METADATA_FILE, "w");
    if (!f) {
        return -1;
    }

    fprintf(f, "{\n");
    for (int i = 0; i < app_count; i++) {
        app_entry_t *e = &app_cache[i];
        fprintf(f, "  \"%s\": {\n", e->app_name);
        fprintf(f, "    \"remote_path\": \"%s\",\n", e->remote_path);
        fprintf(f, "    \"file_hash\": \"%s\",\n", e->file_hash);
        fprintf(f, "    \"deployed_at\": \"%s\"\n", e->deployed_at);
        fprintf(f, "  }%s\n", (i < app_count - 1) ? "," : "");
    }
    fprintf(f, "}\n");

    fclose(f);
    return 0;
}

/* Find app entry by name */
static app_entry_t *find_app(const char *app_name) {
    load_metadata();
    for (int i = 0; i < app_count; i++) {
        if (strcmp(app_cache[i].app_name, app_name) == 0) {
            return &app_cache[i];
        }
    }
    return NULL;
}

/* Public API */

int app_metadata_save(const char *app_name, const char *remote_path,
                      const char *file_hash) {
    load_metadata();

    app_entry_t *entry = find_app(app_name);
    if (!entry) {
        if (app_count >= MAX_APPS) {
            return -1;
        }
        entry = &app_cache[app_count++];
        memset(entry, 0, sizeof(*entry));
        strncpy(entry->app_name, app_name, sizeof(entry->app_name) - 1);
    }

    strncpy(entry->remote_path, remote_path, sizeof(entry->remote_path) - 1);
    strncpy(entry->file_hash, file_hash, sizeof(entry->file_hash) - 1);

    /* Generate ISO timestamp */
    time_t now = time(NULL);
    struct tm *tm = localtime(&now);
    snprintf(entry->deployed_at, sizeof(entry->deployed_at),
             "%04d-%02d-%02dT%02d:%02d:%02d",
             tm->tm_year + 1900, tm->tm_mon + 1, tm->tm_mday,
             tm->tm_hour, tm->tm_min, tm->tm_sec);

    return save_metadata();
}

int app_metadata_get(const char *app_name, char *remote_path, size_t path_size,
                     char *file_hash, size_t hash_size,
                     char *deployed_at, size_t time_size) {
    app_entry_t *entry = find_app(app_name);
    if (!entry) {
        return -1;
    }

    if (remote_path && path_size > 0) {
        strncpy(remote_path, entry->remote_path, path_size - 1);
        remote_path[path_size - 1] = '\0';
    }
    if (file_hash && hash_size > 0) {
        strncpy(file_hash, entry->file_hash, hash_size - 1);
        file_hash[hash_size - 1] = '\0';
    }
    if (deployed_at && time_size > 0) {
        strncpy(deployed_at, entry->deployed_at, time_size - 1);
        deployed_at[time_size - 1] = '\0';
    }

    return 0;
}

int app_metadata_list(app_metadata_callback callback, void *user_data) {
    load_metadata();

    for (int i = 0; i < app_count; i++) {
        app_entry_t *e = &app_cache[i];
        callback(e->app_name, e->remote_path, e->file_hash,
                 e->deployed_at, user_data);
    }

    return app_count;
}

void app_metadata_reload(void) {
    cache_loaded = 0;
    app_count = 0;
}

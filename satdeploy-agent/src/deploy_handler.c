/**
 * Deploy handler - CSP port 20 command handler
 *
 * Receives protobuf-encoded deploy commands and dispatches to
 * the appropriate handler (status, deploy, rollback, etc.)
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <dirent.h>
#include <sys/stat.h>
#include <libgen.h>

#include <csp/csp.h>

#include "satdeploy_agent.h"
#include "deploy.pb-c.h"

/* Maximum number of backups to return in list */
#define MAX_BACKUP_ENTRIES 64

/* Helper: ensure parent directory exists */
static int ensure_parent_dir(const char *path) {
    char *path_copy = strdup(path);
    if (!path_copy) return -1;

    char *dir = dirname(path_copy);
    int ret = mkdir_p(dir);  /* Uses shared mkdir_p from backup_manager.c */

    free(path_copy);
    return ret;
}

/* Chunk size for file transfers (must fit in CSP packet with overhead) */
#define CHUNK_SIZE 1400

/* Upload session state */
typedef struct {
    int active;
    char app_name[MAX_APP_NAME_LEN];
    char remote_path[MAX_PATH_LEN];
    char temp_path[MAX_PATH_LEN];
    char expected_checksum[16];
    uint32_t expected_size;
    uint32_t received_size;
    uint32_t next_chunk;
    uint32_t total_chunks;
    FILE *temp_file;
} upload_session_t;

static upload_session_t upload_session = {0};

/* Structure to collect backups during iteration */
typedef struct {
    Satdeploy__BackupEntry **entries;
    size_t count;
    size_t capacity;
} backup_collection_t;

/* Forward declarations */
static void handle_status(const Satdeploy__DeployRequest *req,
                          Satdeploy__DeployResponse *resp);
static void handle_verify(const Satdeploy__DeployRequest *req,
                          Satdeploy__DeployResponse *resp);
static void handle_list_versions(const Satdeploy__DeployRequest *req,
                                 Satdeploy__DeployResponse *resp);
static void handle_rollback(const Satdeploy__DeployRequest *req,
                            Satdeploy__DeployResponse *resp);
static void handle_deploy(const Satdeploy__DeployRequest *req,
                          Satdeploy__DeployResponse *resp);
static void handle_upload_start(const Satdeploy__DeployRequest *req,
                                Satdeploy__DeployResponse *resp);
static void handle_upload_chunk(const Satdeploy__DeployRequest *req,
                                Satdeploy__DeployResponse *resp);
static void handle_upload_end(const Satdeploy__DeployRequest *req,
                              Satdeploy__DeployResponse *resp);

/* Server socket for deploy connections */
static csp_socket_t deploy_socket = {0};

/**
 * Handle a single deploy connection.
 */
static void handle_connection(csp_conn_t *conn) {
    printf("[deploy] handle_connection called\n");
    fflush(stdout);

    csp_packet_t *packet = csp_read(conn, 10000);
    if (packet == NULL) {
        printf("[deploy] No data received on connection\n");
        fflush(stdout);
        return;
    }

    printf("[deploy] Received %u bytes\n", packet->length);
    fflush(stdout);

    /* Parse protobuf request */
    Satdeploy__DeployRequest *req = satdeploy__deploy_request__unpack(
        NULL, packet->length, packet->data);

    csp_buffer_free(packet);

    if (req == NULL) {
        printf("[deploy] Failed to parse protobuf request\n");
        return;
    }

    printf("[deploy] Command: %d, App: %s\n", req->command,
           req->app_name ? req->app_name : "(null)");
    fflush(stdout);

    /* Prepare response */
    Satdeploy__DeployResponse resp = SATDEPLOY__DEPLOY_RESPONSE__INIT;

    /* Dispatch to handler */
    switch (req->command) {
        case SATDEPLOY__DEPLOY_COMMAND__CMD_STATUS:
            handle_status(req, &resp);
            break;
        case SATDEPLOY__DEPLOY_COMMAND__CMD_VERIFY:
            handle_verify(req, &resp);
            break;
        case SATDEPLOY__DEPLOY_COMMAND__CMD_LIST_VERSIONS:
            handle_list_versions(req, &resp);
            break;
        case SATDEPLOY__DEPLOY_COMMAND__CMD_ROLLBACK:
            handle_rollback(req, &resp);
            break;
        case SATDEPLOY__DEPLOY_COMMAND__CMD_DEPLOY:
            handle_deploy(req, &resp);
            break;
        case SATDEPLOY__DEPLOY_COMMAND__CMD_UPLOAD_START:
            handle_upload_start(req, &resp);
            break;
        case SATDEPLOY__DEPLOY_COMMAND__CMD_UPLOAD_CHUNK:
            handle_upload_chunk(req, &resp);
            break;
        case SATDEPLOY__DEPLOY_COMMAND__CMD_UPLOAD_END:
            handle_upload_end(req, &resp);
            break;
        default:
            printf("[deploy] Unknown command: %d\n", req->command);
            resp.success = 0;
            resp.error_code = SATDEPLOY__DEPLOY_ERROR__ERR_UNKNOWN_COMMAND;
            resp.error_message = "Unknown command";
            break;
    }

    satdeploy__deploy_request__free_unpacked(req, NULL);

    /* Serialize and send response */
    size_t resp_size = satdeploy__deploy_response__get_packed_size(&resp);
    csp_packet_t *resp_packet = csp_buffer_get(resp_size);

    if (resp_packet != NULL) {
        resp_packet->length = satdeploy__deploy_response__pack(&resp, resp_packet->data);
        printf("[deploy] Sending response: %zu bytes, success=%d\n",
               resp_size, resp.success);
        fflush(stdout);
        csp_send(conn, resp_packet);
        printf("[deploy] Response sent\n");
        fflush(stdout);
    } else {
        printf("[deploy] Failed to allocate response buffer\n");
        fflush(stdout);
    }
}

int deploy_handler_init(void) {
    printf("[deploy] Initializing deploy handler on port %d\n", DEPLOY_PORT);

    /* Bind socket to deploy port */
    if (csp_bind(&deploy_socket, DEPLOY_PORT) != CSP_ERR_NONE) {
        printf("[deploy] Failed to bind to port %d\n", DEPLOY_PORT);
        return -1;
    }

    if (csp_listen(&deploy_socket, 10) != CSP_ERR_NONE) {
        printf("[deploy] Failed to listen on socket\n");
        return -1;
    }

    printf("[deploy] Listening on port %d\n", DEPLOY_PORT);
    return 0;
}

void deploy_handler_loop(void) {
    while (running) {
        csp_conn_t *conn = csp_accept(&deploy_socket, 1000);
        if (conn == NULL) {
            continue;
        }

        printf("[deploy] Accepted connection\n");
        handle_connection(conn);
        csp_close(conn);
    }
}

/* --- Command Handlers --- */

/* Callback context for status command */
typedef struct {
    Satdeploy__AppStatusEntry **entries;
    Satdeploy__AppStatusEntry *storage;
    int count;
    int max;
} status_context_t;

/* Callback for app_metadata_list */
static void status_metadata_callback(const char *app_name, const char *remote_path,
                                     const char *binary_hash, const char *deployed_at,
                                     void *user_data) {
    (void)deployed_at;
    (void)binary_hash;
    status_context_t *ctx = (status_context_t *)user_data;

    if (ctx->count >= ctx->max) return;

    /* Verify file actually exists - skip if missing */
    static char hash_buf[32][16];
    if (compute_file_checksum(remote_path, hash_buf[ctx->count], 16) != 0) {
        /* File missing or unreadable - don't include in status */
        printf("[deploy] Skipping %s: file missing at %s\n", app_name, remote_path);
        return;
    }

    Satdeploy__AppStatusEntry *app = &ctx->storage[ctx->count];
    satdeploy__app_status_entry__init(app);
    app->app_name = strdup(app_name);
    app->remote_path = strdup(remote_path);
    app->binary_hash = hash_buf[ctx->count];
    /* Check if process is running by matching the binary path.
       Use [/] trick to prevent pgrep from matching itself. */
    char cmd[320];
    snprintf(cmd, sizeof(cmd), "pgrep -f '[/]%s' > /dev/null 2>&1", remote_path + 1);
    app->running = (system(cmd) == 0) ? 1 : 0;

    ctx->entries[ctx->count] = app;
    ctx->count++;
}

static void handle_status(const Satdeploy__DeployRequest *req,
                          Satdeploy__DeployResponse *resp) {
    (void)req;
    printf("[deploy] STATUS command\n");

    static Satdeploy__AppStatusEntry *app_entries[32];
    static Satdeploy__AppStatusEntry app_storage[32];

    status_context_t ctx = {
        .entries = app_entries,
        .storage = app_storage,
        .count = 0,
        .max = 32
    };

    /* Get deployed apps from metadata */
    app_metadata_list(status_metadata_callback, &ctx);

    resp->success = 1;
    resp->n_apps = ctx.count;
    resp->apps = app_entries;

    printf("[deploy] Status: agent running, %d deployed apps\n", ctx.count);
}

static void handle_verify(const Satdeploy__DeployRequest *req,
                          Satdeploy__DeployResponse *resp) {
    printf("[deploy] VERIFY command for %s at %s\n",
           req->app_name ? req->app_name : "(null)",
           req->remote_path ? req->remote_path : "(null)");

    if (req->remote_path == NULL || strlen(req->remote_path) == 0) {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_APP_NOT_FOUND;
        resp->error_message = "No remote_path specified";
        return;
    }

    static char checksum[16];
    if (compute_file_checksum(req->remote_path, checksum, sizeof(checksum)) == 0) {
        resp->success = 1;
        resp->actual_checksum = checksum;
    } else {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_APP_NOT_FOUND;
        resp->error_message = "File not found or unreadable";
    }
}

/**
 * Callback for backup_list - collects entries into the collection.
 * Deduplicates by hash (old and new format files may have same hash).
 */
static void backup_collect_callback(const char *version, const char *timestamp,
                                    const char *hash, const char *path,
                                    void *user_data) {
    backup_collection_t *col = (backup_collection_t *)user_data;

    if (col->count >= col->capacity || !hash) {
        return;
    }

    /* Deduplicate: skip if this hash already exists */
    for (size_t i = 0; i < col->count; i++) {
        if (col->entries[i]->hash && strcmp(col->entries[i]->hash, hash) == 0) {
            return;  /* Already have this hash */
        }
    }

    /* Allocate and initialize entry */
    Satdeploy__BackupEntry *entry = malloc(sizeof(Satdeploy__BackupEntry));
    if (entry == NULL) {
        return;
    }

    satdeploy__backup_entry__init(entry);
    entry->version = strdup(version ? version : "");
    entry->timestamp = strdup(timestamp ? timestamp : "");
    entry->hash = strdup(hash ? hash : "");
    entry->path = strdup(path ? path : "");

    col->entries[col->count++] = entry;
}

/**
 * Free backup entries allocated during list.
 */
static void free_backup_entries(Satdeploy__BackupEntry **entries, size_t count) {
    for (size_t i = 0; i < count; i++) {
        if (entries[i]) {
            free(entries[i]->version);
            free(entries[i]->timestamp);
            free(entries[i]->hash);
            free(entries[i]->path);
            free(entries[i]);
        }
    }
    free(entries);
}

static void handle_list_versions(const Satdeploy__DeployRequest *req,
                                 Satdeploy__DeployResponse *resp) {
    printf("[deploy] LIST_VERSIONS command for %s\n",
           req->app_name ? req->app_name : "(null)");

    if (req->app_name == NULL || strlen(req->app_name) == 0) {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_APP_NOT_FOUND;
        resp->error_message = "No app_name specified";
        return;
    }

    /* Allocate collection for backups (+ 1 for current deployed) */
    backup_collection_t col = {
        .entries = malloc(sizeof(Satdeploy__BackupEntry *) * MAX_BACKUP_ENTRIES),
        .count = 0,
        .capacity = MAX_BACKUP_ENTRIES
    };

    if (col.entries == NULL) {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_BACKUP_FAILED;
        resp->error_message = "Memory allocation failed";
        return;
    }

    /* First, list backups */
    int result = backup_list(req->app_name, backup_collect_callback, &col);

    /* Get currently deployed version */
    char remote_path[MAX_PATH_LEN], binary_hash[16], deployed_at[32];
    char actual_hash[16] = {0};
    int have_current = 0;

    if (app_metadata_get(req->app_name, remote_path, sizeof(remote_path),
                         binary_hash, sizeof(binary_hash),
                         deployed_at, sizeof(deployed_at)) == 0) {
        /* Verify file actually exists */
        if (compute_file_checksum(remote_path, actual_hash, sizeof(actual_hash)) == 0) {
            have_current = 1;
        }
    }

    /* Check if current hash already exists in backups */
    int current_in_backups = 0;
    if (have_current) {
        for (size_t i = 0; i < col.count; i++) {
            if (col.entries[i]->hash && strcmp(col.entries[i]->hash, actual_hash) == 0) {
                /* Mark this backup as "current" */
                free(col.entries[i]->version);
                col.entries[i]->version = strdup("current");
                current_in_backups = 1;
                break;
            }
        }

        /* Only add separate "current" entry if not in backups */
        if (!current_in_backups) {
            Satdeploy__BackupEntry *current = malloc(sizeof(Satdeploy__BackupEntry));
            if (current) {
                satdeploy__backup_entry__init(current);
                current->version = strdup("current");
                current->timestamp = strdup(deployed_at);
                current->hash = strdup(actual_hash);
                current->path = strdup(remote_path);
                col.entries[col.count++] = current;
            }
        }
    }

    if (result < 0) {
        free_backup_entries(col.entries, col.count);
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_BACKUP_FAILED;
        resp->error_message = "Failed to list backups";
        return;
    }

    /* Sort entries by mtime descending (newest first) - matches dial order */
    for (size_t i = 0; i < col.count; i++) {
        for (size_t j = i + 1; j < col.count; j++) {
            struct stat st_i, st_j;
            time_t mtime_i = 0, mtime_j = 0;
            if (col.entries[i]->path && stat(col.entries[i]->path, &st_i) == 0)
                mtime_i = st_i.st_mtime;
            if (col.entries[j]->path && stat(col.entries[j]->path, &st_j) == 0)
                mtime_j = st_j.st_mtime;
            if (mtime_j > mtime_i) {
                /* Swap - newer should come first */
                Satdeploy__BackupEntry *tmp = col.entries[i];
                col.entries[i] = col.entries[j];
                col.entries[j] = tmp;
            }
        }
    }

    printf("[deploy] Found %zu versions for %s (newest first)\n", col.count, req->app_name);

    resp->success = 1;
    resp->n_backups = col.count;
    resp->backups = col.entries;

    /* Note: entries will be freed after response is serialized in deploy_callback */
}

/**
 * Rollback search state - collects all backups for dial or specific hash lookup.
 */
#define MAX_DIAL_ENTRIES 32

typedef struct {
    char hash[16];
    char path[MAX_PATH_LEN];
    time_t mtime;  /* File modification time for chronological ordering */
} backup_entry_t;

typedef struct {
    backup_entry_t entries[MAX_DIAL_ENTRIES];
    int entry_count;
} rollback_search_t;

static void rollback_collect_callback(const char *version, const char *timestamp,
                                      const char *hash, const char *path,
                                      void *user_data) {
    (void)version;
    (void)timestamp;
    rollback_search_t *search = (rollback_search_t *)user_data;

    if (search->entry_count >= MAX_DIAL_ENTRIES || !hash || !path) {
        return;
    }

    /* Deduplicate: skip if this hash already exists */
    for (int i = 0; i < search->entry_count; i++) {
        if (strcmp(search->entries[i].hash, hash) == 0) {
            return;
        }
    }

    backup_entry_t *entry = &search->entries[search->entry_count];
    strncpy(entry->hash, hash, sizeof(entry->hash) - 1);
    entry->hash[sizeof(entry->hash) - 1] = '\0';
    strncpy(entry->path, path, sizeof(entry->path) - 1);
    entry->path[sizeof(entry->path) - 1] = '\0';

    /* Get file mtime for proper chronological ordering */
    struct stat st;
    entry->mtime = (stat(path, &st) == 0) ? st.st_mtime : 0;

    search->entry_count++;
}

static void handle_rollback(const Satdeploy__DeployRequest *req,
                            Satdeploy__DeployResponse *resp) {
    printf("[deploy] ROLLBACK command for %s, hash=%s\n",
           req->app_name ? req->app_name : "(null)",
           req->rollback_hash ? req->rollback_hash : "(dial)");

    if (!req->app_name || !req->app_name[0]) {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_APP_NOT_FOUND;
        resp->error_message = "No app_name specified";
        return;
    }

    if (!req->remote_path || !req->remote_path[0]) {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_APP_NOT_FOUND;
        resp->error_message = "No remote_path specified";
        return;
    }

    /* Get current deployed hash */
    char current_hash[16] = {0};
    compute_file_checksum(req->remote_path, current_hash, sizeof(current_hash));
    printf("[deploy] Current deployed hash: %s\n", current_hash[0] ? current_hash : "(none)");

    /* Collect all backups (single filesystem traversal) */
    rollback_search_t search = {0};
    int count = backup_list(req->app_name, rollback_collect_callback, &search);

    if (count < 0) {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_BACKUP_FAILED;
        resp->error_message = "Failed to list backups";
        return;
    }

    if (search.entry_count == 0) {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_NO_BACKUPS;
        resp->error_message = "No backups available";
        return;
    }

    /* Find the backup to restore */
    const char *target_hash = (req->rollback_hash && req->rollback_hash[0])
                              ? req->rollback_hash : NULL;
    backup_entry_t *selected = NULL;

    if (target_hash) {
        /* Specific hash requested - find exact match */
        for (int i = 0; i < search.entry_count; i++) {
            if (strcmp(search.entries[i].hash, target_hash) == 0) {
                selected = &search.entries[i];
                break;
            }
        }
        if (!selected) {
            resp->success = 0;
            resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_BACKUP_NOT_FOUND;
            resp->error_message = "Backup with specified hash not found";
            return;
        }
    } else {
        /* Dial behavior: sort by mtime (newest first), pick next after current */

        /* Sort entries by mtime descending (newest first) */
        for (int i = 0; i < search.entry_count - 1; i++) {
            for (int j = i + 1; j < search.entry_count; j++) {
                if (search.entries[j].mtime > search.entries[i].mtime) {
                    backup_entry_t tmp = search.entries[i];
                    search.entries[i] = search.entries[j];
                    search.entries[j] = tmp;
                }
            }
        }

        printf("[deploy] Dial entries (by time, newest first): ");
        for (int i = 0; i < search.entry_count; i++) {
            printf("%s ", search.entries[i].hash);
        }
        printf("\n");

        /* Find current hash position */
        int current_idx = -1;
        for (int i = 0; i < search.entry_count; i++) {
            if (current_hash[0] && strcmp(search.entries[i].hash, current_hash) == 0) {
                current_idx = i;
                break;
            }
        }

        /* Pick next entry (wrap around) */
        int next_idx = (current_idx < 0) ? 0 : (current_idx + 1) % search.entry_count;
        selected = &search.entries[next_idx];

        printf("[deploy] Dial: current=%s idx=%d -> next idx=%d hash=%s\n",
               current_hash, current_idx, next_idx, selected->hash);
    }

    printf("[deploy] Restoring backup: %s -> %s\n", selected->path, req->remote_path);

    /* Backup current version if not already in backups (check in-memory, no second traversal) */
    if (current_hash[0]) {
        int current_in_backups = 0;
        for (int i = 0; i < search.entry_count; i++) {
            if (strcmp(search.entries[i].hash, current_hash) == 0) {
                current_in_backups = 1;
                break;
            }
        }

        if (!current_in_backups) {
            char backup_path[MAX_PATH_LEN];
            if (backup_create(req->app_name, req->remote_path, backup_path, sizeof(backup_path)) == 0) {
                printf("[deploy] Backed up current version (%s) to: %s\n", current_hash, backup_path);
            }
        } else {
            printf("[deploy] Current version (%s) already in backups\n", current_hash);
        }
    }

    /* Restore the backup */
    if (backup_restore(selected->path, req->remote_path) != 0) {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_RESTORE_FAILED;
        resp->error_message = "Failed to restore backup";
        return;
    }

    /* Update metadata - trust the hash we already know, don't recompute */
    app_metadata_save(req->app_name, req->remote_path, selected->hash);
    printf("[deploy] Updated metadata: %s -> %s\n", req->app_name, selected->hash);

    /* Return the backup path that was restored */
    static char restored_path[MAX_PATH_LEN];
    strncpy(restored_path, selected->path, sizeof(restored_path) - 1);
    restored_path[sizeof(restored_path) - 1] = '\0';

    resp->success = 1;
    resp->backup_path = restored_path;
}

static void handle_deploy(const Satdeploy__DeployRequest *req,
                          Satdeploy__DeployResponse *resp) {
    printf("[deploy] DEPLOY command for %s\n",
           req->app_name ? req->app_name : "(null)");
    printf("  remote_path: %s\n", req->remote_path ? req->remote_path : "(null)");
    printf("  dtp_server: node=%u port=%u payload=%u\n",
           req->dtp_server_node, req->dtp_server_port, req->payload_id);
    printf("  expected: size=%u checksum=%s\n",
           req->expected_size,
           req->expected_checksum ? req->expected_checksum : "(null)");

    /* Validate required fields */
    if (req->app_name == NULL || strlen(req->app_name) == 0) {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_APP_NOT_FOUND;
        resp->error_message = "No app_name specified";
        return;
    }

    if (req->remote_path == NULL || strlen(req->remote_path) == 0) {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_APP_NOT_FOUND;
        resp->error_message = "No remote_path specified";
        return;
    }

    if (req->dtp_server_node == 0) {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_DTP_DOWNLOAD_FAILED;
        resp->error_message = "No DTP server node specified";
        return;
    }

    /* Step 1: TODO - Stop app via libparam if running
       For now, we skip this step since libparam integration requires
       knowing the param_name and target node */
    printf("[deploy] Step 1: Skipping app stop (not implemented)\n");

    /* Step 2: Backup current binary if it exists */
    static char backup_path_buf[MAX_PATH_LEN];
    backup_path_buf[0] = '\0';

    if (access(req->remote_path, F_OK) == 0) {
        printf("[deploy] Step 2: Creating backup of %s\n", req->remote_path);
        if (backup_create(req->app_name, req->remote_path,
                          backup_path_buf, sizeof(backup_path_buf)) != 0) {
            resp->success = 0;
            resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_BACKUP_FAILED;
            resp->error_message = "Failed to backup current binary";
            return;
        }
        printf("[deploy] Backup created: %s\n", backup_path_buf);
    } else {
        printf("[deploy] Step 2: No existing binary to backup\n");
    }

    /* Step 3: Download new binary via DTP */
    printf("[deploy] Step 3: Downloading new binary via DTP\n");

    /* Download to a temp file first */
    char temp_path[MAX_PATH_LEN];
    snprintf(temp_path, sizeof(temp_path), "%s.tmp", req->remote_path);

    if (dtp_download_file(req->dtp_server_node, req->payload_id,
                          temp_path, req->expected_size) != 0) {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_DTP_DOWNLOAD_FAILED;
        resp->error_message = "DTP download failed";
        /* TODO: Restore from backup if we had one */
        return;
    }

    /* Step 4: Verify checksum */
    if (req->expected_checksum != NULL && strlen(req->expected_checksum) > 0) {
        printf("[deploy] Step 4: Verifying checksum\n");
        static char actual_checksum[16];
        if (compute_file_checksum(temp_path, actual_checksum, sizeof(actual_checksum)) != 0) {
            resp->success = 0;
            resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_CHECKSUM_MISMATCH;
            resp->error_message = "Failed to compute checksum";
            unlink(temp_path);
            return;
        }

        if (strcmp(actual_checksum, req->expected_checksum) != 0) {
            printf("[deploy] Checksum mismatch: expected=%s, actual=%s\n",
                   req->expected_checksum, actual_checksum);
            resp->success = 0;
            resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_CHECKSUM_MISMATCH;
            resp->error_message = "Checksum mismatch";
            unlink(temp_path);
            return;
        }
        printf("[deploy] Checksum verified: %s\n", actual_checksum);
    } else {
        printf("[deploy] Step 4: Skipping checksum verification (none provided)\n");
    }

    /* Step 5: Install binary (move temp to final location) */
    printf("[deploy] Step 5: Installing binary to %s\n", req->remote_path);
    if (rename(temp_path, req->remote_path) != 0) {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_INSTALL_FAILED;
        resp->error_message = "Failed to install binary";
        unlink(temp_path);
        return;
    }

    /* Make executable */
    chmod(req->remote_path, 0755);

    /* Create backup of newly deployed version to preserve deploy timestamp */
    backup_create(req->app_name, req->remote_path, NULL, 0);

    /* Step 6: TODO - Start app via libparam
       For now, we skip this step */
    printf("[deploy] Step 6: Skipping app start (not implemented)\n");

    /* Success */
    resp->success = 1;
    if (backup_path_buf[0] != '\0') {
        resp->backup_path = backup_path_buf;
    }
    printf("[deploy] Deploy complete!\n");
}

/* --- Direct Upload Handlers --- */

static void upload_session_reset(void) {
    if (upload_session.temp_file) {
        fclose(upload_session.temp_file);
        upload_session.temp_file = NULL;
    }
    if (upload_session.temp_path[0]) {
        unlink(upload_session.temp_path);
    }
    memset(&upload_session, 0, sizeof(upload_session));
}

static void handle_upload_start(const Satdeploy__DeployRequest *req,
                                Satdeploy__DeployResponse *resp) {
    printf("[deploy] UPLOAD_START for %s\n",
           req->app_name ? req->app_name : "(null)");
    printf("  remote_path: %s\n", req->remote_path ? req->remote_path : "(null)");
    printf("  expected: size=%u checksum=%s chunks=%u\n",
           req->expected_size,
           req->expected_checksum ? req->expected_checksum : "(null)",
           req->total_chunks);

    /* Abort any existing upload */
    if (upload_session.active) {
        printf("[deploy] Aborting previous upload session\n");
        upload_session_reset();
    }

    /* Validate required fields */
    if (!req->app_name || strlen(req->app_name) == 0) {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_APP_NOT_FOUND;
        resp->error_message = "No app_name specified";
        return;
    }

    if (!req->remote_path || strlen(req->remote_path) == 0) {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_APP_NOT_FOUND;
        resp->error_message = "No remote_path specified";
        return;
    }

    /* Initialize upload session */
    upload_session.active = 1;
    strncpy(upload_session.app_name, req->app_name, MAX_APP_NAME_LEN - 1);
    strncpy(upload_session.remote_path, req->remote_path, MAX_PATH_LEN - 1);
    snprintf(upload_session.temp_path, MAX_PATH_LEN, "/tmp/satdeploy-%s.tmp", req->app_name);

    if (req->expected_checksum) {
        strncpy(upload_session.expected_checksum, req->expected_checksum,
                sizeof(upload_session.expected_checksum) - 1);
    }
    upload_session.expected_size = req->expected_size;
    upload_session.total_chunks = req->total_chunks;
    upload_session.received_size = 0;
    upload_session.next_chunk = 0;

    /* Open temp file for writing */
    upload_session.temp_file = fopen(upload_session.temp_path, "wb");
    if (!upload_session.temp_file) {
        printf("[deploy] Failed to open temp file: %s\n", upload_session.temp_path);
        upload_session_reset();
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_FILE_WRITE_FAILED;
        resp->error_message = "Failed to create temp file";
        return;
    }

    printf("[deploy] Upload session started, expecting %u chunks\n", req->total_chunks);
    resp->success = 1;
}

static void handle_upload_chunk(const Satdeploy__DeployRequest *req,
                                Satdeploy__DeployResponse *resp) {
    printf("[deploy] UPLOAD_CHUNK seq=%u/%u, %zu bytes\n",
           req->chunk_seq, upload_session.total_chunks,
           req->chunk_data.len);

    if (!upload_session.active) {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_NO_UPLOAD_IN_PROGRESS;
        resp->error_message = "No upload in progress";
        return;
    }

    if (req->chunk_seq != upload_session.next_chunk) {
        printf("[deploy] Chunk out of order: expected %u, got %u\n",
               upload_session.next_chunk, req->chunk_seq);
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_CHUNK_OUT_OF_ORDER;
        resp->error_message = "Chunk out of order";
        return;
    }

    if (req->chunk_data.len == 0 || req->chunk_data.data == NULL) {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_FILE_WRITE_FAILED;
        resp->error_message = "Empty chunk data";
        return;
    }

    /* Write chunk to temp file */
    size_t written = fwrite(req->chunk_data.data, 1, req->chunk_data.len,
                            upload_session.temp_file);
    if (written != req->chunk_data.len) {
        printf("[deploy] Write failed: %zu of %zu bytes\n", written, req->chunk_data.len);
        upload_session_reset();
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_FILE_WRITE_FAILED;
        resp->error_message = "Failed to write chunk";
        return;
    }

    upload_session.received_size += req->chunk_data.len;
    upload_session.next_chunk++;

    resp->success = 1;
}

static void handle_upload_end(const Satdeploy__DeployRequest *req,
                              Satdeploy__DeployResponse *resp) {
    (void)req;
    printf("[deploy] UPLOAD_END - received %u bytes\n", upload_session.received_size);

    if (!upload_session.active) {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_NO_UPLOAD_IN_PROGRESS;
        resp->error_message = "No upload in progress";
        return;
    }

    /* Close temp file */
    fclose(upload_session.temp_file);
    upload_session.temp_file = NULL;

    /* Verify size */
    if (upload_session.expected_size > 0 &&
        upload_session.received_size != upload_session.expected_size) {
        printf("[deploy] Size mismatch: expected %u, got %u\n",
               upload_session.expected_size, upload_session.received_size);
        upload_session_reset();
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_CHECKSUM_MISMATCH;
        resp->error_message = "Size mismatch";
        return;
    }

    /* Verify checksum */
    if (upload_session.expected_checksum[0]) {
        static char actual_checksum[16];
        if (compute_file_checksum(upload_session.temp_path, actual_checksum,
                                  sizeof(actual_checksum)) != 0) {
            upload_session_reset();
            resp->success = 0;
            resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_CHECKSUM_MISMATCH;
            resp->error_message = "Failed to compute checksum";
            return;
        }

        if (strcmp(actual_checksum, upload_session.expected_checksum) != 0) {
            printf("[deploy] Checksum mismatch: expected=%s, actual=%s\n",
                   upload_session.expected_checksum, actual_checksum);
            upload_session_reset();
            resp->success = 0;
            resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_CHECKSUM_MISMATCH;
            resp->error_message = "Checksum mismatch";
            return;
        }
        printf("[deploy] Checksum verified: %s\n", actual_checksum);
    }

    /* Backup existing binary if present */
    static char backup_path_buf[MAX_PATH_LEN];
    backup_path_buf[0] = '\0';

    if (access(upload_session.remote_path, F_OK) == 0) {
        printf("[deploy] Creating backup of %s\n", upload_session.remote_path);
        if (backup_create(upload_session.app_name, upload_session.remote_path,
                          backup_path_buf, sizeof(backup_path_buf)) != 0) {
            upload_session_reset();
            resp->success = 0;
            resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_BACKUP_FAILED;
            resp->error_message = "Failed to backup current binary";
            return;
        }
        printf("[deploy] Backup created: %s\n", backup_path_buf);
    }

    /* Ensure parent directory exists */
    if (ensure_parent_dir(upload_session.remote_path) != 0) {
        printf("[deploy] Failed to create parent directory\n");
        upload_session_reset();
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_INSTALL_FAILED;
        resp->error_message = "Failed to create directory";
        return;
    }

    /* Install binary (copy temp to final location, handles cross-filesystem) */
    printf("[deploy] Installing binary to %s\n", upload_session.remote_path);
    if (copy_file(upload_session.temp_path, upload_session.remote_path) != 0) {
        upload_session_reset();
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_INSTALL_FAILED;
        resp->error_message = "Failed to install binary";
        return;
    }
    unlink(upload_session.temp_path);  /* Clean up temp file */

    /* Make executable */
    chmod(upload_session.remote_path, 0755);

    /* Create backup of newly deployed version to preserve deploy timestamp */
    backup_create(upload_session.app_name, upload_session.remote_path, NULL, 0);

    /* Save app metadata for status/list queries */
    if (app_metadata_save(upload_session.app_name, upload_session.remote_path,
                          upload_session.expected_checksum) != 0) {
        printf("[deploy] Warning: Failed to save app metadata\n");
    }

    /* Clear session (but don't delete files) */
    upload_session.active = 0;
    upload_session.temp_path[0] = '\0';

    /* Success */
    resp->success = 1;
    if (backup_path_buf[0]) {
        resp->backup_path = backup_path_buf;
    }
    printf("[deploy] Direct upload deploy complete!\n");
}

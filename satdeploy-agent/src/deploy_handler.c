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
#include <sys/stat.h>

#include <csp/csp.h>

#include "satdeploy_agent.h"
#include "deploy.pb-c.h"

/* Maximum number of backups to return in list */
#define MAX_BACKUP_ENTRIES 64

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

/**
 * CSP callback for deploy port.
 */
static void deploy_callback(csp_packet_t *packet) {
    if (packet == NULL || packet->length == 0) {
        printf("[deploy] Empty packet received\n");
        if (packet) csp_buffer_free(packet);
        return;
    }

    printf("[deploy] Received %u bytes\n", packet->length);

    /* Parse protobuf request */
    Satdeploy__DeployRequest *req = satdeploy__deploy_request__unpack(
        NULL, packet->length, packet->data);

    if (req == NULL) {
        printf("[deploy] Failed to parse protobuf request\n");
        csp_buffer_free(packet);
        return;
    }

    printf("[deploy] Command: %d, App: %s\n", req->command,
           req->app_name ? req->app_name : "(null)");

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
        default:
            printf("[deploy] Unknown command: %d\n", req->command);
            resp.success = 0;
            resp.error_code = SATDEPLOY__DEPLOY_ERROR__ERR_UNKNOWN_COMMAND;
            resp.error_message = "Unknown command";
            break;
    }

    /* Serialize response */
    size_t resp_size = satdeploy__deploy_response__get_packed_size(&resp);
    csp_packet_t *resp_packet = csp_buffer_get(resp_size);

    if (resp_packet != NULL) {
        resp_packet->length = satdeploy__deploy_response__pack(&resp, resp_packet->data);

        /* Send response back */
        /* Note: For connectionless, we'd need the source address.
           For now, we use the default route. */
        printf("[deploy] Sending response: %zu bytes, success=%d\n",
               resp_size, resp.success);

        /* TODO: Send response via CSP */
        csp_buffer_free(resp_packet);
    }

    /* Cleanup */
    satdeploy__deploy_request__free_unpacked(req, NULL);
    csp_buffer_free(packet);
}

int deploy_handler_init(void) {
    printf("[deploy] Initializing deploy handler on port %d\n", DEPLOY_PORT);

    /* Bind callback to deploy port */
    csp_bind_callback(deploy_callback, DEPLOY_PORT);

    return 0;
}

/* --- Command Handlers --- */

static void handle_status(const Satdeploy__DeployRequest *req,
                          Satdeploy__DeployResponse *resp) {
    (void)req;
    printf("[deploy] STATUS command\n");

    /* For now, return success with no apps.
       Full implementation would query libparam for running apps
       and check binary checksums. */
    resp->success = 1;
    resp->n_apps = 0;
    resp->apps = NULL;

    printf("[deploy] Status: agent running, no apps registered\n");
}

static void handle_verify(const Satdeploy__DeployRequest *req,
                          Satdeploy__DeployResponse *resp) {
    printf("[deploy] VERIFY command for %s at %s\n",
           req->app_name ? req->app_name : "(null)",
           req->remote_path ? req->remote_path : "(null)");

    if (req->remote_path == NULL) {
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
 */
static void backup_collect_callback(const char *version, const char *timestamp,
                                    const char *hash, const char *path,
                                    void *user_data) {
    backup_collection_t *col = (backup_collection_t *)user_data;

    if (col->count >= col->capacity) {
        return;  /* At capacity */
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

    /* Allocate collection for backups */
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

    /* List backups */
    int result = backup_list(req->app_name, backup_collect_callback, &col);

    if (result < 0) {
        free_backup_entries(col.entries, col.count);
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_BACKUP_FAILED;
        resp->error_message = "Failed to list backups";
        return;
    }

    printf("[deploy] Found %zu backups for %s\n", col.count, req->app_name);

    resp->success = 1;
    resp->n_backups = col.count;
    resp->backups = col.entries;

    /* Note: entries will be freed after response is serialized in deploy_callback */
}

/**
 * Callback to find a specific or most recent backup.
 */
typedef struct {
    const char *target_hash;  /* NULL means find most recent */
    char found_path[MAX_PATH_LEN];
    int found;
} rollback_search_t;

static void rollback_search_callback(const char *version, const char *timestamp,
                                     const char *hash, const char *path,
                                     void *user_data) {
    (void)version;
    (void)timestamp;
    rollback_search_t *search = (rollback_search_t *)user_data;

    if (search->target_hash != NULL) {
        /* Looking for specific hash */
        if (hash != NULL && strcmp(hash, search->target_hash) == 0) {
            strncpy(search->found_path, path, MAX_PATH_LEN - 1);
            search->found_path[MAX_PATH_LEN - 1] = '\0';
            search->found = 1;
        }
    } else {
        /* Looking for most recent - just take any (backup_list returns sorted) */
        if (!search->found && path != NULL) {
            strncpy(search->found_path, path, MAX_PATH_LEN - 1);
            search->found_path[MAX_PATH_LEN - 1] = '\0';
            search->found = 1;
        }
    }
}

static void handle_rollback(const Satdeploy__DeployRequest *req,
                            Satdeploy__DeployResponse *resp) {
    printf("[deploy] ROLLBACK command for %s, hash=%s\n",
           req->app_name ? req->app_name : "(null)",
           req->rollback_hash ? req->rollback_hash : "(latest)");

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

    /* Search for backup to restore */
    rollback_search_t search = {
        .target_hash = (req->rollback_hash && strlen(req->rollback_hash) > 0)
                       ? req->rollback_hash : NULL,
        .found_path = {0},
        .found = 0
    };

    int count = backup_list(req->app_name, rollback_search_callback, &search);

    if (count < 0) {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_BACKUP_FAILED;
        resp->error_message = "Failed to list backups";
        return;
    }

    if (count == 0 || !search.found) {
        resp->success = 0;
        if (search.target_hash != NULL) {
            resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_BACKUP_NOT_FOUND;
            resp->error_message = "Backup with specified hash not found";
        } else {
            resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_NO_BACKUPS;
            resp->error_message = "No backups available";
        }
        return;
    }

    printf("[deploy] Restoring backup: %s -> %s\n", search.found_path, req->remote_path);

    /* Restore the backup */
    if (backup_restore(search.found_path, req->remote_path) != 0) {
        resp->success = 0;
        resp->error_code = SATDEPLOY__DEPLOY_ERROR__ERR_RESTORE_FAILED;
        resp->error_message = "Failed to restore backup";
        return;
    }

    /* Return the backup path that was restored */
    static char restored_path[MAX_PATH_LEN];
    strncpy(restored_path, search.found_path, sizeof(restored_path) - 1);
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

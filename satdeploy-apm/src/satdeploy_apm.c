/*
 * satdeploy APM - Slash commands for satellite binary deployment
 *
 * Provides commands to interact with satdeploy-agent running on target:
 *   satdeploy status  - Query agent status
 *   satdeploy deploy  - Deploy a binary
 *   satdeploy rollback - Rollback to previous version
 *   satdeploy list    - List available backups
 *   satdeploy verify  - Verify binary checksum
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/stat.h>

#include <slash/slash.h>
#include <slash/optparse.h>
#include <csp/csp.h>
#include <apm/csh_api.h>

#include "deploy.pb-c.h"
#include "config.h"
#include "output.h"

#define SATDEPLOY_PORT 20
#define DEFAULT_TIMEOUT 10000

/*
 * File utilities for computing size and checksum
 */

static int get_file_size(const char *path, uint32_t *size_out)
{
    struct stat st;
    if (stat(path, &st) < 0) {
        return -1;
    }
    *size_out = (uint32_t)st.st_size;
    return 0;
}

static int compute_checksum(const char *path, char *hash_out, size_t hash_size)
{
    if (hash_size < 9) {
        return -1;
    }

    FILE *f = fopen(path, "rb");
    if (!f) {
        return -1;
    }

    /* FNV-1a hash - must match agent's backup_manager.c */
    uint32_t h = 0x811c9dc5;
    unsigned char buf[4096];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0) {
        for (size_t i = 0; i < n; i++) {
            h ^= buf[i];
            h *= 0x01000193;
        }
    }
    fclose(f);

    snprintf(hash_out, hash_size, "%08x", h);
    return 0;
}

static int send_deploy_request(unsigned int node, Satdeploy__DeployRequest *req,
                               Satdeploy__DeployResponse **resp_out)
{
    size_t req_size = satdeploy__deploy_request__get_packed_size(req);
    uint8_t *req_buf = malloc(req_size);
    if (!req_buf) {
        printf("Failed to allocate request buffer\n");
        return -1;
    }
    satdeploy__deploy_request__pack(req, req_buf);

    /* Allocate response buffer - use a reasonable max size */
    uint8_t resp_buf[4096];

    int resp_len = csp_transaction_w_opts(CSP_PRIO_NORM, node, SATDEPLOY_PORT,
                                          DEFAULT_TIMEOUT, req_buf, req_size,
                                          resp_buf, -1,  /* -1 = unknown reply size */
                                          CSP_O_CRC32);
    free(req_buf);

    if (resp_len <= 0) {
        printf("No response from agent (timeout or error)\n");
        return -1;
    }

    *resp_out = satdeploy__deploy_response__unpack(NULL, resp_len, resp_buf);
    if (!*resp_out) {
        printf("Failed to parse response\n");
        return -1;
    }

    return 0;
}

static int satdeploy_status_cmd(struct slash *slash)
{
    unsigned int node = 0;

    optparse_t *parser = optparse_new("satdeploy status", "[-n node]");
    optparse_add_help(parser);
    optparse_add_unsigned(parser, 'n', "node", "NUM", 0, &node, "Target node (default: from config)");

    int argi = optparse_parse(parser, slash->argc - 1, (const char **)slash->argv + 1);
    if (argi < 0) {
        optparse_del(parser);
        return SLASH_EINVAL;
    }
    optparse_del(parser);

    /* Use config default if -n not specified */
    if (node == 0) {
        satdeploy_config_t *config = satdeploy_config_load();
        if (config && config->target_node != 0) {
            node = config->target_node;
        } else {
            node = slash_dfl_node;
        }
    }

    Satdeploy__DeployRequest req = SATDEPLOY__DEPLOY_REQUEST__INIT;
    req.command = SATDEPLOY__DEPLOY_COMMAND__CMD_STATUS;

    Satdeploy__DeployResponse *resp = NULL;
    if (send_deploy_request(node, &req, &resp) < 0) {
        return SLASH_EIO;
    }

    if (!resp->success) {
        output_error(resp->error_message);
        satdeploy__deploy_response__free_unpacked(resp, NULL);
        return SLASH_EIO;
    }

    /* Print formatted status table */
    printf("Target: node %u\n\n", node);

    if (resp->n_apps == 0) {
        printf("No apps deployed.\n");
        satdeploy__deploy_response__free_unpacked(resp, NULL);
        return SLASH_SUCCESS;
    }

    output_status_header();
    output_separator(60);

    for (size_t i = 0; i < resp->n_apps; i++) {
        Satdeploy__AppStatusEntry *app = resp->apps[i];
        const char *status = app->running ? "running" : "stopped";
        int has_service = 1;  /* Assume all apps have services for now */

        output_status_row(
            app->app_name,
            status,
            app->binary_hash,
            app->remote_path,
            app->running,
            has_service
        );
    }

    satdeploy__deploy_response__free_unpacked(resp, NULL);
    return SLASH_SUCCESS;
}

static int satdeploy_deploy_cmd(struct slash *slash)
{
    unsigned int node = 0;
    char *app_name = NULL;
    char *local_path = NULL;
    char *remote_path = NULL;

    optparse_t *parser = optparse_new("satdeploy deploy", "<app_name>");
    optparse_add_help(parser);
    optparse_add_unsigned(parser, 'n', "node", "NUM", 0, &node, "Target node (default: from config)");
    optparse_add_string(parser, 'f', "file", "PATH", &local_path, "Local binary path (overrides config)");
    optparse_add_string(parser, 'r', "remote", "PATH", &remote_path, "Remote installation path");

    int argi = optparse_parse(parser, slash->argc - 1, (const char **)slash->argv + 1);
    if (argi < 0) {
        optparse_del(parser);
        return SLASH_EINVAL;
    }

    if (argi >= slash->argc - 1) {
        printf("Error: app_name required\n");
        optparse_help(parser, stdout);
        optparse_del(parser);
        return SLASH_EUSAGE;
    }
    app_name = slash->argv[argi + 1];
    optparse_del(parser);

    /* Load config for defaults */
    satdeploy_config_t *config = satdeploy_config_load();

    /* Use config default if -n not specified */
    if (node == 0) {
        if (config && config->target_node != 0) {
            node = config->target_node;
        } else {
            node = slash_dfl_node;
        }
    }

    /* Look up app-specific config */
    satdeploy_app_config_t *app_config = NULL;
    if (config) {
        app_config = satdeploy_config_get_app(config, app_name);
    }

    /* Apply app-specific defaults (CLI args override) */
    if (app_config) {
        if (!local_path && app_config->local_path[0]) {
            local_path = app_config->local_path;
        }
        if (!remote_path && app_config->remote_path[0]) {
            remote_path = app_config->remote_path;
        }
    }

    /* Validate required fields */
    if (!local_path) {
        printf("Error: No local file specified\n");
        printf("       Use -f <path> or set 'local' in config for app '%s'\n", app_name);
        return SLASH_EUSAGE;
    }

    if (!remote_path) {
        printf("Error: No remote path specified\n");
        printf("       Use -r <path> or set 'remote' in config for app '%s'\n", app_name);
        return SLASH_EUSAGE;
    }

    /* Auto-compute size and checksum from local file */
    uint32_t file_size = 0;
    char checksum[16] = {0};

    if (get_file_size(local_path, &file_size) < 0) {
        printf("Error: Cannot read file '%s'\n", local_path);
        return SLASH_EIO;
    }

    if (compute_checksum(local_path, checksum, sizeof(checksum)) < 0) {
        printf("Error: Cannot compute checksum for '%s'\n", local_path);
        return SLASH_EIO;
    }

    /* Calculate number of chunks needed */
    #define CHUNK_SIZE 1400
    uint32_t total_chunks = (file_size + CHUNK_SIZE - 1) / CHUNK_SIZE;
    if (total_chunks == 0) total_chunks = 1;

    printf("Deploying %s:\n", app_name);
    printf("  Local:    %s\n", local_path);
    printf("  Remote:   %s\n", remote_path);
    printf("  Size:     %u bytes (%u chunks)\n", file_size, total_chunks);
    printf("  Checksum: %s\n", checksum);
    printf("  Target:   node %u\n", node);

    /* Open local file for reading */
    FILE *f = fopen(local_path, "rb");
    if (!f) {
        printf("Error: Cannot open file '%s'\n", local_path);
        return SLASH_EIO;
    }

    /* Step 1: Send UPLOAD_START */
    printf("Sending UPLOAD_START to node %u...\n", node);
    Satdeploy__DeployRequest start_req = SATDEPLOY__DEPLOY_REQUEST__INIT;
    start_req.command = SATDEPLOY__DEPLOY_COMMAND__CMD_UPLOAD_START;
    start_req.app_name = app_name;
    start_req.remote_path = remote_path;
    start_req.expected_size = file_size;
    start_req.expected_checksum = checksum;
    start_req.total_chunks = total_chunks;

    Satdeploy__DeployResponse *resp = NULL;
    if (send_deploy_request(node, &start_req, &resp) < 0) {
        fclose(f);
        return SLASH_EIO;
    }

    if (!resp->success) {
        printf("UPLOAD_START failed: %s\n", resp->error_message);
        satdeploy__deploy_response__free_unpacked(resp, NULL);
        fclose(f);
        return SLASH_EIO;
    }
    satdeploy__deploy_response__free_unpacked(resp, NULL);
    resp = NULL;

    /* Step 2: Send file chunks */
    uint8_t chunk_buf[CHUNK_SIZE];
    uint32_t chunk_seq = 0;
    size_t bytes_sent = 0;

    printf("Uploading: ");
    fflush(stdout);

    while (!feof(f)) {
        size_t n = fread(chunk_buf, 1, CHUNK_SIZE, f);
        if (n == 0) break;

        Satdeploy__DeployRequest chunk_req = SATDEPLOY__DEPLOY_REQUEST__INIT;
        chunk_req.command = SATDEPLOY__DEPLOY_COMMAND__CMD_UPLOAD_CHUNK;
        chunk_req.chunk_seq = chunk_seq;
        chunk_req.chunk_data.data = chunk_buf;
        chunk_req.chunk_data.len = n;

        if (send_deploy_request(node, &chunk_req, &resp) < 0) {
            printf("\nChunk %u failed: no response\n", chunk_seq);
            fclose(f);
            return SLASH_EIO;
        }

        if (!resp->success) {
            printf("\nChunk %u failed: %s\n", chunk_seq, resp->error_message);
            satdeploy__deploy_response__free_unpacked(resp, NULL);
            fclose(f);
            return SLASH_EIO;
        }
        satdeploy__deploy_response__free_unpacked(resp, NULL);
        resp = NULL;

        bytes_sent += n;
        chunk_seq++;

        /* Progress indicator */
        printf(".");
        fflush(stdout);
    }
    fclose(f);
    printf(" done (%zu bytes)\n", bytes_sent);

    /* Step 3: Send UPLOAD_END */
    printf("Sending UPLOAD_END...\n");
    Satdeploy__DeployRequest end_req = SATDEPLOY__DEPLOY_REQUEST__INIT;
    end_req.command = SATDEPLOY__DEPLOY_COMMAND__CMD_UPLOAD_END;

    if (send_deploy_request(node, &end_req, &resp) < 0) {
        return SLASH_EIO;
    }

    if (!resp->success) {
        output_error(resp->error_message);
        satdeploy__deploy_response__free_unpacked(resp, NULL);
        return SLASH_EIO;
    }

    char success_msg[256];
    snprintf(success_msg, sizeof(success_msg), "Deployed %s (%s)", app_name, checksum);
    output_success(success_msg);

    satdeploy__deploy_response__free_unpacked(resp, NULL);
    return SLASH_SUCCESS;
}

static int satdeploy_rollback_cmd(struct slash *slash)
{
    unsigned int node = 0;
    char *app_name = NULL;
    char *hash = NULL;

    optparse_t *parser = optparse_new("satdeploy rollback", "<app_name>");
    optparse_add_help(parser);
    optparse_add_unsigned(parser, 'n', "node", "NUM", 0, &node, "Target node (default: from config)");
    optparse_add_string(parser, 'H', "hash", "HASH", &hash, "Specific backup hash to restore");

    int argi = optparse_parse(parser, slash->argc - 1, (const char **)slash->argv + 1);
    if (argi < 0) {
        optparse_del(parser);
        return SLASH_EINVAL;
    }

    if (argi >= slash->argc - 1) {
        printf("Error: app_name required\n");
        optparse_help(parser, stdout);
        optparse_del(parser);
        return SLASH_EUSAGE;
    }
    app_name = slash->argv[argi + 1];
    optparse_del(parser);

    /* Load config for defaults */
    satdeploy_config_t *config = satdeploy_config_load();

    /* Use config default if -n not specified */
    if (node == 0) {
        if (config && config->target_node != 0) {
            node = config->target_node;
        } else {
            node = slash_dfl_node;
        }
    }

    /* Look up remote_path from config */
    char *remote_path = NULL;
    if (config) {
        satdeploy_app_config_t *app_config = satdeploy_config_get_app(config, app_name);
        if (app_config) {
            remote_path = app_config->remote_path;
        }
    }

    if (!remote_path || !remote_path[0]) {
        char errmsg[256];
        snprintf(errmsg, sizeof(errmsg), "No remote_path configured for '%s'", app_name);
        output_error(errmsg);
        printf("Add it to ~/.satdeploy/config.yaml under apps/%s/remote\n", app_name);
        return SLASH_EINVAL;
    }

    Satdeploy__DeployRequest req = SATDEPLOY__DEPLOY_REQUEST__INIT;
    req.command = SATDEPLOY__DEPLOY_COMMAND__CMD_ROLLBACK;
    req.app_name = app_name;
    req.remote_path = remote_path ? remote_path : "";
    req.rollback_hash = hash ? hash : "";

    printf("Rolling back %s on node %u...\n", app_name, node);

    Satdeploy__DeployResponse *resp = NULL;
    if (send_deploy_request(node, &req, &resp) < 0) {
        return SLASH_EIO;
    }

    if (!resp->success) {
        output_error(resp->error_message);
        satdeploy__deploy_response__free_unpacked(resp, NULL);
        return SLASH_EIO;
    }

    char success_msg[256];
    snprintf(success_msg, sizeof(success_msg), "Rolled back %s", app_name);
    output_success(success_msg);
    satdeploy__deploy_response__free_unpacked(resp, NULL);
    return SLASH_SUCCESS;
}

static int satdeploy_list_cmd(struct slash *slash)
{
    unsigned int node = 0;
    char *app_name = NULL;

    optparse_t *parser = optparse_new("satdeploy list", "<app_name>");
    optparse_add_help(parser);
    optparse_add_unsigned(parser, 'n', "node", "NUM", 0, &node, "Target node (default: from config)");

    int argi = optparse_parse(parser, slash->argc - 1, (const char **)slash->argv + 1);
    if (argi < 0) {
        optparse_del(parser);
        return SLASH_EINVAL;
    }

    if (argi >= slash->argc - 1) {
        printf("Error: app_name required\n");
        optparse_help(parser, stdout);
        optparse_del(parser);
        return SLASH_EUSAGE;
    }
    app_name = slash->argv[argi + 1];
    optparse_del(parser);

    /* Use config default if -n not specified */
    if (node == 0) {
        satdeploy_config_t *config = satdeploy_config_load();
        if (config && config->target_node != 0) {
            node = config->target_node;
        } else {
            node = slash_dfl_node;
        }
    }

    /* First, query status to get currently deployed hash */
    Satdeploy__DeployRequest status_req = SATDEPLOY__DEPLOY_REQUEST__INIT;
    status_req.command = SATDEPLOY__DEPLOY_COMMAND__CMD_STATUS;

    Satdeploy__DeployResponse *status_resp = NULL;
    char current_hash[32] = {0};

    if (send_deploy_request(node, &status_req, &status_resp) == 0 && status_resp->success) {
        /* Find the app in status and get its current hash */
        for (size_t i = 0; i < status_resp->n_apps; i++) {
            if (strcmp(status_resp->apps[i]->app_name, app_name) == 0) {
                if (status_resp->apps[i]->binary_hash) {
                    strncpy(current_hash, status_resp->apps[i]->binary_hash, sizeof(current_hash) - 1);
                }
                break;
            }
        }
        satdeploy__deploy_response__free_unpacked(status_resp, NULL);
    }

    /* Now query backups */
    Satdeploy__DeployRequest req = SATDEPLOY__DEPLOY_REQUEST__INIT;
    req.command = SATDEPLOY__DEPLOY_COMMAND__CMD_LIST_VERSIONS;
    req.app_name = app_name;

    Satdeploy__DeployResponse *resp = NULL;
    if (send_deploy_request(node, &req, &resp) < 0) {
        return SLASH_EIO;
    }

    if (!resp->success) {
        output_error(resp->error_message);
        satdeploy__deploy_response__free_unpacked(resp, NULL);
        return SLASH_EIO;
    }

    /* Print formatted version table */
    char title[128];
    snprintf(title, sizeof(title), "Versions for %s:", app_name);
    output_title(title);
    printf("\n");

    if (resp->n_backups == 0) {
        printf("No versions found.\n");
        satdeploy__deploy_response__free_unpacked(resp, NULL);
        return SLASH_SUCCESS;
    }

    output_versions_header();
    output_separator(45);

    for (size_t i = 0; i < resp->n_backups; i++) {
        Satdeploy__BackupEntry *backup = resp->backups[i];
        int is_deployed = (current_hash[0] && backup->hash &&
                          strcmp(current_hash, backup->hash) == 0);

        output_version_row(backup->hash, backup->timestamp, is_deployed);
    }

    satdeploy__deploy_response__free_unpacked(resp, NULL);
    return SLASH_SUCCESS;
}

static int satdeploy_verify_cmd(struct slash *slash)
{
    unsigned int node = 0;
    char *app_name = NULL;
    char *remote_path = NULL;
    char *expected_checksum = NULL;

    optparse_t *parser = optparse_new("satdeploy verify", "<app_name>");
    optparse_add_help(parser);
    optparse_add_unsigned(parser, 'n', "node", "NUM", 0, &node, "Target node (default: from config)");
    optparse_add_string(parser, 'r', "remote", "PATH", &remote_path, "Remote file path to verify");
    optparse_add_string(parser, 'c', "checksum", "HEX", &expected_checksum, "Expected checksum to compare");

    int argi = optparse_parse(parser, slash->argc - 1, (const char **)slash->argv + 1);
    if (argi < 0) {
        optparse_del(parser);
        return SLASH_EINVAL;
    }

    if (argi >= slash->argc - 1) {
        printf("Error: app_name required\n");
        optparse_help(parser, stdout);
        optparse_del(parser);
        return SLASH_EUSAGE;
    }
    app_name = slash->argv[argi + 1];
    optparse_del(parser);

    /* Load config for defaults */
    satdeploy_config_t *config = satdeploy_config_load();

    /* Use config default if -n not specified */
    if (node == 0) {
        if (config && config->target_node != 0) {
            node = config->target_node;
        } else {
            node = slash_dfl_node;
        }
    }

    /* Look up remote_path from config if not specified */
    if (!remote_path && config) {
        satdeploy_app_config_t *app_config = satdeploy_config_get_app(config, app_name);
        if (app_config && app_config->remote_path[0]) {
            remote_path = app_config->remote_path;
        }
    }

    printf("Verifying '%s' on node %u...\n", app_name, node);

    Satdeploy__DeployRequest req = SATDEPLOY__DEPLOY_REQUEST__INIT;
    req.command = SATDEPLOY__DEPLOY_COMMAND__CMD_VERIFY;
    req.app_name = app_name;
    req.remote_path = remote_path ? remote_path : "";
    req.expected_checksum = expected_checksum ? expected_checksum : "";

    Satdeploy__DeployResponse *resp = NULL;
    if (send_deploy_request(node, &req, &resp) < 0) {
        return SLASH_EIO;
    }

    if (!resp->success) {
        printf("Verify failed: %s\n", resp->error_message);
        satdeploy__deploy_response__free_unpacked(resp, NULL);
        return SLASH_EIO;
    }

    printf("Checksum: %s\n", resp->actual_checksum);
    if (expected_checksum && strlen(expected_checksum) > 0) {
        if (strncmp(resp->actual_checksum, expected_checksum, strlen(expected_checksum)) == 0) {
            printf("Verification: MATCH\n");
        } else {
            printf("Verification: MISMATCH (expected %s)\n", expected_checksum);
        }
    }

    satdeploy__deploy_response__free_unpacked(resp, NULL);
    return SLASH_SUCCESS;
}

static int satdeploy_config_cmd(struct slash *slash)
{
    int reload = 0;

    optparse_t *parser = optparse_new("satdeploy config", "[--reload]");
    optparse_add_help(parser);
    optparse_add_set(parser, 'r', "reload", 1, &reload, "Force reload config from disk");

    int argi = optparse_parse(parser, slash->argc - 1, (const char **)slash->argv + 1);
    if (argi < 0) {
        optparse_del(parser);
        return SLASH_EINVAL;
    }
    optparse_del(parser);

    if (reload) {
        satdeploy_config_reset();
        printf("Config cache cleared\n");
    }

    char config_path[256];
    if (satdeploy_config_path(config_path, sizeof(config_path)) < 0) {
        printf("Error: Could not determine config path\n");
        return SLASH_EIO;
    }

    printf("Config file: %s\n", config_path);

    satdeploy_config_t *config = satdeploy_config_load();
    if (!config) {
        printf("  (failed to load)\n");
        return SLASH_EIO;
    }

    printf("\nDefaults:\n");
    printf("  target_node: %u%s\n", config->target_node,
           config->target_node == 0 ? " (use -n)" : "");

    printf("\nApps: %d\n", config->num_apps);
    for (int i = 0; i < config->num_apps; i++) {
        satdeploy_app_config_t *app = &config->apps[i];
        printf("  %s:\n", app->name);
        if (app->local_path[0]) {
            printf("    local:  %s\n", app->local_path);
        }
        if (app->remote_path[0]) {
            printf("    remote: %s\n", app->remote_path);
        }
    }

    if (config->num_apps == 0) {
        printf("  (none configured)\n");
    }

    return SLASH_SUCCESS;
}

static int satdeploy_help_cmd(struct slash *slash)
{
    (void)slash;
    printf("satdeploy - Satellite binary deployment tool\n\n");
    printf("Usage: satdeploy <command> [options]\n\n");
    printf("Commands:\n");
    printf("  config              Show current configuration\n");
    printf("  status              Query agent status\n");
    printf("  deploy <app>        Deploy a binary to the target\n");
    printf("  list <app>          List available backups\n");
    printf("  rollback <app>      Rollback to previous version\n");
    printf("  verify <app>        Verify installed binary checksum\n");
    printf("\nExamples:\n");
    printf("  satdeploy deploy test-app           Deploy using config defaults\n");
    printf("  satdeploy deploy -n 5424 test-app   Deploy to specific node\n");
    printf("  satdeploy list test-app             Show backup history\n");
    printf("  satdeploy rollback test-app         Restore previous version\n");
    printf("\nConfiguration: ~/.satdeploy/config.yaml\n");
    return SLASH_SUCCESS;
}

slash_command_group(satdeploy, "Satellite binary deployment");
slash_command_sub(satdeploy, help, satdeploy_help_cmd, NULL, "Show this help message");
slash_command_sub(satdeploy, config, satdeploy_config_cmd, "[-r]", "Show current configuration (use -r to reload)");
slash_command_sub(satdeploy, status, satdeploy_status_cmd, NULL, "Query agent status and list deployed apps");
slash_command_sub(satdeploy, deploy, satdeploy_deploy_cmd, "<app> [options]", "Deploy a binary to the target");
slash_command_sub(satdeploy, rollback, satdeploy_rollback_cmd, "<app> [-H hash]", "Rollback to previous version");
slash_command_sub(satdeploy, list, satdeploy_list_cmd, "<app>", "List available backups for an app");
slash_command_sub(satdeploy, verify, satdeploy_verify_cmd, "<app> [-r path] [-c checksum]", "Verify binary checksum");

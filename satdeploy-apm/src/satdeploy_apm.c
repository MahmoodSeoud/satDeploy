/*
 * satdeploy APM - Slash commands for satellite file deployment
 *
 * Provides commands to interact with satdeploy-agent running on target:
 *   satdeploy status  - Query agent status
 *   satdeploy deploy  - Deploy a file
 *   satdeploy rollback - Rollback to previous version
 *   satdeploy list    - List available backups
 *   satdeploy logs    - Show service logs
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/stat.h>

#include "sha256.h"

#include <slash/slash.h>
#include <slash/optparse.h>
#include <pthread.h>

#include <csp/csp.h>
#include <csp/csp_iflist.h>
#include <apm/csh_api.h>
#include <dtp/dtp.h>
#include <dtp/dtp_file_payload.h>
#include <dtp/dtp_protocol.h>
#include "deploy.pb-c.h"
#include "config.h"
#include "history.h"
#include "output.h"

#define SATDEPLOY_PORT 20
#define DEFAULT_TIMEOUT 30000

/* DTP defaults */
#define DTP_DEFAULT_MTU          1024
#define DTP_DEFAULT_THROUGHPUT   10000000
#define DTP_DEFAULT_TIMEOUT_S    60

/*
 * Tab completion for app names from config
 */
static void app_name_completer(struct slash *slash, char *token)
{
    satdeploy_config_t *cfg = satdeploy_config_load();
    if (!cfg || !cfg->loaded || cfg->num_apps == 0)
        return;

    /* Find the token to complete (last word in the buffer) */
    char *tok = token;
    size_t tok_len = strlen(tok);

    /* Strip leading spaces */
    while (*tok == ' ')
        tok++;
    tok_len = strlen(tok);

    int matches = 0;
    int last_match = -1;

    /* Count matches and print if multiple */
    for (int i = 0; i < cfg->num_apps; i++) {
        if (tok_len == 0 || strncmp(tok, cfg->apps[i].name, tok_len) == 0) {
            matches++;
            last_match = i;
        }
    }

    if (matches == 0) {
        return;
    } else if (matches == 1) {
        /* Single match — complete it */
        size_t prefix_len = token - slash->buffer;
        /* Preserve leading space */
        if (*token == ' ')
            prefix_len++;
        snprintf(slash->buffer + prefix_len, slash->line_size - prefix_len,
                 "%s", cfg->apps[last_match].name);
        slash->length = prefix_len + strlen(cfg->apps[last_match].name);
        slash->cursor = slash->length;
    } else {
        /* Multiple matches — find common prefix and print all */
        printf("\n");
        size_t common = strlen(cfg->apps[0].name);
        int first_match = -1;
        for (int i = 0; i < cfg->num_apps; i++) {
            if (tok_len == 0 || strncmp(tok, cfg->apps[i].name, tok_len) == 0) {
                printf("  %s\n", cfg->apps[i].name);
                if (first_match == -1) {
                    first_match = i;
                } else {
                    size_t p = 0;
                    while (p < common &&
                           cfg->apps[first_match].name[p] &&
                           cfg->apps[i].name[p] &&
                           cfg->apps[first_match].name[p] == cfg->apps[i].name[p])
                        p++;
                    if (p < common)
                        common = p;
                }
            }
        }
        /* Fill buffer with common prefix */
        if (common > tok_len && first_match >= 0) {
            size_t prefix_len = tok - slash->buffer;
            snprintf(slash->buffer + prefix_len, slash->line_size - prefix_len,
                     "%.*s", (int)common, cfg->apps[first_match].name);
            slash->length = prefix_len + common;
            slash->cursor = slash->length;
        }
    }
}

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

    /* SHA256 hash - first 8 hex chars, matches agent and ground station */
    sha256_ctx ctx;
    sha256_init(&ctx);

    unsigned char buf[4096];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0) {
        sha256_update(&ctx, buf, n);
    }
    fclose(f);

    unsigned char digest[32];
    sha256_final(&ctx, digest);

    snprintf(hash_out, hash_size, "%02x%02x%02x%02x",
             digest[0], digest[1], digest[2], digest[3]);
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

    /* Use agent_node from config if not specified via -n */
    if (node == 0) {
        satdeploy_config_t *config = satdeploy_config_load();
        if (config && config->agent_node > 0) {
            node = config->agent_node;
        }
        if (node == 0) {
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
        const char *status = app->running ? "running" : "deployed";
        int has_service = app->running;

        /* Look up provenance from history.db */
        satdeploy_deploy_record_t hist_rec;
        const char *provenance = NULL;
        if (satdeploy_history_get_last(app->app_name, &hist_rec) == 0 && hist_rec.valid) {
            if (hist_rec.git_hash[0]) {
                provenance = hist_rec.git_hash;
            }
        }

        output_status_row(
            app->app_name,
            status,
            app->file_hash,
            app->remote_path,
            provenance,
            app->running,
            has_service
        );
    }

    satdeploy__deploy_response__free_unpacked(resp, NULL);
    return SLASH_SUCCESS;
}

/*
 * DTP server support — override weak get_payload_meta from libdtp
 * to use the file-based payload registry.
 */
bool get_payload_meta(dtp_payload_meta_t *meta, uint8_t payload_id) {
    printf("[dtp-server] sizeof(dtp_meta_req_t)=%zu  get_payload_meta(id=%u)\n",
           sizeof(dtp_meta_req_t), payload_id);
    fflush(stdout);
    bool result = dtp_file_payload_get_meta(meta, payload_id);
    printf("[dtp-server] payload lookup: %s (size=%u)\n", result ? "OK" : "NOT FOUND",
           result ? (unsigned)meta->size : 0);
    fflush(stdout);
    return result;
}

/* DTP server thread context */
typedef struct {
    volatile bool exit_flag;
    volatile bool ready;
} dtp_server_ctx_t;

static void *dtp_server_thread(void *arg) {
    dtp_server_ctx_t *ctx = (dtp_server_ctx_t *)arg;
    ctx->ready = true;
    dtp_server_main(&ctx->exit_flag);
    return NULL;
}

/* Payload ID counter */
static uint8_t next_payload_id = 1;

/**
 * Deploy a single app to the target node.
 * Used by both single-app push and --all.
 */
static int deploy_single_app(unsigned int node, char *app_name,
                              const char *local_override, const char *remote_override,
                              int force, satdeploy_config_t *config)
{
    const char *local_path = local_override;
    const char *remote_path = remote_override;
    int adhoc_mode = (local_override && remote_override);

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

    /* Expand tilde in local_path */
    char expanded_path[MAX_PATH_LEN];
    if (local_path && local_path[0] == '~' && (local_path[1] == '/' || local_path[1] == '\0')) {
        const char *home = getenv("HOME");
        if (home) {
            snprintf(expanded_path, sizeof(expanded_path), "%s%s", home, local_path + 1);
            local_path = expanded_path;
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

    /* Check if already deployed with same hash (skip if --force) */
    if (!force) {
        Satdeploy__DeployRequest status_req = SATDEPLOY__DEPLOY_REQUEST__INIT;
        status_req.command = SATDEPLOY__DEPLOY_COMMAND__CMD_STATUS;

        Satdeploy__DeployResponse *status_resp = NULL;
        if (send_deploy_request(node, &status_req, &status_resp) == 0 && status_resp->success) {
            for (size_t i = 0; i < status_resp->n_apps; i++) {
                if (strcmp(status_resp->apps[i]->app_name, app_name) == 0) {
                    if (status_resp->apps[i]->file_hash &&
                        strcmp(status_resp->apps[i]->file_hash, checksum) == 0) {
                        printf("Already deployed: %s (%s)\n", app_name, checksum);
                        satdeploy__deploy_response__free_unpacked(status_resp, NULL);
                        return SLASH_SUCCESS;
                    }
                    break;
                }
            }
        }
        if (status_resp)
            satdeploy__deploy_response__free_unpacked(status_resp, NULL);
    }

    if (adhoc_mode) {
        printf("  Ad-hoc mode: no service restart, no dependency ordering.\n");
        printf("  To configure as a managed app, add it to config.yaml.\n\n");
    }
    printf("Deploying %s via DTP:\n", app_name);
    printf("  Local:    %s\n", local_path);
    printf("  Remote:   %s\n", remote_path);
    printf("  Size:     %u bytes\n", file_size);
    printf("  Checksum: %s\n", checksum);
    printf("  Target:   node %u\n", node);

    /* Step 1: Register the file as a DTP payload */
    uint8_t payload_id = next_payload_id++;
    printf("[dtp] Registering payload id=%u file=%s\n", payload_id, local_path);
    fflush(stdout);
    if (!dtp_file_payload_add(payload_id, local_path)) {
        printf("Error: Failed to register file as DTP payload\n");
        return SLASH_EIO;
    }
    printf("[dtp] Payload registered. Current payloads:\n");
    dtp_file_payload_info();
    fflush(stdout);

    /* Step 2: Start DTP server in background thread */
    dtp_server_ctx_t dtp_ctx = { .exit_flag = false, .ready = false };
    pthread_t dtp_thread;
    if (pthread_create(&dtp_thread, NULL, dtp_server_thread, &dtp_ctx) != 0) {
        printf("Error: Failed to start DTP server thread\n");
        dtp_file_payload_del(payload_id);
        return SLASH_EIO;
    }

    /* Wait for server thread to start */
    for (int i = 0; i < 50 && !dtp_ctx.ready; i++) {
        usleep(20000);
    }
    /* Give dtp_server_main time to bind port 7 */
    usleep(200000);

    /* Step 3: Send CMD_DEPLOY — agent will pull the file via DTP */
    csp_iface_t *default_iface = csp_iflist_get_by_isdfl(NULL);
    uint16_t ground_node = default_iface ? default_iface->addr : 0;

    printf("Deploying via DTP (ground node %u, payload %u)...\n", ground_node, payload_id);

    Satdeploy__DeployRequest deploy_req = SATDEPLOY__DEPLOY_REQUEST__INIT;
    deploy_req.command = SATDEPLOY__DEPLOY_COMMAND__CMD_DEPLOY;
    deploy_req.app_name = app_name;
    deploy_req.remote_path = (char *)remote_path;
    deploy_req.expected_size = file_size;
    deploy_req.expected_checksum = checksum;
    deploy_req.dtp_server_node = ground_node;
    deploy_req.dtp_server_port = 7;
    deploy_req.payload_id = payload_id;
    deploy_req.dtp_mtu = DTP_DEFAULT_MTU;
    deploy_req.dtp_throughput = DTP_DEFAULT_THROUGHPUT;
    deploy_req.dtp_timeout = DTP_DEFAULT_TIMEOUT_S;

    /* Get file mode */
    struct stat st;
    if (stat(local_path, &st) == 0) {
        deploy_req.file_mode = st.st_mode & 0777;
    }

    Satdeploy__DeployResponse *resp = NULL;
    int rc = send_deploy_request(node, &deploy_req, &resp);

    /* Step 4: Stop DTP server and clean up */
    dtp_ctx.exit_flag = true;
    pthread_join(dtp_thread, NULL);
    dtp_file_payload_del(payload_id);

    if (rc < 0) {
        printf("Error: No response from agent (timeout)\n");
        return SLASH_EIO;
    }

    if (!resp->success) {
        output_error(resp->error_message);
        satdeploy__deploy_response__free_unpacked(resp, NULL);
        return SLASH_EIO;
    }

    char success_msg[256];
    snprintf(success_msg, sizeof(success_msg), "Deployed %s (%s) via DTP", app_name, checksum);
    output_success(success_msg);

    satdeploy__deploy_response__free_unpacked(resp, NULL);
    return SLASH_SUCCESS;
}

static int satdeploy_deploy_cmd(struct slash *slash)
{
    unsigned int node = 0;
    char *app_name = NULL;
    char *local_path = NULL;
    char *remote_path = NULL;

    int force = 0;

    /*
     * Reorder argv so positional args come after options.
     * slash's optparse uses POSIX-style parsing (stops at first non-option),
     * so "deploy test_app -f /tmp/binary" would fail without this.
     */
    int sub_argc = slash->argc - 1;
    const char **sub_argv = (const char **)slash->argv + 1;
    const char *reordered[32];
    int nopt = 0, npos = 0;
    const char *positional[8];

    for (int i = 0; i < sub_argc && i < 30; i++) {
        if (sub_argv[i][0] == '-') {
            reordered[nopt++] = sub_argv[i];
            /* Options that take a value: consume the next arg too */
            if (i + 1 < sub_argc &&
                (strcmp(sub_argv[i], "-f") == 0 || strcmp(sub_argv[i], "--file") == 0 ||
                 strcmp(sub_argv[i], "-r") == 0 || strcmp(sub_argv[i], "--remote") == 0 ||
                 strcmp(sub_argv[i], "-n") == 0 || strcmp(sub_argv[i], "--node") == 0)) {
                reordered[nopt++] = sub_argv[++i];
            }
        } else {
            if (npos < 8)
                positional[npos++] = sub_argv[i];
        }
    }
    /* Append positional args after options */
    for (int i = 0; i < npos; i++)
        reordered[nopt + i] = positional[i];
    int total = nopt + npos;

    int deploy_all = 0;
    optparse_t *parser = optparse_new("satdeploy push", "<app_name> | -f PATH -r PATH | -a");
    optparse_add_help(parser);
    optparse_add_unsigned(parser, 'n', "node", "NUM", 0, &node, "Target node (default: from config)");
    optparse_add_string(parser, 'f', "local", "PATH", &local_path, "Local file path (overrides config)");
    optparse_add_string(parser, 'r', "remote", "PATH", &remote_path, "Remote installation path");
    optparse_add_set(parser, 'F', "force", 1, &force, "Force deploy even if same version");
    optparse_add_set(parser, 'a', "all", 1, &deploy_all, "Deploy all apps from config");

    int argi = optparse_parse(parser, total, reordered);
    if (argi < 0) {
        optparse_del(parser);
        return SLASH_EINVAL;
    }

    int adhoc_mode = 0;
    static char derived_name[128];

    /* Handle --all: deploy every app in config */
    if (deploy_all) {
        optparse_del(parser);
        satdeploy_config_t *all_config = satdeploy_config_load();
        if (!all_config || all_config->num_apps == 0) {
            printf("Error: No apps configured\n");
            return SLASH_EINVAL;
        }
        /* Use agent_node from config if not specified via -n */
        if (node == 0 && all_config->agent_node > 0)
            node = all_config->agent_node;
        if (node == 0)
            node = slash_dfl_node;

        int failed = 0;
        for (int i = 0; i < all_config->num_apps; i++) {
            int rc = deploy_single_app(node, all_config->apps[i].name,
                                       NULL, NULL, force, all_config);
            if (rc != SLASH_SUCCESS) failed++;
        }
        if (failed > 0) {
            printf("\n%d of %d deployments failed\n", failed, all_config->num_apps);
            return SLASH_EIO;
        }
        printf("\n%d app(s) deployed\n", all_config->num_apps);
        return SLASH_SUCCESS;
    }

    if (argi >= total) {
        /* No app name given — allow ad-hoc mode if both -f and -r are provided */
        if (local_path && remote_path) {
            /* Derive app name from remote path basename, strip extension */
            const char *base = strrchr(remote_path, '/');
            base = base ? base + 1 : remote_path;
            strncpy(derived_name, base, sizeof(derived_name) - 1);
            derived_name[sizeof(derived_name) - 1] = '\0';
            /* Strip final extension */
            char *dot = strrchr(derived_name, '.');
            if (dot && dot != derived_name) {
                *dot = '\0';
            }
            /* Replace remaining dots with dashes */
            for (char *p = derived_name; *p; p++) {
                if (*p == '.') *p = '-';
            }
            app_name = derived_name;
            adhoc_mode = 1;
        } else {
            printf("Error: app_name required (or use -f/-r for ad-hoc, or -a for all)\n");
            optparse_help(parser, stdout);
            optparse_del(parser);
            return SLASH_EUSAGE;
        }
    } else {
        app_name = (char *)reordered[argi];
    }
    optparse_del(parser);

    /* Load config for defaults */
    satdeploy_config_t *config = satdeploy_config_load();

    /* Use agent_node from config if not specified via -n */
    if (node == 0 && config && config->agent_node > 0) {
        node = config->agent_node;
    }
    if (node == 0) {
        node = slash_dfl_node;
    }

    return deploy_single_app(node, app_name, local_path, remote_path, force, config);
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

    /* Use agent_node from config if not specified via -n */
    if (node == 0 && config && config->agent_node > 0) {
        node = config->agent_node;
    }
    if (node == 0) {
        node = slash_dfl_node;
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

    /* Show which backup was restored */
    char success_msg[256];
    if (resp->backup_path && strlen(resp->backup_path) > 0) {
        /* Extract hash from backup path - new format: <hash>.bak */
        const char *filename = strrchr(resp->backup_path, '/');
        filename = filename ? filename + 1 : resp->backup_path;

        char restored_hash[16] = {0};
        size_t len = strlen(filename);
        if (len > 4 && strcmp(filename + len - 4, ".bak") == 0) {
            /* Backup format: YYYYMMDD-HHMMSS-{hash8}.bak
             * Extract hash from after the last '-' */
            const char *last_dash = strrchr(filename, '-');
            if (last_dash) {
                last_dash++;  /* skip the '-' */
                size_t hash_len = (filename + len - 4) - last_dash;
                if (hash_len > 0 && hash_len <= 8) {
                    strncpy(restored_hash, last_dash, hash_len);
                    restored_hash[hash_len] = '\0';
                }
            }
        }

        if (restored_hash[0]) {
            snprintf(success_msg, sizeof(success_msg), "Rolled back %s to %s",
                     app_name, restored_hash);
        } else {
            snprintf(success_msg, sizeof(success_msg), "Rolled back %s", app_name);
        }
    } else {
        snprintf(success_msg, sizeof(success_msg), "Rolled back %s", app_name);
    }
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

    /* Use agent_node from config if not specified via -n */
    if (node == 0) {
        satdeploy_config_t *config = satdeploy_config_load();
        if (config && config->agent_node > 0) {
            node = config->agent_node;
        }
        if (node == 0) {
            node = slash_dfl_node;
        }
    }

    /* Query versions (agent includes current deployed version in response) */
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
        /* Only first entry (version="current") is deployed */
        int is_deployed = (backup->version && strcmp(backup->version, "current") == 0);

        output_version_row(backup->hash, backup->timestamp, is_deployed);
    }

    satdeploy__deploy_response__free_unpacked(resp, NULL);
    return SLASH_SUCCESS;
}

static int satdeploy_logs_cmd(struct slash *slash)
{
    unsigned int node = 0;
    unsigned int lines = 100;
    char *app_name = NULL;

    optparse_t *parser = optparse_new("satdeploy logs", "<app_name>");
    optparse_add_help(parser);
    optparse_add_unsigned(parser, 'n', "node", "NUM", 0, &node, "Target node (default: from config)");
    optparse_add_unsigned(parser, 'l', "lines", "NUM", 0, &lines, "Number of log lines (default: 100)");

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

    /* Use agent_node from config if not specified via -n */
    if (node == 0 && config && config->agent_node > 0) {
        node = config->agent_node;
    }
    if (node == 0) {
        node = slash_dfl_node;
    }

    Satdeploy__DeployRequest req = SATDEPLOY__DEPLOY_REQUEST__INIT;
    req.command = SATDEPLOY__DEPLOY_COMMAND__CMD_LOGS;
    req.app_name = app_name;
    req.log_lines = lines;

    Satdeploy__DeployResponse *resp = NULL;
    if (send_deploy_request(node, &req, &resp) < 0) {
        return SLASH_EIO;
    }

    if (!resp->success) {
        printf("Logs failed: %s\n", resp->error_message);
        satdeploy__deploy_response__free_unpacked(resp, NULL);
        return SLASH_EIO;
    }

    if (resp->log_output && strlen(resp->log_output) > 0) {
        printf("%s\n", resp->log_output);
    } else {
        printf("No logs available for %s\n", app_name);
    }

    satdeploy__deploy_response__free_unpacked(resp, NULL);
    return SLASH_SUCCESS;
}

static int satdeploy_config_cmd(struct slash *slash)
{
    (void)slash;

    /* Always reload config from disk to show current state */
    satdeploy_config_reset();

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

    printf("\nAgent node: %u\n", config->agent_node);

    printf("\nApps: %d\n", config->num_apps);
    for (int i = 0; i < config->num_apps; i++) {
        satdeploy_app_config_t *app = &config->apps[i];
        printf("  %s:\n", app->name);
        if (app->local_path[0]) {
            printf("    local:       %s\n", app->local_path);
        }
        if (app->remote_path[0]) {
            printf("    remote:      %s\n", app->remote_path);
        }
    }

    if (config->num_apps == 0) {
        printf("  (none configured)\n");
    }

    return SLASH_SUCCESS;
}

/*
 * Interactive prompt helper: prints prompt, reads line, returns trimmed input.
 * If user enters empty string, default_val is used (if non-NULL).
 */
static void prompt_string(const char *prompt, const char *default_val,
                          char *out, size_t out_size)
{
    char buf[256];
    if (default_val && default_val[0]) {
        printf("  %s [%s]: ", prompt, default_val);
    } else {
        printf("  %s: ", prompt);
    }
    fflush(stdout);

    if (!fgets(buf, sizeof(buf), stdin)) {
        out[0] = '\0';
        return;
    }

    /* Strip trailing newline */
    size_t len = strlen(buf);
    while (len > 0 && (buf[len - 1] == '\n' || buf[len - 1] == '\r'))
        buf[--len] = '\0';

    if (len == 0 && default_val) {
        strncpy(out, default_val, out_size - 1);
        out[out_size - 1] = '\0';
    } else {
        strncpy(out, buf, out_size - 1);
        out[out_size - 1] = '\0';
    }
}

static int prompt_int(const char *prompt, int default_val)
{
    char buf[64];
    char def_str[32];
    snprintf(def_str, sizeof(def_str), "%d", default_val);
    prompt_string(prompt, def_str, buf, sizeof(buf));
    if (buf[0] == '\0')
        return default_val;
    return atoi(buf);
}

static int satdeploy_init_cmd(struct slash *slash)
{
    char config_path[MAX_PATH_LEN];
    if (satdeploy_config_path(config_path, sizeof(config_path)) < 0) {
        output_error("Could not determine config path");
        return SLASH_EINVAL;
    }

    /* Release terminal from slash raw mode so fgets works normally */
    slash_release_std_in_out(slash);

    /* Check if config already exists */
    FILE *check = fopen(config_path, "r");
    if (check) {
        fclose(check);
        char confirm[16];
        prompt_string("Config already exists. Overwrite? (y/N)", "N",
                      confirm, sizeof(confirm));
        if (confirm[0] != 'y' && confirm[0] != 'Y') {
            printf("  Aborted.\n");
            slash_acquire_std_in_out(slash);
            return SLASH_SUCCESS;
        }
    }

    printf(COLOR_BOLD "\n  Setting up satdeploy configuration...\n\n" COLOR_RESET);

    char name[MAX_APP_NAME_LEN];
    prompt_string("Target name", "default", name, sizeof(name));

    /* CSP is the only transport in APM (SSH is Python CLI only) */
    printf("  Transport: csp\n");

    char zmq_endpoint[MAX_PATH_LEN];
    prompt_string("ZMQ endpoint (zmqproxy host)", "tcp://localhost:9600",
                  zmq_endpoint, sizeof(zmq_endpoint));

    int agent_node = prompt_int("Agent CSP node", 5425);
    int ground_node = prompt_int("Ground CSP node", 40);

    /* Restore slash raw mode before any further output */
    slash_acquire_std_in_out(slash);

    /* Create config directory */
    char dir_path[MAX_PATH_LEN];
    const char *home = getenv("HOME");
    if (!home) {
        output_error("HOME not set");
        return SLASH_EINVAL;
    }
    snprintf(dir_path, sizeof(dir_path), "%s/.satdeploy", home);
    mkdir(dir_path, 0755);

    /* Write YAML config */
    FILE *f = fopen(config_path, "w");
    if (!f) {
        char msg[MAX_PATH_LEN + 32];
        snprintf(msg, sizeof(msg), "Could not write to %s", config_path);
        output_error(msg);
        return SLASH_EINVAL;
    }

    fprintf(f, "name: %s\n", name);
    fprintf(f, "transport: csp\n");
    fprintf(f, "zmq_endpoint: %s\n", zmq_endpoint);
    fprintf(f, "agent_node: %d\n", agent_node);
    fprintf(f, "ground_node: %d\n", ground_node);
    fprintf(f, "backup_dir: /opt/satdeploy/backups\n");
    fprintf(f, "max_backups: 10\n");
    fprintf(f, "apps: {}\n");

    fclose(f);

    /* Force config reload on next access */
    satdeploy_config_reset();

    printf("\n");
    char msg[MAX_PATH_LEN + 32];
    snprintf(msg, sizeof(msg), "Config saved to %s", config_path);
    output_success(msg);

    return SLASH_SUCCESS;
}

static int satdeploy_help_cmd(struct slash *slash)
{
    (void)slash;
    printf("  Deploy files to embedded Linux targets.\n\n");
    printf("Commands:\n");
    printf("  config    Show current configuration.\n");
    printf("  init      Interactive setup, creates config.yaml.\n");
    printf("  list      List all versions of an app (deployed + backups).\n");
    printf("  logs      Show logs for an app's service.\n");
    printf("  push      Deploy one or more apps to a target.\n");
    printf("  rollback  Rollback to a previous version.\n");
    printf("  status    Show status of deployed apps and services.\n");
    return SLASH_SUCCESS;
}

slash_command_group(satdeploy, "Satellite file deployment");
slash_command_sub(satdeploy, help, satdeploy_help_cmd, NULL, "Show this help message");
slash_command_sub(satdeploy, init, satdeploy_init_cmd, "", "Interactive setup, creates config.yaml.");
slash_command_sub(satdeploy, config, satdeploy_config_cmd, "", "Show current configuration.");
slash_command_sub_completer(satdeploy, push, satdeploy_deploy_cmd, app_name_completer, "<app> [options]", "Deploy one or more apps to a target.");
slash_command_sub_completer(satdeploy, list, satdeploy_list_cmd, app_name_completer, "<app>", "List all versions of an app (deployed + backups).");
slash_command_sub_completer(satdeploy, logs, satdeploy_logs_cmd, app_name_completer, "<app> [-l lines]", "Show logs for an app's service.");
slash_command_sub_completer(satdeploy, rollback, satdeploy_rollback_cmd, app_name_completer, "<app> [-H hash]", "Rollback to a previous version.");
slash_command_sub(satdeploy, status, satdeploy_status_cmd, NULL, "Show status of deployed apps and services.");

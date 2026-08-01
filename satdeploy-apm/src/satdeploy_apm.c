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
#include <fcntl.h>
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
#include "svu_serve_session.h"

/* Was config.h's; the config file is gone but the bound is still the one
 * every path buffer in this file and the agent protocol agree on. */
#ifndef MAX_PATH_LEN
#define MAX_PATH_LEN 256
#endif
#include "history.h"
#include "output.h"
#include "version.h"

#define SATDEPLOY_PORT 20
/* Long enough for the agent to complete a multi-hundred-MB DTP pull.
 * Must exceed DTP_DEFAULT_TIMEOUT_S * 1000 with margin (500 MB at 3 MB/s
 * is ~167 s on the wire, plus DTP setup, plus checksum). */
#define DEFAULT_TIMEOUT 360000

/* DTP defaults.
 * Throughput is intentionally conservative: at 10 MB/s with MTU 1024 the
 * receiver fires ~9.8k packets/sec, exhausting CSP_BUFFER_COUNT (1000) on
 * a localhost ZMQ loop and causing "RX zmq: Failed to get csp_buffer"
 * drops mid-transfer. 3 MB/s keeps the buffer pool from saturating. */
#define DTP_DEFAULT_MTU          1024
#define DTP_DEFAULT_THROUGHPUT   3000000
#define DTP_DEFAULT_TIMEOUT_S    300


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
    if (hash_out == NULL || hash_size < HISTORY_MAX_HASH) {
        return -1;
    }

    FILE *f = fopen(path, "rb");
    if (!f) {
        return -1;
    }

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

    /* Full 32-byte SHA256 → 64 hex chars + NUL. Display still uses %.8s. */
    for (int i = 0; i < 32; i++) {
        snprintf(hash_out + (i * 2), 3, "%02x", digest[i]);
    }
    hash_out[64] = '\0';
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
                                          0);  /* no CRC32: agent does not set it on reply */
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
    optparse_add_unsigned(parser, 'n', "node", "NUM", 0, &node, "Target node (default: current csh node)");

    int argi = optparse_parse(parser, slash->argc - 1, (const char **)slash->argv + 1);
    if (argi < 0) {
        optparse_del(parser);
        return SLASH_EINVAL;
    }
    optparse_del(parser);

    /* No config file: an unspecified -n means csh's currently selected node,
     * which is the node the operator already picked with `node <N>`. */
    if (node == 0) {
        node = slash_dfl_node;
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



/**
 * Deploy a single app to the target node.
 * The artifact is served over SVU for the duration of this call.
 *
 * dtp_mtu: always a concrete, already-validated value — callers resolve the
 * default before this point, so the ground's DTP_DEFAULT_MTU is the single
 * authority for what goes on the wire.
 */
static int deploy_single_app(unsigned int node, char *app_name,
                              const char *local_override, const char *remote_override,
                              int force, uint32_t dtp_mtu)
{
    const char *local_path = local_override;
    const char *remote_path = remote_override;
    int adhoc_mode = (local_override && remote_override);

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
        printf("       Use -f <path>\n");
        return SLASH_EUSAGE;
    }

    if (!remote_path) {
        printf("Error: No remote path specified\n");
        printf("       Use -r <path>\n");
        return SLASH_EUSAGE;
    }

    /* Auto-compute size and checksum from local file */
    uint32_t file_size = 0;
    char checksum[HISTORY_MAX_HASH] = {0};

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
                        printf("Already deployed: %s (%.8s)\n", app_name, checksum);
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
    }
    printf("Deploying %s over SVU:\n", app_name);
    printf("  Local:    %s\n", local_path);
    printf("  Remote:   %s\n", remote_path);
    printf("  Size:     %u bytes\n", file_size);
    printf("  Checksum: %.8s\n", checksum);
    printf("  Target:   node %u\n", node);

    /* Step 1: Start serving the artifact over SVU.
     *
     * There is no payload registry and no payload_id any more. libdtp
     * addressed an artifact by a uint8_t slot, which is why v1 hashed app
     * names into 256 values and carried a del-then-add dance plus an fd-1
     * redirect to hide the registry's error output. SVU serves the buffer it
     * is handed, so all of that goes away. */
    svu_serve_session_t server = {0};
    if (svu_serve_session_start(&server, local_path, SVU_SERVE_DEFAULT_BLOCK) != 0) {
        printf("Error: Failed to start the SVU server\n");
        return SLASH_EIO;
    }

    /* Step 3: Send CMD_DEPLOY — agent will pull the file over SVU */
    csp_iface_t *default_iface = csp_iflist_get_by_isdfl(NULL);
    uint16_t ground_node = default_iface ? default_iface->addr : 0;

    Satdeploy__DeployRequest deploy_req = SATDEPLOY__DEPLOY_REQUEST__INIT;
    deploy_req.command = SATDEPLOY__DEPLOY_COMMAND__CMD_DEPLOY;
    deploy_req.app_name = app_name;
    deploy_req.remote_path = (char *)remote_path;
    deploy_req.expected_size = file_size;
    deploy_req.expected_checksum = checksum;
    deploy_req.dtp_server_node = ground_node;
    /* Wire fields retained for protocol compatibility with agents that still
     * speak v1. The SVU path derives its ports from svu_proto.h and needs no
     * payload id, throughput hint, or idle timeout. */
    deploy_req.dtp_server_port = 7;
    deploy_req.payload_id = 0;
    deploy_req.dtp_mtu = dtp_mtu;
    deploy_req.dtp_throughput = DTP_DEFAULT_THROUGHPUT;
    deploy_req.dtp_timeout = DTP_DEFAULT_TIMEOUT_S;

    /* Get file mode */
    struct stat st;
    if (stat(local_path, &st) == 0) {
        deploy_req.file_mode = st.st_mode & 0777;
    }

    Satdeploy__DeployResponse *resp = NULL;
    int rc = send_deploy_request(node, &deploy_req, &resp);

    /* Step 4: Wind the SVU server down. svu_serve_loop frees its CTRL port on
     * stop, so a later push in the same process re-binds cleanly -- the
     * re-bind hazard that forced v1 to share one server across a loop does
     * not exist here. */
    svu_serve_session_stop(&server);

    if (rc < 0) {
        printf("Error: No response from agent (timeout)\n");
        satdeploy_history_write_t hist = {
            .module = "default",
            .app = app_name,
            .file_hash = checksum,
            .remote_path = remote_path,
            .action = "push",
            .success = 0,
            .error_message = "No response from agent (timeout)",
            .transport = "csp",
        };
        satdeploy_history_record(&hist);
        return SLASH_EIO;
    }

    if (!resp->success) {
        output_error(resp->error_message);
        satdeploy_history_write_t hist = {
            .module = "default",
            .app = app_name,
            .file_hash = checksum,
            .remote_path = remote_path,
            .action = "push",
            .success = 0,
            .error_message = resp->error_message,
            .transport = "csp",
        };
        satdeploy_history_record(&hist);
        satdeploy__deploy_response__free_unpacked(resp, NULL);
        return SLASH_EIO;
    }

    char success_msg[256];
    snprintf(success_msg, sizeof(success_msg), "Deployed %s (%.8s) over SVU", app_name, checksum);
    output_success(success_msg);

    /* Record successful deploy to history.db */
    satdeploy_history_write_t hist = {
        .module = "default",
        .app = app_name,
        .file_hash = checksum,
        .remote_path = remote_path,
        .action = "push",
        .success = 1,
        .backup_path = resp->backup_path,
        .transport = "csp",
    };
    satdeploy_history_record(&hist);

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
                 strcmp(sub_argv[i], "-n") == 0 || strcmp(sub_argv[i], "--node") == 0 ||
                 strcmp(sub_argv[i], "-m") == 0 || strcmp(sub_argv[i], "--mtu") == 0)) {
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

    unsigned int mtu = 0;
    optparse_t *parser = optparse_new("satdeploy push", "<app_name> | -f PATH -r PATH | -a");
    optparse_add_help(parser);
    optparse_add_unsigned(parser, 'n', "node", "NUM", 0, &node, "Target node (default: current csh node)");
    optparse_add_string(parser, 'f', "local", "PATH", &local_path, "Local file path");
    optparse_add_string(parser, 'r', "remote", "PATH", &remote_path, "Remote installation path");
    optparse_add_set(parser, 'F', "force", 1, &force, "Force deploy even if same version");
    optparse_add_unsigned(parser, 'm', "mtu", "BYTES", 0, &mtu, "Transfer MTU 64-1024 (default 1024)");

    int argi = optparse_parse(parser, total, reordered);
    if (argi < 0) {
        optparse_del(parser);
        return SLASH_EINVAL;
    }

    /* Validate at the boundary: the agent uses mtu-8 as its fragment payload
     * span, so a tiny value would fail far from the mistake, on the flight
     * side. Reject here, before any request is built or sent. The upper bound
     * is DTP_DEFAULT_MTU, the largest value any deployment has ever run. */
    if (mtu != 0 && (mtu < 64 || mtu > DTP_DEFAULT_MTU)) {
        printf("Error: --mtu %u out of range [64, %u]\n", mtu, DTP_DEFAULT_MTU);
        optparse_del(parser);
        return SLASH_EUSAGE;
    }
    if (mtu == 0)
        mtu = DTP_DEFAULT_MTU;

    /* deploy_single_app re-derives adhoc mode from local/remote overrides;
     * we just need a place to land app_name when ad-hoc with -f/-r. */
    static char derived_name[128];

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

    if (node == 0) {
        node = slash_dfl_node;
    }

    return deploy_single_app(node, app_name, local_path, remote_path, force, mtu);
}

static int satdeploy_rollback_cmd(struct slash *slash)
{
    unsigned int node = 0;
    char *app_name = NULL;
    char *hash = NULL;
    char *remote_path = NULL;

    optparse_t *parser = optparse_new("satdeploy rollback", "<app_name> -r <remote_path>");
    optparse_add_help(parser);
    optparse_add_unsigned(parser, 'n', "node", "NUM", 0, &node, "Target node (default: current csh node)");
    optparse_add_string(parser, 'r', "remote", "PATH", &remote_path, "Remote installation path (required)");
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

    if (node == 0) {
        node = slash_dfl_node;
    }

    if (!remote_path || !remote_path[0]) {
        char errmsg[256];
        snprintf(errmsg, sizeof(errmsg), "No remote path given for '%s'; pass -r <path>", app_name);
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
        satdeploy_history_write_t hist = {
            .module = "default",
            .app = app_name,
            .file_hash = "",
            .remote_path = remote_path,
            .action = "rollback",
            .success = 0,
            .error_message = "No response from agent (timeout)",
            .transport = "csp",
        };
        satdeploy_history_record(&hist);
        return SLASH_EIO;
    }

    if (!resp->success) {
        output_error(resp->error_message);
        satdeploy_history_write_t hist = {
            .module = "default",
            .app = app_name,
            .file_hash = "",
            .remote_path = remote_path,
            .action = "rollback",
            .success = 0,
            .error_message = resp->error_message,
            .transport = "csp",
        };
        satdeploy_history_record(&hist);
        satdeploy__deploy_response__free_unpacked(resp, NULL);
        return SLASH_EIO;
    }

    /* Show which backup was restored */
    char success_msg[256];
    char restored_hash[HISTORY_MAX_HASH] = {0};
    if (resp->backup_path && strlen(resp->backup_path) > 0) {
        /* Backup filename format: YYYYMMDD-HHMMSS-<hash>.bak
         * Extract the hash segment between the last '-' and ".bak". Accepts
         * both legacy 8-char and current 64-char hashes (older backups won't
         * disappear after the bump — they're addressable by their truncated
         * hash). */
        const char *filename = strrchr(resp->backup_path, '/');
        filename = filename ? filename + 1 : resp->backup_path;

        size_t len = strlen(filename);
        if (len > 4 && strcmp(filename + len - 4, ".bak") == 0) {
            const char *last_dash = strrchr(filename, '-');
            if (last_dash) {
                last_dash++;
                size_t hash_len = (filename + len - 4) - last_dash;
                if (hash_len > 0 && hash_len < HISTORY_MAX_HASH) {
                    memcpy(restored_hash, last_dash, hash_len);
                    restored_hash[hash_len] = '\0';
                }
            }
        }

        if (restored_hash[0]) {
            snprintf(success_msg, sizeof(success_msg), "Rolled back %s to %.8s",
                     app_name, restored_hash);
        } else {
            snprintf(success_msg, sizeof(success_msg), "Rolled back %s", app_name);
        }
    } else {
        snprintf(success_msg, sizeof(success_msg), "Rolled back %s", app_name);
    }
    output_success(success_msg);

    /* Record successful rollback to history.db */
    satdeploy_history_write_t hist = {
        .module = "default",
        .app = app_name,
        .file_hash = restored_hash[0] ? restored_hash : "",
        .remote_path = remote_path,
        .action = "rollback",
        .success = 1,
        .backup_path = resp->backup_path,
        .transport = "csp",
    };
    satdeploy_history_record(&hist);

    satdeploy__deploy_response__free_unpacked(resp, NULL);
    return SLASH_SUCCESS;
}

static int satdeploy_list_cmd(struct slash *slash)
{
    unsigned int node = 0;
    char *app_name = NULL;

    char *deploy_path = NULL;

    optparse_t *parser = optparse_new("satdeploy list", "<app_name> [-r remote_path]");
    optparse_add_help(parser);
    /* The agent dedups the "current" entry with its matching backup, so that
     * row's path is the .bak file and not the install slot. Naming the slot
     * here restores the distinction; without it the agent's path is shown. */
    optparse_add_string(parser, 'r', "remote", "PATH", &deploy_path, "Remote install path, for the deployed row");
    optparse_add_unsigned(parser, 'n', "node", "NUM", 0, &node, "Target node (default: current csh node)");

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


    if (node == 0) {
        node = slash_dfl_node;
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
        printf("  No versions found on target.\n");
        satdeploy__deploy_response__free_unpacked(resp, NULL);
        return SLASH_SUCCESS;
    }

    output_versions_header();
    output_separator(80);

    for (size_t i = 0; i < resp->n_backups; i++) {
        Satdeploy__BackupEntry *backup = resp->backups[i];
        /* Only first entry (version="current") is deployed */
        int is_deployed = (backup->version && strcmp(backup->version, "current") == 0);

        /* For the deployed row, show the deploy slot rather than the backup
         * .bak path the agent returned (see comment on `deploy_path` above).
         * Backup rows keep their .bak path so an operator can locate the file
         * on target if they need to rescue it manually. */
        const char *row_path = is_deployed && deploy_path && deploy_path[0]
                                   ? deploy_path
                                   : backup->path;

        /* Old agents (before BackupEntry.size_bytes was added) leave the
         * field at proto3 default 0 — output_version_row renders "-" in
         * that case, so the column degrades cleanly across mixed-version
         * deploys. */
        output_version_row(backup->hash, backup->timestamp, is_deployed,
                           backup->size_bytes, row_path);
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
    optparse_add_unsigned(parser, 'n', "node", "NUM", 0, &node, "Target node (default: current csh node)");
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

    /* Validate the app exists in config — same rationale as list_cmd. */

    /* Use agent_node from config if not specified via -n */
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
        printf("Logs failed: %s\n",
               resp->error_message ? resp->error_message : "(no error message)");
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





static int satdeploy_version_cmd(struct slash *slash)
{
    (void)slash;
    printf("satdeploy-apm %s (%s)\n", SATDEPLOY_VERSION, SATDEPLOY_GIT_REV);
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
    printf("  version   Show APM version.\n");
    printf("\n");
    printf("Quick recipes:\n");
    printf("  satdeploy push controller            # deploy one app from config\n");
    printf("  satdeploy push -a                    # deploy every app\n");
    printf("  satdeploy push -f ./bin/foo -r /opt/foo   # ad-hoc, no config entry\n");
    printf("  satdeploy status                     # what's running on the target\n");
    printf("  satdeploy list controller            # versions: deployed + backups\n");
    printf("  satdeploy rollback controller        # back one version\n");
    printf("  satdeploy logs controller -l 50      # last 50 service log lines\n");
    printf("\n");
    printf("Use -n <node> on any command to target a different CSP node.\n");
    printf("Full reference: https://github.com/MahmoodSeoud/satDeploy/blob/main/docs/commands.md\n");
    return SLASH_SUCCESS;
}

slash_command_group(satdeploy, "Satellite file deployment");
slash_command_sub(satdeploy, help, satdeploy_help_cmd, NULL, "Show this help message");
slash_command_sub(satdeploy, push, satdeploy_deploy_cmd, "<app> [options]", "Deploy one or more apps to a target.");
slash_command_sub(satdeploy, list, satdeploy_list_cmd, "<app>", "List all versions of an app (deployed + backups).");
slash_command_sub(satdeploy, logs, satdeploy_logs_cmd, "<app> [-l lines]", "Show logs for an app's service.");
slash_command_sub(satdeploy, rollback, satdeploy_rollback_cmd, "<app> [-H hash]", "Rollback to a previous version.");
slash_command_sub(satdeploy, status, satdeploy_status_cmd, NULL, "Show status of deployed apps and services.");
slash_command_sub(satdeploy, version, satdeploy_version_cmd, NULL, "Show APM version.");

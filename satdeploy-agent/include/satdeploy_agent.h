/**
 * satdeploy-agent - Satellite-side deployment agent
 *
 * Main header file with shared definitions.
 */

#ifndef SATDEPLOY_AGENT_H
#define SATDEPLOY_AGENT_H

#include <stdint.h>
#include <stdbool.h>

/* CSP port for deploy commands */
#define DEPLOY_PORT 20

/* Default backup directory */
#define BACKUP_DIR "/opt/satdeploy/backups"

/* Maximum path length — sized to handle backup paths like
 * /opt/satdeploy/backups/<app>/YYYYMMDD-HHMMSS-<hash>.bak */
#define MAX_PATH_LEN 512

/* Maximum app name length */
#define MAX_APP_NAME_LEN 64

/* Full SHA256 hex length and buffer size.
 *
 * We transmit the *full* 64-hex SHA256 across the wire so the agent can
 * gate cross-pass resume by strict-equality content addressing — an 8-char
 * prefix is not collision-resistant enough to be safe when a re-staged
 * binary lands on a partially-received transfer's bitmap.
 *
 * Display is still truncated to 8 chars (printf("%.8s", hash)) so the
 * status table stays readable. */
#define HASH_HEX_LEN 64
#define HASH_BUF_LEN 65  /* HASH_HEX_LEN + NUL */

/* Global running flag (set to 0 to trigger shutdown) */
extern volatile int running;

/**
 * Initialize the deploy handler.
 *
 * Binds to CSP port 20 and starts handling deploy commands.
 *
 * @return 0 on success, -1 on failure.
 */
int deploy_handler_init(void);

/**
 * Run the deploy handler loop.
 *
 * This function blocks and handles incoming deploy connections.
 * Should be called from a dedicated thread.
 */
void deploy_handler_loop(void);

/* --- Shared utilities --- */

/**
 * Recursively create directory path (like mkdir -p).
 *
 * @param path Directory path to create.
 * @return 0 on success, -1 on failure.
 */
int mkdir_p(const char *path);

/**
 * Copy a file, handling ETXTBSY (running binary replacement).
 *
 * @param src Source file path.
 * @param dst Destination file path.
 * @return 0 on success, -1 on failure.
 */
int copy_file(const char *src, const char *dst);

/**
 * Compute SHA256 checksum of a file.
 *
 * Writes the full 64-char hex digest plus NUL terminator (65 bytes total).
 * Callers display only the first 8 with `%.8s` for readable output.
 *
 * @param path Path to the file.
 * @param hash_out Buffer to store hex digest (must be >= HASH_BUF_LEN).
 * @param hash_size Size of hash_out buffer (must be >= HASH_BUF_LEN).
 * @return 0 on success, -1 on failure.
 */
int compute_file_checksum(const char *path, char *hash_out, size_t hash_size);

/**
 * Create a backup of a file.
 *
 * @param app_name Application name (used for backup directory).
 * @param src_path Source file path to backup.
 * @param backup_path_out Buffer to store the backup path.
 * @param backup_path_size Size of backup_path_out buffer.
 * @return 0 on success, -1 on failure.
 */
int backup_create(const char *app_name, const char *src_path,
                  char *backup_path_out, size_t backup_path_size);

/**
 * Restore a backup file.
 *
 * @param backup_path Path to the backup file.
 * @param dest_path Destination path to restore to.
 * @return 0 on success, -1 on failure.
 */
int backup_restore(const char *backup_path, const char *dest_path);

/**
 * List backups for an app.
 *
 * @param app_name Application name.
 * @param callback Function called for each backup found.
 * @param user_data User data passed to callback.
 * @return Number of backups found, or -1 on error.
 */
typedef void (*backup_list_callback)(const char *version, const char *timestamp,
                                     const char *hash, const char *path,
                                     void *user_data);
int backup_list(const char *app_name, backup_list_callback callback, void *user_data);

/* dtp_download_file moved to libsatpush as satpush_pull_file (see
 * ../libsatpush/include/satpush/satpush.h). The pre-extract
 * src/dtp_client.c remains on disk for revert safety but is no longer
 * built into the agent. */

/**
 * Save app deployment metadata.
 *
 * @param app_name Application name.
 * @param remote_path Path where app is installed.
 * @param file_hash Hash of the deployed file.
 * @return 0 on success, -1 on failure.
 */
int app_metadata_save(const char *app_name, const char *remote_path,
                      const char *file_hash);

/**
 * Get app deployment metadata.
 *
 * @param app_name Application name.
 * @param remote_path Buffer for remote path (can be NULL).
 * @param path_size Size of remote_path buffer.
 * @param file_hash Buffer for full SHA256 hex (can be NULL); must be >= HASH_BUF_LEN.
 * @param hash_size Size of file_hash buffer.
 * @param deployed_at Buffer for timestamp (can be NULL).
 * @param time_size Size of deployed_at buffer.
 * @return 0 on success, -1 if app not found.
 */
int app_metadata_get(const char *app_name, char *remote_path, size_t path_size,
                     char *file_hash, size_t hash_size,
                     char *deployed_at, size_t time_size);

/**
 * List all deployed apps.
 *
 * @param callback Function called for each app.
 * @param user_data User data passed to callback.
 * @return Number of apps.
 */
typedef void (*app_metadata_callback)(const char *app_name, const char *remote_path,
                                      const char *file_hash, const char *deployed_at,
                                      void *user_data);
int app_metadata_list(app_metadata_callback callback, void *user_data);

/**
 * Reload metadata from disk (clears cache).
 */
void app_metadata_reload(void);

#endif /* SATDEPLOY_AGENT_H */

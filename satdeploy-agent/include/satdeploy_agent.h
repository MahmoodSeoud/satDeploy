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

/* Maximum path length */
#define MAX_PATH_LEN 256

/* Maximum app name length */
#define MAX_APP_NAME_LEN 64

/* Hash length (8 hex chars) */
#define HASH_LEN 8

/**
 * Initialize the deploy handler.
 *
 * Binds to CSP port 20 and starts handling deploy commands.
 *
 * @return 0 on success, -1 on failure.
 */
int deploy_handler_init(void);

/**
 * Compute SHA256 checksum of a file.
 *
 * @param path Path to the file.
 * @param hash_out Buffer to store the first 8 chars of hex digest.
 * @param hash_size Size of hash_out buffer (must be >= 9).
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

/**
 * Download a file via DTP protocol.
 *
 * @param server_node DTP server CSP node address.
 * @param payload_id DTP payload identifier.
 * @param dest_path Local path to save the downloaded file.
 * @param expected_size Expected file size (0 to skip size check).
 * @return 0 on success, -1 on failure.
 */
int dtp_download_file(uint32_t server_node, uint16_t payload_id,
                      const char *dest_path, uint32_t expected_size);

#endif /* SATDEPLOY_AGENT_H */

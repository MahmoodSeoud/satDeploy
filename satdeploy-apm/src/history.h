/**
 * history.h - Deployment history database (read + write)
 *
 * Reads and writes ~/.satdeploy/history.db, matching the Python CLI's
 * history module schema. Both Python (SSH deploys) and APM (CSP deploys)
 * write to the same database using WAL mode for concurrency.
 */

#ifndef SATDEPLOY_HISTORY_H
#define SATDEPLOY_HISTORY_H

/* SHA256 hex (64 chars) + NUL. Shared by all hash-bearing buffers in APM. */
#define HISTORY_MAX_HASH 65
#define HISTORY_MAX_PROV 128
#define HISTORY_MAX_PATH 256
#define HISTORY_MAX_MSG  512

/* Last deployment record for an app (read) */
typedef struct {
    char app[64];
    char file_hash[HISTORY_MAX_HASH];
    char git_hash[HISTORY_MAX_PROV];      /* provenance string e.g. "main@3c940acf" */
    char remote_path[HISTORY_MAX_PATH];
    int  success;
    int  valid;                            /* 0 if no record found */
} satdeploy_deploy_record_t;

/* Record to write after a deploy or rollback (write) */
typedef struct {
    const char *module;           /* target name from config, or "default" */
    const char *app;              /* app name */
    const char *file_hash;        /* SHA256 of deployed file (full 64 hex chars) */
    const char *remote_path;      /* install path on target */
    const char *action;           /* "push" or "rollback" */
    int         success;          /* 1 for success, 0 for failure */
    const char *git_hash;         /* provenance, or NULL */
    const char *backup_path;      /* backup file path, or NULL */
    const char *error_message;    /* error details if !success, or NULL */
    const char *transport;        /* "csp" for APM deploys */
} satdeploy_history_write_t;

/**
 * Get the last successful deployment record for an app.
 *
 * @param app_name Application name.
 * @param record   Output record (zeroed if not found).
 * @return 0 on success, -1 on error (db not found, etc).
 */
int satdeploy_history_get_last(const char *app_name, satdeploy_deploy_record_t *record);

/**
 * Record a deployment or rollback to history.db.
 *
 * Creates the database and schema if they don't exist. Uses WAL mode
 * and busy_timeout=5000 for concurrent access with the Python CLI.
 *
 * History write failures are logged but never fatal — a failed history
 * write should not block a deploy.
 *
 * @param record  The deployment record to write.
 * @return 0 on success, -1 on error.
 */
int satdeploy_history_record(const satdeploy_history_write_t *record);

#endif /* SATDEPLOY_HISTORY_H */

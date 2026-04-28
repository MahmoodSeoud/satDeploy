/*
 * output.h - CLI output formatting utilities for satdeploy APM
 *
 * Provides ANSI colors, Unicode symbols, and tabular formatting
 * to match the Python satdeploy CLI output style.
 */

#ifndef SATDEPLOY_OUTPUT_H
#define SATDEPLOY_OUTPUT_H

#include <stdio.h>
#include <stdint.h>

/* ANSI color codes */
#define COLOR_RESET       "\033[0m"
#define COLOR_GREEN       "\033[32m"
#define COLOR_YELLOW      "\033[33m"
#define COLOR_RED         "\033[31m"
#define COLOR_BLUE        "\033[34m"
#define COLOR_WHITE       "\033[37m"
#define COLOR_BRIGHT_WHITE "\033[97m"
#define COLOR_BRIGHT_BLACK "\033[90m"
#define COLOR_BOLD        "\033[1m"

/* ASCII symbols (fallback for terminals without UTF-8) */
#define SYM_CHECK   ">"   /* Running/success */
#define SYM_CROSS   "x"   /* Failed */
#define SYM_ARROW   ">"   /* Deployed/current */
#define SYM_BULLET  "*"   /* Backup/stopped */

/* Column widths for tabular output */
#define COL_APP_WIDTH      16
#define COL_STATUS_WIDTH   14
#define COL_HASH_WIDTH     10
#define COL_TIMESTAMP_WIDTH 20

/*
 * Print a success message with green checkmark.
 * Example: "▸ Deployed controller (a3f2c9b1)"
 */
void output_success(const char *message);

/*
 * Print a warning message with yellow color.
 * Example: "[WARNING] Service not found"
 */
void output_warning(const char *message);

/*
 * Print an error message with red color.
 * Example: "[ERROR] Connection failed"
 */
void output_error(const char *message);

/*
 * Print a table header with dim color.
 * Formats: "    APP             STATUS          HASH          TIMESTAMP"
 */
void output_status_header(void);

/*
 * Print a separator line.
 * Formats: "    ------------------------------------------------------------"
 */
void output_separator(int width);

/*
 * Print a status row for an app.
 *
 * Args:
 *   app_name: Application name
 *   status: "running", "stopped", "failed", "deployed", "not deployed"
 *   hash: File hash (8 chars) or "-"
 *   path: Remote path (optional, can be NULL)
 *   is_running: Whether service is running (for symbol selection)
 *   has_service: Whether app has a service (vs library)
 */
void output_status_row(const char *app_name, const char *status,
                       const char *hash, const char *path,
                       const char *provenance,
                       int is_running, int has_service);

/*
 * Print a list/versions header.
 * Formats: "    HASH          TIMESTAMP             STATUS    SIZE         PATH"
 */
void output_versions_header(void);

/*
 * Print a version row for backup listing.
 *
 * Args:
 *   hash: File hash
 *   timestamp: Deployment timestamp
 *   is_deployed: Whether this version is currently deployed
 *   size_bytes: File size in bytes (0 = unknown / not displayed)
 *   path: Path on target (deploy path for current, backup file for archives;
 *         NULL = not displayed)
 */
void output_version_row(const char *hash, const char *timestamp, int is_deployed,
                        uint64_t size_bytes, const char *path);

/*
 * Print a title line in bold.
 */
void output_title(const char *title);

/*
 * Print a step counter message.
 * Example: "[1/5] Stopping controller"
 */
void output_step(int current, int total, const char *message);

#endif /* SATDEPLOY_OUTPUT_H */

/*
 * output.c - CLI output formatting utilities for satdeploy APM
 */

#include "output.h"
#include <string.h>

void output_success(const char *message)
{
    printf(COLOR_GREEN SYM_CHECK " %s" COLOR_RESET "\n", message);
}

void output_warning(const char *message)
{
    printf(COLOR_YELLOW "[WARNING] %s" COLOR_RESET "\n", message);
}

void output_error(const char *message)
{
    printf(COLOR_RED "[ERROR] %s" COLOR_RESET "\n", message);
}

void output_status_header(void)
{
    printf(COLOR_BRIGHT_BLACK "    %-*s\t%-*s\t%-*s\tPATH" COLOR_RESET "\n",
           COL_APP_WIDTH, "APP",
           COL_STATUS_WIDTH, "STATUS",
           COL_HASH_WIDTH, "HASH");
}

void output_separator(int width)
{
    printf(COLOR_BRIGHT_BLACK "    ");
    for (int i = 0; i < width; i++) {
        printf("-");
    }
    printf(COLOR_RESET "\n");
}

void output_status_row(const char *app_name, const char *status,
                       const char *hash, const char *path,
                       const char *provenance,
                       int is_running, int has_service)
{
    const char *symbol;
    const char *status_color;
    const char *symbol_color;

    if (!has_service) {
        /* Library - show deployed status */
        symbol = SYM_BULLET;
        symbol_color = COLOR_GREEN;
        status_color = COLOR_GREEN;
    } else if (is_running) {
        symbol = SYM_CHECK;
        symbol_color = COLOR_GREEN;
        status_color = COLOR_GREEN;
    } else if (strcmp(status, "failed") == 0) {
        symbol = SYM_CROSS;
        symbol_color = COLOR_RED;
        status_color = COLOR_RED;
    } else if (strcmp(status, "not deployed") == 0) {
        symbol = SYM_BULLET;
        symbol_color = COLOR_YELLOW;
        status_color = COLOR_YELLOW;
    } else if (strcmp(status, "deployed") == 0) {
        /* File on target, no running process */
        symbol = SYM_BULLET;
        symbol_color = COLOR_GREEN;
        status_color = COLOR_GREEN;
    } else {
        /* stopped or other */
        symbol = SYM_BULLET;
        symbol_color = COLOR_YELLOW;
        status_color = COLOR_YELLOW;
    }

    /* Build hash column: "abcd1234 (main@deadbeef)" or just "abcd1234".
     * file_hash is full 64-char SHA256; display first 8 (git short-hash style)
     * so the table stays readable at COL_HASH_WIDTH. */
    char hash_col[64];
    const char *h = hash ? hash : "-";
    if (provenance && provenance[0]) {
        snprintf(hash_col, sizeof(hash_col), "%.8s (%s)", h, provenance);
    } else {
        snprintf(hash_col, sizeof(hash_col), "%.8s", h);
    }

    printf("  %s%s%s %-*s\t%s%-*s%s\t%s%-10s%s\t%s%s%s\n",
           symbol_color, symbol, COLOR_RESET,
           COL_APP_WIDTH, app_name,
           status_color, COL_STATUS_WIDTH, status, COLOR_RESET,
           COLOR_WHITE, hash_col, COLOR_RESET,
           COLOR_BRIGHT_BLACK, path ? path : "", COLOR_RESET);
}

void output_versions_header(void)
{
    printf(COLOR_BRIGHT_BLACK "    %-*s\t%-*s\tSTATUS" COLOR_RESET "\n",
           COL_HASH_WIDTH, "HASH",
           COL_TIMESTAMP_WIDTH, "TIMESTAMP");
}

void output_version_row(const char *hash, const char *timestamp, int is_deployed)
{
    const char *symbol;
    const char *color;
    const char *status_text;

    if (is_deployed) {
        symbol = SYM_ARROW;
        color = COLOR_GREEN;
        status_text = "deployed";
    } else {
        symbol = SYM_BULLET;
        color = COLOR_BLUE;
        status_text = "backup";
    }

    /* Display first 8 chars of SHA256 (git short-hash style). */
    char hash_short[9] = {0};
    if (hash) {
        strncpy(hash_short, hash, 8);
    } else {
        hash_short[0] = '-';
    }

    printf("  %s%s%s %s%-*s%s\t%s%-*s%s\t%s%s%s\n",
           color, symbol, COLOR_RESET,
           color, COL_HASH_WIDTH, hash_short, COLOR_RESET,
           COLOR_BRIGHT_BLACK, COL_TIMESTAMP_WIDTH, timestamp ? timestamp : "-", COLOR_RESET,
           color, status_text, COLOR_RESET);
}

void output_title(const char *title)
{
    printf(COLOR_BOLD "%s" COLOR_RESET "\n", title);
}

void output_step(int current, int total, const char *message)
{
    printf(COLOR_BRIGHT_WHITE "[%d/%d]" COLOR_RESET " %s\n", current, total, message);
}

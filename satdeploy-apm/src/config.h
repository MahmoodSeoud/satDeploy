/**
 * satdeploy config - YAML configuration file support
 *
 * Reads defaults from ~/.satdeploy/config.yaml
 */

#ifndef SATDEPLOY_CONFIG_H
#define SATDEPLOY_CONFIG_H

#include <stdint.h>
#include <stdbool.h>

#define MAX_APPS 32
#define MAX_APP_NAME_LEN 64
#define MAX_PATH_LEN 256

/* Per-app configuration */
typedef struct {
    char name[MAX_APP_NAME_LEN];
    char local_path[MAX_PATH_LEN];      /* Local binary path (on ground station) */
    char remote_path[MAX_PATH_LEN];     /* Remote install path (on satellite) */
} satdeploy_app_config_t;

/* Global configuration */
typedef struct {
    /* Agent address */
    uint32_t agent_node;                /* satdeploy-agent CSP node address */

    /* App configs */
    satdeploy_app_config_t apps[MAX_APPS];
    int num_apps;

    /* Config loaded flag */
    bool loaded;
} satdeploy_config_t;

/**
 * Reset cached config (forces reload on next load call).
 */
void satdeploy_config_reset(void);

/**
 * Load configuration from ~/.satdeploy/config.yaml
 *
 * @return Pointer to static config structure, or NULL on error.
 */
satdeploy_config_t *satdeploy_config_load(void);

/**
 * Get app-specific config by name.
 *
 * @param config Config structure.
 * @param app_name Application name to look up.
 * @return Pointer to app config, or NULL if not found.
 */
satdeploy_app_config_t *satdeploy_config_get_app(satdeploy_config_t *config,
                                                  const char *app_name);

/**
 * Get the config file path.
 *
 * @param path_out Buffer to store path.
 * @param path_size Size of buffer.
 * @return 0 on success, -1 on error.
 */
int satdeploy_config_path(char *path_out, size_t path_size);

#endif /* SATDEPLOY_CONFIG_H */

/**
 * satdeploy config - YAML configuration file support
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <yaml.h>

#include "config.h"

/* Static config instance */
static satdeploy_config_t g_config = {0};

void satdeploy_config_reset(void)
{
    memset(&g_config, 0, sizeof(g_config));
}

int satdeploy_config_path(char *path_out, size_t path_size)
{
    const char *home = getenv("HOME");
    if (!home) {
        return -1;
    }

    int ret = snprintf(path_out, path_size, "%s/.satdeploy/config.yaml", home);
    if (ret < 0 || (size_t)ret >= path_size) {
        return -1;
    }

    return 0;
}

/* Helper to safely copy string */
static void safe_strcpy(char *dest, const char *src, size_t dest_size)
{
    if (!src) {
        dest[0] = '\0';
        return;
    }
    strncpy(dest, src, dest_size - 1);
    dest[dest_size - 1] = '\0';
}

/* Parse state machine states */
typedef enum {
    STATE_START,
    STATE_ROOT,
    STATE_DEFAULTS,
    STATE_DEFAULTS_KEY,
    STATE_APPS,
    STATE_APP_NAME,
    STATE_APP_MAP,
    STATE_APP_KEY,
    /* For legacy Python satdeploy format */
    STATE_MODULES,
    STATE_MODULE_NAME,
    STATE_MODULE_MAP,
    STATE_MODULE_KEY,
} parse_state_t;

satdeploy_config_t *satdeploy_config_load(void)
{
    /* Return cached config if already loaded */
    if (g_config.loaded) {
        return &g_config;
    }

    char config_path[MAX_PATH_LEN];
    if (satdeploy_config_path(config_path, sizeof(config_path)) < 0) {
        return NULL;
    }

    FILE *file = fopen(config_path, "r");
    if (!file) {
        /* Config file doesn't exist - return empty config with defaults */
        g_config.num_apps = 0;
        g_config.loaded = true;
        return &g_config;
    }

    yaml_parser_t parser;
    yaml_event_t event;

    if (!yaml_parser_initialize(&parser)) {
        fclose(file);
        return NULL;
    }

    yaml_parser_set_input_file(&parser, file);

    /* Set defaults */
    g_config.appsys_node = 0;
    g_config.num_apps = 0;

    parse_state_t state = STATE_START;
    char current_key[128] = {0};
    char current_app_name[MAX_APP_NAME_LEN] = {0};
    satdeploy_app_config_t *current_app = NULL;
    int done = 0;

    while (!done) {
        if (!yaml_parser_parse(&parser, &event)) {
            fprintf(stderr, "YAML parse error: %s\n", parser.problem);
            yaml_parser_delete(&parser);
            fclose(file);
            return NULL;
        }

        switch (event.type) {
        case YAML_STREAM_END_EVENT:
            done = 1;
            break;

        case YAML_MAPPING_START_EVENT:
            if (state == STATE_START) {
                state = STATE_ROOT;
            } else if (state == STATE_DEFAULTS) {
                /* Already in defaults mapping */
            } else if (state == STATE_APPS) {
                /* Starting apps mapping */
            } else if (state == STATE_APP_NAME) {
                /* Starting individual app config */
                state = STATE_APP_MAP;
            } else if (state == STATE_MODULES) {
                /* Starting modules mapping */
            } else if (state == STATE_MODULE_NAME) {
                /* Starting individual module config */
                state = STATE_MODULE_MAP;
            }
            break;

        case YAML_MAPPING_END_EVENT:
            if (state == STATE_DEFAULTS_KEY || state == STATE_DEFAULTS) {
                state = STATE_ROOT;
            } else if (state == STATE_APP_MAP || state == STATE_APP_KEY) {
                state = STATE_APPS;
                current_app = NULL;
            } else if (state == STATE_APPS) {
                state = STATE_ROOT;
            } else if (state == STATE_MODULE_MAP || state == STATE_MODULE_KEY) {
                state = STATE_MODULES;
            } else if (state == STATE_MODULES) {
                state = STATE_ROOT;
            }
            break;

        case YAML_SCALAR_EVENT: {
            const char *value = (const char *)event.data.scalar.value;

            if (state == STATE_ROOT) {
                if (strcmp(value, "defaults") == 0) {
                    state = STATE_DEFAULTS;
                } else if (strcmp(value, "apps") == 0) {
                    state = STATE_APPS;
                } else if (strcmp(value, "modules") == 0) {
                    /* Legacy Python satdeploy format */
                    state = STATE_MODULES;
                } else {
                    /* Read flat top-level fields (Python CLI format) */
                    safe_strcpy(current_key, value, sizeof(current_key));
                    state = STATE_DEFAULTS_KEY;  /* reuse defaults value handler */
                }
            } else if (state == STATE_DEFAULTS) {
                safe_strcpy(current_key, value, sizeof(current_key));
                state = STATE_DEFAULTS_KEY;
            } else if (state == STATE_DEFAULTS_KEY) {
                if (strcmp(current_key, "appsys_node") == 0) {
                    g_config.appsys_node = (uint32_t)atoi(value);
                }
                state = STATE_DEFAULTS;
            } else if (state == STATE_MODULES) {
                /* Module name (e.g., "satellite") */
                state = STATE_MODULE_NAME;
            } else if (state == STATE_MODULE_MAP) {
                safe_strcpy(current_key, value, sizeof(current_key));
                state = STATE_MODULE_KEY;
            } else if (state == STATE_MODULE_KEY) {
                /* Legacy module fields - ignored, use csh default node */
                state = STATE_MODULE_MAP;
            } else if (state == STATE_APPS) {
                /* This is an app name key */
                safe_strcpy(current_app_name, value, sizeof(current_app_name));
                if (g_config.num_apps < MAX_APPS) {
                    current_app = &g_config.apps[g_config.num_apps];
                    memset(current_app, 0, sizeof(*current_app));
                    safe_strcpy(current_app->name, value, sizeof(current_app->name));
                    g_config.num_apps++;
                }
                state = STATE_APP_NAME;
            } else if (state == STATE_APP_MAP) {
                safe_strcpy(current_key, value, sizeof(current_key));
                state = STATE_APP_KEY;
            } else if (state == STATE_APP_KEY && current_app) {
                /* Support both new and legacy field names */
                if (strcmp(current_key, "local_path") == 0 ||
                    strcmp(current_key, "local") == 0) {
                    safe_strcpy(current_app->local_path, value,
                               sizeof(current_app->local_path));
                } else if (strcmp(current_key, "remote_path") == 0 ||
                           strcmp(current_key, "remote") == 0) {
                    safe_strcpy(current_app->remote_path, value,
                               sizeof(current_app->remote_path));
                } else if (strcmp(current_key, "param") == 0) {
                    safe_strcpy(current_app->param, value,
                               sizeof(current_app->param));
                } else if (strcmp(current_key, "csp_node") == 0) {
                    current_app->csp_node = (uint32_t)atoi(value);
                }
                state = STATE_APP_MAP;
            }
            break;
        }

        default:
            break;
        }

        yaml_event_delete(&event);
    }

    yaml_parser_delete(&parser);
    fclose(file);

    g_config.loaded = true;
    return &g_config;
}

satdeploy_app_config_t *satdeploy_config_get_app(satdeploy_config_t *config,
                                                  const char *app_name)
{
    if (!config || !app_name) {
        return NULL;
    }

    for (int i = 0; i < config->num_apps; i++) {
        if (strcmp(config->apps[i].name, app_name) == 0) {
            return &config->apps[i];
        }
    }

    return NULL;
}

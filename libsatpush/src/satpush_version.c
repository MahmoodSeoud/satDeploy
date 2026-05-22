/*
 * satpush_version.c - library version string.
 *
 * Step 1 of the libsatpush extract (per /plan-eng-review 2026-05-21) ships
 * only this stub so the directory structure and meson build resolve. Step 2
 * adds satpush_pull.c, satpush_serve.c, session_state.c, and sha256.c from
 * satdeploy-agent/src/.
 */

#include "satpush/satpush.h"

#ifndef SATPUSH_VERSION
#define SATPUSH_VERSION "0.1.0"
#endif

const char* satpush_version_string(void) {
    return SATPUSH_VERSION;
}

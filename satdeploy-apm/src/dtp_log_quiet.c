/*
 * dtp_log_quiet.c — silence libdtp's unconditional [LOG]/[WARN] printfs.
 *
 * The DTP submodule's lib/dtp/src/dtp_log.c defines dbg_log_impl,
 * dbg_warn_impl, and dbg_enable_colors as plain extern functions that
 * unconditionally printf every message. During a normal `satdeploy push`
 * that produces 25+ lines of "Incoming connection / Got meta data request /
 * Round time / nof_csp_packets" noise mixed into operator output.
 *
 * libdtp doesn't expose a level filter, so we resolve those symbols from
 * this object instead of from libdtp's dtp_log.o. As long as this file is
 * listed in the APM's source list (it is, see meson.build), the static
 * linker satisfies the dbg_log_impl reference from us first and never
 * needs to pull dtp_log.o out of libdtp_*.a — that file is silently dropped
 * from the final shared object.
 *
 * To re-enable libdtp logging for debugging, set SATDEPLOY_DTP_VERBOSE=1
 * before loading the APM. The check happens once on first call so the
 * runtime cost is one getenv per process.
 */

#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>

/* Match the prototypes from libdtp's dtp_log.h so -Wmissing-prototypes is
 * happy and the linker resolves identical signatures. */
#include "dtp/dtp_log.h"

static int dtp_verbose_cached = -1;

static int dtp_verbose(void)
{
    if (dtp_verbose_cached < 0) {
        const char *v = getenv("SATDEPLOY_DTP_VERBOSE");
        dtp_verbose_cached = (v && v[0] && v[0] != '0') ? 1 : 0;
    }
    return dtp_verbose_cached;
}

void dbg_log_impl(const char *file, unsigned int line,
                  const char *__restrict format, ...)
{
    if (!dtp_verbose()) return;
    printf("[LOG] %s:%u: ", file, line);
    va_list ap;
    va_start(ap, format);
    vprintf(format, ap);
    va_end(ap);
    printf("\n");
}

void dbg_warn_impl(const char *file, unsigned int line,
                   const char *__restrict format, ...)
{
    if (!dtp_verbose()) return;
    printf("[WARN] %s:%u: ", file, line);
    va_list ap;
    va_start(ap, format);
    vprintf(format, ap);
    va_end(ap);
    printf("\n");
}

/* libdtp's dbg_enable_colors flips a static-color flag inside dtp_log.c.
 * Since that file is no longer linked, callers reach this no-op instead. */
void dbg_enable_colors(bool enable)
{
    (void)enable;
}

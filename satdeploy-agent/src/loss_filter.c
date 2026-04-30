/*
 * loss_filter.c — TEST-ONLY CSP packet drop hook for thesis experiments.
 *
 * See loss_filter.h for the rationale and call-site notes.
 *
 * This file is only compiled when -DSATDEPLOY_TEST_LOSS_FILTER is set.
 * Flight builds get the inline no-op stubs from the header instead, and
 * meson omits this source from the build entirely.
 *
 * Pattern file format: experiments/loss-pattern-format.md
 */

#ifdef SATDEPLOY_TEST_LOSS_FILTER

#include <errno.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "loss_filter.h"

/* On-disk action tags. Match experiments/loss-pattern-format.md. */
typedef enum {
    ACT_UP    = 0,
    ACT_DOWN  = 1,
    ACT_PROB  = 2,
    ACT_CLEAR = 3,
} action_t;

typedef struct {
    double   t_offset_s;   /* seconds since pattern start */
    action_t action;
    double   prob;         /* only used for ACT_PROB */
} event_t;

/* Module state. Mutex protects only the stats counters and current_idx;
 * pattern is read-only after init. */
static struct {
    bool      initialized;
    bool      enabled;          /* false if no LOSS_PATTERN_FILE set */
    event_t  *events;           /* sorted by t_offset_s */
    size_t    n_events;
    struct timespec t0;         /* clock anchor (init time) */

    /* Current state machine — what the link is doing right now. */
    bool      link_up;          /* true = pass packets, false = drop all */
    double    drop_prob;        /* if non-zero, Bernoulli drop at this rate */
    size_t    current_idx;      /* next event to apply */

    /* Stats. */
    uint32_t  packets_seen;
    uint32_t  packets_dropped;

    pthread_mutex_t mtx;
} g = {
    .mtx = PTHREAD_MUTEX_INITIALIZER,
    .link_up = true,
};

/* Cheap deterministic PRNG for ACT_PROB Bernoulli decisions. We don't
 * need cryptographic randomness — we need reproducibility per seed.
 * xorshift64 is 5 lines and perfect here. Seeded from the env var
 * LOSS_PATTERN_SEED (default 0x12345678). */
static uint64_t g_rng_state = 0x12345678ULL;

static uint64_t xorshift64(void) {
    uint64_t x = g_rng_state;
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    g_rng_state = x;
    return x;
}

static double rand_unit(void) {
    /* Top 53 bits as a double in [0, 1). */
    return (double)(xorshift64() >> 11) / (double)(1ULL << 53);
}

/* --------------------------------------------------------------------
 * Pattern file parsing
 * -------------------------------------------------------------------- */

static int parse_pattern_line(const char *line, event_t *out) {
    /* Skip blank lines and comments. */
    while (*line && (*line == ' ' || *line == '\t')) line++;
    if (*line == '\0' || *line == '\n' || *line == '#') {
        return 1;  /* skip */
    }

    char action_buf[16];
    double t = 0;
    double prob = 0;
    int matched = sscanf(line, "%lf %15s %lf", &t, action_buf, &prob);
    if (matched < 2) {
        return -1;
    }
    out->t_offset_s = t;
    out->prob = 0;
    if (strcmp(action_buf, "up") == 0) {
        out->action = ACT_UP;
    } else if (strcmp(action_buf, "down") == 0) {
        out->action = ACT_DOWN;
    } else if (strcmp(action_buf, "clear") == 0) {
        out->action = ACT_CLEAR;
    } else if (strcmp(action_buf, "prob") == 0) {
        if (matched < 3) return -1;
        out->action = ACT_PROB;
        out->prob = prob;
    } else {
        return -1;
    }
    return 0;
}

static int load_pattern_file(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) {
        fprintf(stderr, "[loss_filter] cannot open '%s': %s\n",
                path, strerror(errno));
        return -1;
    }

    /* Two-pass: count, then allocate, then fill. Keeps the data structure
     * compact and lets us bail early on parse errors. */
    char buf[256];
    size_t n = 0;
    while (fgets(buf, sizeof(buf), f)) {
        event_t tmp;
        int rc = parse_pattern_line(buf, &tmp);
        if (rc == 0) n++;
        else if (rc < 0) {
            fprintf(stderr, "[loss_filter] %s: parse error in line: %s",
                    path, buf);
            fclose(f);
            return -1;
        }
    }

    g.events = calloc(n, sizeof(event_t));
    if (!g.events) { fclose(f); return -1; }
    g.n_events = n;

    rewind(f);
    size_t i = 0;
    while (fgets(buf, sizeof(buf), f) && i < n) {
        event_t tmp;
        int rc = parse_pattern_line(buf, &tmp);
        if (rc == 0) g.events[i++] = tmp;
    }
    fclose(f);

    /* Validate monotonic timestamps. */
    for (size_t j = 1; j < g.n_events; j++) {
        if (g.events[j].t_offset_s < g.events[j-1].t_offset_s) {
            fprintf(stderr, "[loss_filter] %s: timestamps not monotonic "
                    "(event[%zu]=%.3f < event[%zu]=%.3f)\n",
                    path, j, g.events[j].t_offset_s,
                    j-1, g.events[j-1].t_offset_s);
            free(g.events);
            g.events = NULL;
            g.n_events = 0;
            return -1;
        }
    }
    return 0;
}

/* --------------------------------------------------------------------
 * Public API
 * -------------------------------------------------------------------- */

int loss_filter_init(void) {
    pthread_mutex_lock(&g.mtx);
    if (g.initialized) {
        pthread_mutex_unlock(&g.mtx);
        return 0;
    }

    const char *path = getenv("LOSS_PATTERN_FILE");
    if (path == NULL || path[0] == '\0') {
        /* No env var set — filter is a no-op. Common case for production
         * runs that just happen to be linked against a test build. */
        g.initialized = true;
        g.enabled = false;
        pthread_mutex_unlock(&g.mtx);
        return 0;
    }

    if (load_pattern_file(path) != 0) {
        pthread_mutex_unlock(&g.mtx);
        return -1;
    }

    /* Optional: seed the PRNG. */
    const char *seed_str = getenv("LOSS_PATTERN_SEED");
    if (seed_str) {
        g_rng_state = (uint64_t)strtoull(seed_str, NULL, 0);
        if (g_rng_state == 0) g_rng_state = 1;  /* xorshift hates 0 */
    }

    clock_gettime(CLOCK_MONOTONIC, &g.t0);
    g.initialized = true;
    g.enabled = true;
    g.link_up = true;
    g.drop_prob = 0;
    g.current_idx = 0;

    fprintf(stderr, "[loss_filter] loaded %zu events from %s\n",
            g.n_events, path);
    pthread_mutex_unlock(&g.mtx);
    return 0;
}

static double now_offset_s(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (now.tv_sec - g.t0.tv_sec) + (now.tv_nsec - g.t0.tv_nsec) / 1e9;
}

/* Apply any events whose timestamp is <= now. Updates link_up / drop_prob. */
static void advance_state_locked(double now_s) {
    while (g.current_idx < g.n_events &&
           g.events[g.current_idx].t_offset_s <= now_s) {
        event_t *ev = &g.events[g.current_idx];
        switch (ev->action) {
            case ACT_UP:    g.link_up = true;  g.drop_prob = 0; break;
            case ACT_DOWN:  g.link_up = false; g.drop_prob = 0; break;
            case ACT_CLEAR: g.link_up = true;  g.drop_prob = 0; break;
            case ACT_PROB:  g.link_up = true;  g.drop_prob = ev->prob; break;
        }
        g.current_idx++;
    }
}

bool loss_filter_should_drop(void) {
    pthread_mutex_lock(&g.mtx);
    if (!g.initialized || !g.enabled) {
        pthread_mutex_unlock(&g.mtx);
        return false;
    }

    g.packets_seen++;

    double now_s = now_offset_s();
    advance_state_locked(now_s);

    bool drop = false;
    if (!g.link_up) {
        drop = true;
    } else if (g.drop_prob > 0) {
        drop = (rand_unit() < g.drop_prob);
    }

    if (drop) g.packets_dropped++;

    pthread_mutex_unlock(&g.mtx);
    return drop;
}

void loss_filter_close(void) {
    pthread_mutex_lock(&g.mtx);
    if (g.events) {
        free(g.events);
        g.events = NULL;
    }
    g.n_events = 0;
    g.initialized = false;
    g.enabled = false;

    if (g.packets_seen > 0) {
        fprintf(stderr,
                "[loss_filter] final stats: dropped %u of %u packets (%.2f%%)\n",
                g.packets_dropped, g.packets_seen,
                100.0 * g.packets_dropped / g.packets_seen);
    }
    pthread_mutex_unlock(&g.mtx);
}

void loss_filter_stats(uint32_t *out_packets_seen,
                       uint32_t *out_packets_dropped) {
    pthread_mutex_lock(&g.mtx);
    if (out_packets_seen)    *out_packets_seen = g.packets_seen;
    if (out_packets_dropped) *out_packets_dropped = g.packets_dropped;
    pthread_mutex_unlock(&g.mtx);
}

#endif  /* SATDEPLOY_TEST_LOSS_FILTER */

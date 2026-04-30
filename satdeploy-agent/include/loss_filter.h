/*
 * loss_filter.h — TEST-ONLY CSP packet drop hook for thesis experiments.
 *
 * What it does:
 *   When the agent boots with the LOSS_PATTERN_FILE environment variable
 *   set, this module loads a pattern file (see
 *   experiments/loss-pattern-format.md) and exposes a single decision
 *   function: "should I drop the next packet?"
 *
 *   The agent's CSP receive paths consult this function before processing
 *   incoming packets. When the filter returns true, the packet is freed
 *   and the receive call returns as if the packet never arrived.
 *
 * Why it's compile-time gated:
 *   This is *test scaffolding*, not flight code. Flight builds must NEVER
 *   include this module. The entire compilation is wrapped in
 *
 *       #ifdef SATDEPLOY_TEST_LOSS_FILTER
 *
 *   so a flight build (no -DSATDEPLOY_TEST_LOSS_FILTER) gets the empty
 *   stubs from the bottom of this header. The compiler optimizes the
 *   conditional checks out entirely; the resulting binary has no
 *   loss_filter code at all.
 *
 *   Belt-and-braces: the meson build for the agent only adds
 *   loss_filter.c to the source list when the test option is enabled.
 *   Even if a future engineer ifdef's wrong, the symbol just doesn't
 *   exist in the flight build.
 *
 * Where it hooks:
 *   See callers in deploy_handler.c and dtp_client.c (or wherever the
 *   agent ultimately receives CSP packets). The general pattern is:
 *
 *       csp_packet_t *p = csp_read(conn, timeout);
 *       if (p && loss_filter_should_drop()) {
 *           csp_buffer_free(p);
 *           p = NULL;
 *       }
 *
 *   For DTP, hooking is more invasive — DTP runs its own internal loop.
 *   The cleanest path is to hook the libdtp callback that delivers
 *   per-packet data to the application; if libdtp doesn't expose such a
 *   hook, see CALLER NOTES at the bottom.
 */

#ifndef LOSS_FILTER_H
#define LOSS_FILTER_H

#include <stdbool.h>
#include <stdint.h>

#ifdef SATDEPLOY_TEST_LOSS_FILTER

/*
 * Initialize the filter. Reads $LOSS_PATTERN_FILE (env var). On failure
 * to read the file, logs a warning and sets the filter to no-op (always
 * returns "don't drop"). Safe to call multiple times — second and later
 * calls are no-ops.
 *
 * Return: 0 on success (file loaded or env var unset), -1 on parse error.
 */
int loss_filter_init(void);

/*
 * Should the *next* packet be dropped?
 *
 * Decision is based on:
 *   - Wall-clock time since loss_filter_init() was called
 *   - The pattern file's events at that timestamp
 *
 * Thread-safe (intended to be called from CSP receive callbacks which
 * may run on libcsp's router thread).
 *
 * If the filter is uninitialized OR the env var was unset, returns false.
 */
bool loss_filter_should_drop(void);

/*
 * Free pattern memory. Called from main() at shutdown. Safe to call
 * before init() (no-op).
 */
void loss_filter_close(void);

/*
 * Statistics. Returned by reference for caller-friendly printf.
 * Useful for the agent's per-trial summary log.
 */
void loss_filter_stats(uint32_t *out_packets_seen,
                       uint32_t *out_packets_dropped);

#else  /* !SATDEPLOY_TEST_LOSS_FILTER */

/* Stubs for flight builds. Compiler eliminates the calls entirely. */
static inline int  loss_filter_init(void)        { return 0; }
static inline bool loss_filter_should_drop(void) { return false; }
static inline void loss_filter_close(void)       { }
static inline void loss_filter_stats(uint32_t *p, uint32_t *d) {
    if (p) *p = 0;
    if (d) *d = 0;
}

#endif  /* SATDEPLOY_TEST_LOSS_FILTER */

#endif  /* LOSS_FILTER_H */

/* ===========================================================================
 * CALLER NOTES — where to hook this in the agent code
 * ===========================================================================
 *
 * 1. CSP control socket (deploy_handler.c).
 *
 *    The simple case. After csp_read returns, check loss_filter_should_drop;
 *    if true, free the packet and behave as if it never arrived.
 *
 *        // satdeploy-agent/src/deploy_handler.c
 *        csp_packet_t *packet = csp_read(conn, 10000);
 *        if (packet && loss_filter_should_drop()) {
 *            csp_buffer_free(packet);
 *            packet = NULL;
 *        }
 *        if (packet == NULL) {
 *            printf("[deploy] error: no data received (or filter dropped)\n");
 *            return;
 *        }
 *
 *    This causes the deploy command itself to be droppable, which is
 *    realistic — at high real-world loss, the deploy request frame can
 *    fail just like any other CSP packet.
 *
 * 2. DTP data path (dtp_client.c).
 *
 *    DTP maintains its own session and runs its own receive loop inside
 *    libdtp. Hooking it cleanly requires either:
 *
 *      (a) A libdtp callback for "packet just arrived". libdtp does
 *          expose hooks via dtp_session_set_user_ctx + the various
 *          on_data hooks. The cleanest place is an on_data_received
 *          callback that returns "drop me" — wire that to consult
 *          loss_filter_should_drop().
 *
 *      (b) If libdtp doesn't expose a usable RX hook, alternative is
 *          to wrap the underlying CSP interface with a "lossy" wrapper
 *          that intercepts at the iface->nexthop level. More invasive
 *          but applies uniformly to all CSP traffic regardless of which
 *          subsystem owns the socket.
 *
 *    Recommended: start with (a). If libdtp's hook surface isn't
 *    sufficient, escalate to (b).
 *
 * 3. Initialization order.
 *
 *    Call loss_filter_init() in main.c BEFORE csp_init() runs — the
 *    filter's wall clock starts at init() time, and we want that to
 *    happen before any CSP traffic flows. The pattern file env var
 *    LOSS_PATTERN_FILE should also be honored at this point.
 *
 *        // satdeploy-agent/src/main.c
 *        int main(int argc, char **argv) {
 *            ...
 *            if (loss_filter_init() != 0) {
 *                fprintf(stderr, "loss_filter: pattern file failed to load\n");
 *                exit(1);
 *            }
 *            ...
 *            csp_init(...);
 *            ...
 *            atexit(loss_filter_close);
 *        }
 *
 * 4. Logging.
 *
 *    At shutdown (or every N seconds during a long run), call
 *    loss_filter_stats() and log: "loss_filter: dropped N of M packets".
 *    Goes into agent_log; the harness reads it and writes to CSV.
 */

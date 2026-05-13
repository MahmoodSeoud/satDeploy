/*
 * loss_filter_iface_hook.c — TEST-ONLY linker-wrap of csp_qfifo_write.
 *
 * Why this exists:
 *   libcsp routes every received CSP packet — from any interface (ZMQ, CAN,
 *   KISS, ETH, I2C, UDP, loopback) — through a single function:
 *
 *       void csp_qfifo_write(csp_packet_t *packet,
 *                            csp_iface_t  *iface,
 *                            void         *pxTaskWoken);
 *
 *   It's the choke point where interface RX threads inject packets into the
 *   router's queue. If we wrap it, the loss filter sees every packet exactly
 *   once, regardless of which subsystem (deploy_handler, libdtp, libparam,
 *   csh shell commands) is reading from the resulting connection.
 *
 *   This replaces the earlier "filter at each call site" approach:
 *     - deploy_handler.c had its own should_drop() check on csp_read
 *     - lib/dtp/src/dtp_client.c had a patch around csp_recvfrom
 *   Both are now redundant. The iface-level hook is the single source of
 *   truth — call-site checks would double-filter the same packet.
 *
 * How the wrap works:
 *   GNU/LLVM ld supports `-Wl,--wrap=symbol`. The linker rewrites all calls
 *   to `csp_qfifo_write` so they hit `__wrap_csp_qfifo_write` instead, and
 *   the original implementation becomes available as `__real_csp_qfifo_write`.
 *   This means we don't need to modify libcsp source at all — the
 *   interception is purely a link-time decision, gated by a meson option.
 *
 * Flight builds:
 *   When `-Dtest_loss_filter=false` (default), this file isn't compiled and
 *   the `-Wl,--wrap=csp_qfifo_write` link arg isn't passed. The agent links
 *   directly against the real `csp_qfifo_write`, byte-identical to flight.
 *
 * Thread context:
 *   csp_qfifo_write is called from interface RX threads on POSIX builds.
 *   loss_filter_apply_latency() may sleep — fine on Linux, since the RX
 *   thread serializing one packet at a time accurately models a 4800-baud
 *   half-duplex radio link (next packet can't arrive until the previous
 *   one has finished transmission).
 */

#ifdef SATDEPLOY_TEST_LOSS_FILTER

#include <stddef.h>  /* NULL */

#include <csp/csp.h>
#include <csp/csp_buffer.h>
#include <csp/csp_interface.h>

#include "loss_filter.h"

/* Real implementation, supplied by the linker via --wrap. */
extern void __real_csp_qfifo_write(csp_packet_t *packet,
                                   csp_iface_t  *iface,
                                   void         *pxTaskWoken);

void __wrap_csp_qfifo_write(csp_packet_t *packet,
                            csp_iface_t  *iface,
                            void         *pxTaskWoken)
{
    if (packet != NULL && loss_filter_should_drop()) {
        /* Free the packet and return without touching the iface counters.
         * loss_filter_stats() is the source of truth for drop counts;
         * bumping iface->rx_error would lie about the physical interface. */
        csp_buffer_free(packet);
        return;
    }

    if (packet != NULL) {
        /* Apply the configured RTT floor before injecting into the router
         * queue. Models the time the real radio would have spent receiving
         * this packet's bytes — a 4800-baud half-duplex link cannot deliver
         * packet N+1 until packet N's frame has cleared the wire. */
        loss_filter_apply_latency();
    }

    __real_csp_qfifo_write(packet, iface, pxTaskWoken);
}

#endif  /* SATDEPLOY_TEST_LOSS_FILTER */

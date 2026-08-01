/*
 * svu_session.h - the SVU client transfer loop, decoupled from CSP bring-up.
 *
 * The whole point: this runs on an ALREADY-INITIALIZED CSP stack. A standalone
 * binary calls svu_net_init() first; the CSH APM calls nothing (csh already did
 * csp_init + the CAN interface + routes). Same transfer logic either way, so the
 * developer's csh session -- bus up, node map known -- is reused instead of
 * re-created. That reuse is the entire DX win of the APM form factor.
 */
#ifndef SVU_SESSION_H
#define SVU_SESSION_H

#include <stdint.h>

/*
 * Cross-pass persistence hooks. All optional (any pointer may be NULL, or the
 * whole struct may be NULL) -- without them the transfer is purely in-memory,
 * which is the standalone binary's behavior.
 *
 * The design deliberately persists DATA, not verification state: the caller
 * stores the raw partial reassembly buffer as fragments arrive, and the next
 * pass re-verifies whatever it restored against a FRESH manifest from the
 * server. A stale sidecar (artifact rebuilt between passes) simply fails
 * block verification and gets re-requested -- no generation counters, no
 * bitmap schema to keep in sync with the transfer geometry.
 */
typedef struct svu_client_hooks {
    void *ctx;
    /*
     * Offer a prior pass's partial data. Return a malloc'd buffer (ownership
     * transfers to the transfer loop, freed there) and set *total_out to its
     * length, or return NULL for a fresh transfer. When data is returned the
     * first request is manifest-only (one empty interval, nothing blasted),
     * so a resumed pass spends bandwidth only on the blocks that fail
     * verification.
     */
    uint8_t *(*restore)(void *ctx, uint32_t *total_out);
    /* Transfer geometry is known (manifest received). Restored data whose
     * length disagrees with `total` has been discarded by this point. */
    void (*meta)(void *ctx, uint32_t total, uint32_t block_size);
    /* One data fragment was accepted into the reassembly buffer. */
    void (*data)(void *ctx, uint32_t offset, const uint8_t *buf, uint32_t len);
    /* Every block verified; any persisted partial state is now obsolete. */
    void (*complete)(void *ctx);
} svu_client_hooks_t;

/*
 * Pull a file from `server_addr` over SVU, verify it block-by-block, and write it
 * to `outfile`. Assumes CSP is already initialized and the interface is up. Returns
 * 0 on a block-verified transfer, -1 otherwise. `block_size`/`mtu` are the client's
 * requested values; the server's manifest is authoritative for block size.
 */
int svu_client_run(uint16_t server_addr, uint32_t block_size, uint32_t mtu,
                   uint32_t max_rounds, const char *outfile);

/* svu_client_run with cross-pass persistence hooks (NULL = identical). */
int svu_client_run_hooked(uint16_t server_addr, uint32_t block_size, uint32_t mtu,
                          uint32_t max_rounds, const char *outfile,
                          const svu_client_hooks_t *hooks);

#endif /* SVU_SESSION_H */

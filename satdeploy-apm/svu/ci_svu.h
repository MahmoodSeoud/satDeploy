/*
 * ci_svu.h - the Self-Verifying Uploader core: a reassembly + block-integrity
 * tracker. Pure logic, no libcsp -- this is the "brain" a correct fire-and-forget
 * uploader is built around, and it is exactly the logic libdtp lacks.
 *
 * The point of this module: DTP judges a transfer "delivered" by a byte COUNTER
 * (bytes_received == size_in_bytes). A byte-counter cannot tell a bit-flipped
 * packet from a good one, so it silently completes corrupt transfers. This tracker
 * replaces that with a verified-completion rule: a transfer is done only when
 * every byte arrived AND every block's SHA-256 matches the sender's manifest, and
 * when it is not done it reports exactly which byte ranges to re-request.
 *
 * Design notes:
 *  - Fire-and-forget is preserved: this adds ONE integrity gate at completion plus
 *    per-block hashes, not a per-packet ACK loop (which would reintroduce RDP's
 *    round-trip cost on the ~4800 bps link).
 *  - Block-level (not whole-file) hashing lets us localize damage to a block and
 *    re-request only that block, instead of re-blasting the whole file.
 */
#ifndef CI_SVU_H
#define CI_SVU_H

#include <stddef.h>
#include <stdint.h>

#define CI_SVU_HASH_LEN 32u   /* SHA-256 digest length, bytes */

typedef enum {
    CI_SVU_INCOMPLETE = 0,     /* one or more byte ranges never arrived        */
    CI_SVU_COMPLETE_VERIFIED,  /* full coverage AND every block hash matches   */
    CI_SVU_CORRUPT             /* full coverage but >= 1 block fails its hash   */
} ci_svu_status_t;

typedef struct {
    uint32_t start;   /* inclusive byte offset */
    uint32_t end;     /* exclusive byte offset */
} ci_svu_interval_t;

typedef struct ci_svu ci_svu_t;

/*
 * Number of blocks for a payload: ceil(total_size / block_size). Returns 0 if
 * either argument is 0. Callers use this to size the manifest buffer.
 */
uint32_t ci_svu_nblocks(uint32_t total_size, uint32_t block_size);

/*
 * Build a manifest (per-block SHA-256 digests) from a known-good source buffer.
 * Writes nblocks * CI_SVU_HASH_LEN bytes into `digests_out` (caller sizes it via
 * ci_svu_nblocks). Returns the number of blocks, or 0 on bad args.
 */
uint32_t ci_svu_manifest(const uint8_t *src, uint32_t total_size,
                         uint32_t block_size, uint8_t *digests_out);

/*
 * Create a receiver expecting `total_size` bytes in blocks of `block_size`, whose
 * per-block SHA-256 digests are `expected` (nblocks * CI_SVU_HASH_LEN bytes). The
 * digests are copied. Returns NULL on OOM, on total_size==0 / block_size==0, or if
 * `nblocks` disagrees with ceil(total_size/block_size).
 */
ci_svu_t *ci_svu_new(uint32_t total_size, uint32_t block_size,
                     const uint8_t *expected, uint32_t nblocks);

void ci_svu_free(ci_svu_t *s);

/*
 * Deliver a data fragment: `len` payload bytes destined for byte `offset`. Writes
 * that overlap an already-covered range overwrite it (last-writer-wins, mirroring
 * the real offset-addressed receiver). Returns 0 on success, -1 if the fragment
 * falls outside [0, total_size) or `s`/`buf` is NULL.
 */
int ci_svu_accept(ci_svu_t *s, uint32_t offset, const uint8_t *buf, size_t len);

/*
 * Bytes covered so far -- the value the OLD receiver compares against total_size
 * to declare "delivered". Exposed so callers (and tests) can show the differential
 * between the byte-counter verdict and the verified verdict.
 */
uint32_t ci_svu_covered(const ci_svu_t *s);

/*
 * Full verdict. When the status is not COMPLETE_VERIFIED and `out`/`max` are given,
 * up to `max` byte ranges needing re-request are written to `out` and their count
 * to `*nout`:
 *   - INCOMPLETE -> the uncovered (missing) byte ranges
 *   - CORRUPT    -> the byte ranges of blocks whose SHA-256 failed
 * Pass out==NULL or max==0 to get only the status. `nout` may be NULL.
 */
ci_svu_status_t ci_svu_verify(ci_svu_t *s, ci_svu_interval_t *out,
                              uint32_t max, uint32_t *nout);

/*
 * The reassembled payload bytes (total_size long, owned by `s`). Only trustworthy
 * once ci_svu_verify() has returned COMPLETE_VERIFIED; before that the buffer may
 * still hold zero-holes or corrupt bytes. Returns NULL if `s` is NULL. This is how
 * a correct uploader hands the finished file to the application -- and the point is
 * that it only does so after the hash gate, never on a byte-counter.
 */
const uint8_t *ci_svu_data(const ci_svu_t *s);

#endif /* CI_SVU_H */

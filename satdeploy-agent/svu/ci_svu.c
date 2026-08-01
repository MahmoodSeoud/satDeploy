/*
 * ci_svu.c - Self-Verifying Uploader core. See ci_svu.h for the rationale.
 *
 * Correctness is load-bearing: this module's whole reason to exist is to NOT lie
 * about delivery the way DTP's byte-counter does, so the coverage accounting and
 * the hash comparison are kept deliberately simple and obviously correct rather
 * than clever. Memory is total_size for the reassembly buffer plus one byte of
 * coverage per payload byte; a flight build would pack coverage to one bit/byte,
 * but for the host-side core and its test set that clarity is worth the bytes.
 */
#include "ci_svu.h"

#include <stdlib.h>
#include <string.h>

#include "ci_sha256.h"

struct ci_svu {
    uint32_t total;         /* total payload bytes expected            */
    uint32_t block;         /* block size for integrity                */
    uint32_t nblocks;       /* ceil(total/block)                       */
    uint32_t covered_count; /* distinct bytes covered so far           */
    uint8_t *data;          /* reassembly buffer, `total` bytes        */
    uint8_t *covered;       /* 1 byte per payload byte: 0 = not yet     */
    uint8_t *expected;      /* nblocks * CI_SVU_HASH_LEN digest bytes  */
};

uint32_t ci_svu_nblocks(uint32_t total_size, uint32_t block_size)
{
    if (total_size == 0u || block_size == 0u) {
        return 0u;
    }
    return (total_size + block_size - 1u) / block_size;
}

/* Hash block `b` of `src` into out[32]. Block b covers [b*block, min(end,total)). */
static void hash_block(const uint8_t *src, uint32_t total, uint32_t block,
                       uint32_t b, uint8_t out[CI_SVU_HASH_LEN])
{
    uint64_t start = (uint64_t)b * block;
    uint64_t end = start + block;
    if (end > total) {
        end = total;
    }
    ci_sha256_t ctx;
    ci_sha256_init(&ctx);
    ci_sha256_update(&ctx, src + start, (size_t)(end - start));
    ci_sha256_final(&ctx, out);
}

uint32_t ci_svu_manifest(const uint8_t *src, uint32_t total_size,
                         uint32_t block_size, uint8_t *digests_out)
{
    if (src == NULL || digests_out == NULL) {
        return 0u;
    }
    uint32_t nblk = ci_svu_nblocks(total_size, block_size);
    if (nblk == 0u) {
        return 0u;
    }
    for (uint32_t b = 0u; b < nblk; b++) {
        hash_block(src, total_size, block_size, b,
                   digests_out + (uint64_t)b * CI_SVU_HASH_LEN);
    }
    return nblk;
}

ci_svu_t *ci_svu_new(uint32_t total_size, uint32_t block_size,
                     const uint8_t *expected, uint32_t nblocks)
{
    uint32_t nblk = ci_svu_nblocks(total_size, block_size);
    if (nblk == 0u || nblk != nblocks || expected == NULL) {
        return NULL;
    }

    ci_svu_t *s = calloc(1u, sizeof(*s));
    if (s == NULL) {
        return NULL;
    }
    s->total = total_size;
    s->block = block_size;
    s->nblocks = nblk;
    s->covered_count = 0u;
    s->data = calloc(1u, total_size);
    s->covered = calloc(1u, total_size);
    s->expected = malloc((size_t)nblk * CI_SVU_HASH_LEN);
    if (s->data == NULL || s->covered == NULL || s->expected == NULL) {
        ci_svu_free(s);
        return NULL;
    }
    memcpy(s->expected, expected, (size_t)nblk * CI_SVU_HASH_LEN);
    return s;
}

void ci_svu_free(ci_svu_t *s)
{
    if (s == NULL) {
        return;
    }
    free(s->data);
    free(s->covered);
    free(s->expected);
    free(s);
}

int ci_svu_accept(ci_svu_t *s, uint32_t offset, const uint8_t *buf, size_t len)
{
    if (s == NULL || buf == NULL) {
        return -1;
    }
    uint64_t end = (uint64_t)offset + len;
    if (end > s->total) {
        return -1; /* fragment runs past the declared payload */
    }
    for (uint64_t i = offset; i < end; i++) {
        if (s->covered[i] == 0u) {
            s->covered[i] = 1u;
            s->covered_count++;
        }
    }
    if (len > 0u) {
        memcpy(s->data + offset, buf, len);
    }
    return 0;
}

uint32_t ci_svu_covered(const ci_svu_t *s)
{
    return (s == NULL) ? 0u : s->covered_count;
}

const uint8_t *ci_svu_data(const ci_svu_t *s)
{
    return (s == NULL) ? NULL : s->data;
}

/* Append interval [start,end) to out[] if room; always advance *nout logically. */
static void push_interval(ci_svu_interval_t *out, uint32_t max, uint32_t *nout,
                          uint32_t start, uint32_t end)
{
    if (out != NULL && *nout < max) {
        out[*nout].start = start;
        out[*nout].end = end;
    }
    (*nout)++;
}

ci_svu_status_t ci_svu_verify(ci_svu_t *s, ci_svu_interval_t *out,
                              uint32_t max, uint32_t *nout)
{
    uint32_t count = 0u;
    ci_svu_status_t status;

    if (s == NULL) {
        if (nout != NULL) {
            *nout = 0u;
        }
        return CI_SVU_INCOMPLETE;
    }

    if (s->covered_count < s->total) {
        /* Holes: coalesce runs of uncovered bytes into missing intervals. */
        status = CI_SVU_INCOMPLETE;
        uint32_t i = 0u;
        while (i < s->total) {
            if (s->covered[i] == 0u) {
                uint32_t run = i;
                while (run < s->total && s->covered[run] == 0u) {
                    run++;
                }
                push_interval(out, max, &count, i, run);
                i = run;
            } else {
                i++;
            }
        }
    } else {
        /* Full coverage: the gate DTP's byte-counter stops at. Now verify blocks.
         * Adjacent failing blocks coalesce into one interval, mirroring the
         * hole-coalescing above -- a contiguous damaged region (e.g. restored
         * partial data whose tail never arrived) is one re-request, not one
         * per block fighting the per-round interval cap. */
        status = CI_SVU_COMPLETE_VERIFIED;
        uint32_t run_start = 0u;
        uint32_t run_end = 0u;   /* run_end != run_start => a run is open */
        for (uint32_t b = 0u; b < s->nblocks; b++) {
            uint8_t got[CI_SVU_HASH_LEN];
            hash_block(s->data, s->total, s->block, b, got);
            if (memcmp(got, s->expected + (uint64_t)b * CI_SVU_HASH_LEN,
                       CI_SVU_HASH_LEN) != 0) {
                status = CI_SVU_CORRUPT;
                uint64_t start = (uint64_t)b * s->block;
                uint64_t end = start + s->block;
                if (end > s->total) {
                    end = s->total;
                }
                if (run_end != run_start && (uint32_t)start == run_end) {
                    run_end = (uint32_t)end;
                } else {
                    if (run_end != run_start) {
                        push_interval(out, max, &count, run_start, run_end);
                    }
                    run_start = (uint32_t)start;
                    run_end = (uint32_t)end;
                }
            }
        }
        if (run_end != run_start) {
            push_interval(out, max, &count, run_start, run_end);
        }
    }

    if (nout != NULL) {
        *nout = count;
    }
    return status;
}

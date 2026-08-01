/*
 * ci_sha256.h - minimal SHA-256 for the integrity oracle (oracle C).
 *
 * Self-contained, no crypto dependency. Used by the verify command to answer the
 * one question the uploader's "delivered" signal cannot be trusted on: did the
 * bytes survive. Correctness is load-bearing here -- a wrong hash makes the tool
 * lie, which is the exact failure it exists to catch -- so this is a by-the-book
 * FIPS 180-4 implementation, checked against published NIST test vectors in the
 * test suite (tests/test_lib.c :: test_sha256).
 */
#ifndef CI_SHA256_H
#define CI_SHA256_H

#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint32_t state[8];
    uint64_t bitlen;
    uint8_t  buf[64];
    size_t   buflen;
} ci_sha256_t;

void ci_sha256_init(ci_sha256_t *c);
void ci_sha256_update(ci_sha256_t *c, const void *data, size_t len);
void ci_sha256_final(ci_sha256_t *c, uint8_t out[32]);

/*
 * Hash a whole file. On success returns 0, writes 64 lowercase hex chars + NUL
 * into hex_out (needs 65 bytes), and (if bytes_out != NULL) the byte count.
 * Returns -1 on open/read error.
 */
int ci_sha256_file(const char *path, char hex_out[65], uint64_t *bytes_out);

#endif /* CI_SHA256_H */

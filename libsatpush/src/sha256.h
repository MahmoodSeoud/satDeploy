/*
 * sha256.h - internal SHA256 helper for libsatpush.
 *
 * Public-domain SHA256 (Brad Conte, crypto-algorithms). Vendored to avoid
 * pulling libcrypto for what is otherwise ~300 lines of pure C. Output
 * matches OpenSSL EVP_sha256 byte-for-byte.
 *
 * Not part of the public libsatpush API. Adopters who need SHA256 should
 * use satpush_sha256_file() from <satpush/satpush.h>, not this header.
 */

#ifndef SATPUSH_SHA256_INTERNAL_H
#define SATPUSH_SHA256_INTERNAL_H

#include <stddef.h>
#include <stdint.h>

#define SHA256_DIGEST_SIZE 32

typedef struct {
    uint8_t  data[64];
    uint32_t datalen;
    uint64_t bitlen;
    uint32_t state[8];
} sha256_ctx;

void sha256_init(sha256_ctx *ctx);
void sha256_update(sha256_ctx *ctx, const uint8_t *data, size_t len);
void sha256_final(sha256_ctx *ctx, uint8_t hash[SHA256_DIGEST_SIZE]);

#endif /* SATPUSH_SHA256_INTERNAL_H */

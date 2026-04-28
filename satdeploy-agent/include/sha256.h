/* Public-domain SHA256 (Brad Conte). Vendored to drop the OpenSSL dependency,
 * which was contributing ~20-40 KB to the linked binary just for SHA256. */

#ifndef SATDEPLOY_SHA256_H
#define SATDEPLOY_SHA256_H

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

#endif

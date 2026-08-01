/*
 * svu_serve.h - the SVU sender/serve loop, decoupled from CSP bring-up.
 *
 * Same split as svu_session.h on the client side: this runs on an ALREADY-
 * INITIALIZED CSP stack. The standalone svu_server binary calls svu_net_init()
 * first; the CSH `svu_put` APM calls nothing (csh already did csp_init + the CAN
 * interface). One serve implementation shared by both.
 */
#ifndef SVU_SERVE_H
#define SVU_SERVE_H

#include <stdint.h>

/* Read a whole file into a malloc'd buffer. Caller frees. NULL on error. */
uint8_t *svu_load_file(const char *path, uint32_t *size_out);

/* Serve `src` (total bytes, split into block_size blocks) over SVU on an already-
 * initialized CSP stack: reply to each meta request with the SHA-256 manifest, then
 * blast the requested byte ranges connectionless. Runs until `*stop` becomes nonzero
 * (pass NULL to run forever, e.g. a standalone daemon); on stop it frees the CTRL port
 * so it can be re-served. Returns -1 only if setup fails (alloc or the port bind). */
int svu_serve_loop(const uint8_t *src, uint32_t total, uint32_t block_size,
                   volatile int *stop);

#endif /* SVU_SERVE_H */

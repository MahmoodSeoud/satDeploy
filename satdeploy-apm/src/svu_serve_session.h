/*
 * svu_serve_session.h - lifecycle for the ground-side SVU server.
 *
 * See svu_serve_session.c for why this replaced the libdtp server thread.
 */
#ifndef SATDEPLOY_SVU_SERVE_SESSION_H
#define SATDEPLOY_SVU_SERVE_SESSION_H

#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>

/* Manifest granularity. Must match the agent's SVU_BLOCK_SIZE: the client asks
 * for a block size in its meta request, but the server's manifest is
 * authoritative, so a mismatch only wastes recovery precision. Kept equal so
 * board cells stay comparable with the host-arm cells in the evaluation. */
#define SVU_SERVE_DEFAULT_BLOCK 4096u

typedef struct {
    uint8_t      *src;        /* artifact, read once up front            */
    uint32_t      total;      /* its size in bytes                       */
    uint32_t      block_size; /* manifest granularity                    */
    volatile int  stop;       /* set to 1 to wind the serve loop down    */
    pthread_t     thread;
    bool          active;
} svu_serve_session_t;

/* Read `path`, compute nothing yet, and start serving it over SVU on the CSP
 * stack csh has already initialized. Returns 0 on success. */
int svu_serve_session_start(svu_serve_session_t *s, const char *path,
                            uint32_t block_size);

/* Stop the serve loop, join the thread, and release the artifact buffer. */
void svu_serve_session_stop(svu_serve_session_t *s);

/* Thread entry; exposed only so the implementation can reference it. */
void *svu_serve_thread(void *arg);

#endif /* SATDEPLOY_SVU_SERVE_SESSION_H */

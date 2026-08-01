/*
 * svu_serve_session.c - the ground end of satdeploy's transfer, on SVU.
 *
 * Replaces the libdtp server thread. The shape is the same -- spawn a server,
 * let the agent pull, reap it -- but three things the DTP path needed go away.
 *
 * No payload registry, and no payload_id. libdtp addressed an artifact by a
 * uint8_t id, so the ground had to hash app names into 256 slots and carry a
 * comment explaining what happens on collision. SVU's meta request names the
 * transfer by session, and the server serves exactly the buffer it was handed,
 * so there is nothing to collide.
 *
 * No re-bind hazard. libdtp's dtp_server_run kept a `static csp_socket_t` and
 * re-bound port 7 unconditionally on every entry, which is why the v1 code had
 * to keep one server alive across a multi-app loop rather than spawning per
 * push. svu_serve_loop frees the CTRL port when it stops, so serving twice in
 * one process is ordinary.
 *
 * The artifact is read into memory once, up front. That is deliberate: the
 * manifest is a SHA-256 per block over the whole file, so the server needs the
 * bytes anyway, and computing it once beats re-reading per re-request.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "svu_serve_session.h"
#include "svu_serve.h"

int svu_serve_session_start(svu_serve_session_t *s, const char *path,
                            uint32_t block_size)
{
    if (s == NULL || path == NULL) {
        return -1;
    }
    memset(s, 0, sizeof(*s));
    s->block_size = (block_size != 0u) ? block_size : SVU_SERVE_DEFAULT_BLOCK;

    s->src = svu_load_file(path, &s->total);
    if (s->src == NULL) {
        fprintf(stderr, "satdeploy: cannot read %s to serve\n", path);
        return -1;
    }
    if (s->total == 0u) {
        fprintf(stderr, "satdeploy: %s is empty, nothing to serve\n", path);
        free(s->src);
        s->src = NULL;
        return -1;
    }

    s->stop = 0;
    if (pthread_create(&s->thread, NULL, svu_serve_thread, s) != 0) {
        fprintf(stderr, "satdeploy: cannot start the SVU server thread\n");
        free(s->src);
        s->src = NULL;
        return -1;
    }

    /* Let the serve loop reach its csp_bind before the agent is told to pull.
     * Unlike the DTP path this is a bind of one port with no static state, so
     * a short settle is enough; the agent retries its ctrl exchange anyway. */
    usleep(100000);
    s->active = true;
    return 0;
}

void *svu_serve_thread(void *arg)
{
    svu_serve_session_t *s = (svu_serve_session_t *)arg;
    (void)svu_serve_loop(s->src, s->total, s->block_size, &s->stop);
    return NULL;
}

void svu_serve_session_stop(svu_serve_session_t *s)
{
    if (s == NULL || !s->active) {
        return;
    }
    s->stop = 1;
    pthread_join(s->thread, NULL);
    free(s->src);
    s->src = NULL;
    s->active = false;
}

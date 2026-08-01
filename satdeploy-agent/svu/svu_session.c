/*
 * svu_session.c - SVU client transfer loop (see svu_session.h). Extracted verbatim
 * from svu_client.c's main so the standalone binary and the CSH APM share one
 * implementation. Does NOT call csp_init / add an interface -- the caller owns the
 * CSP stack.
 */
#include "svu_session.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <csp/csp.h>
#include <csp/csp_debug.h>

#include "svu_proto.h"
#include "ci_svu.h"

/* Receive data packets until the link goes idle for `idle_ms`, feeding ci_svu. */
/* Returns the number of data frames accepted, so the caller can tell a genuinely
 * idle link (0) from a drain that is still making progress. */
static uint32_t drain_data(csp_socket_t *data_sock, ci_svu_t *recv, uint32_t idle_ms,
                           const svu_client_hooks_t *hooks)
{
    csp_packet_t *pkt;
    uint32_t accepted = 0u;
    while ((pkt = csp_recvfrom(data_sock, idle_ms)) != NULL) {
        if (pkt->length > SVU_DATA_HDR) {
            uint32_t off = svu_get32(pkt->data + 0);
            uint32_t len = (uint32_t)pkt->length - SVU_DATA_HDR;
            if (ci_svu_accept(recv, off, pkt->data + SVU_DATA_HDR, len) == 0) {
                /* Persist as we go: a kill at any point leaves the sidecar
                 * holding everything accepted so far, not everything up to
                 * some checkpoint boundary. */
                if (hooks != NULL && hooks->data != NULL) {
                    hooks->data(hooks->ctx, off, pkt->data + SVU_DATA_HDR, len);
                }
                accepted++;
            }
        }
        csp_buffer_free(pkt);
    }
    return accepted;
}

/* One CTRL round: send the request, read the resp header + manifest. */
static int ctrl_exchange(uint16_t server_addr, const svu_req_t *req,
                         uint32_t *total_out, uint32_t *block_out,
                         uint32_t *nblocks_out, uint8_t **manifest_out)
{
    uint8_t buf[SVU_REQ_HDR + (SVU_MAX_INTERVALS * 8u)];
    size_t nbytes = svu_req_encode(req, buf);

    /* Plain CSP connection (no RDP handshake): the first packet opens it on the
     * server's csp_accept. CRC32 still guards the small control messages. */
    csp_conn_t *conn = csp_connect(CSP_PRIO_NORM, server_addr, SVU_CTRL_PORT,
                                   10000, CSP_O_CRC32);
    if (conn == NULL) {
        return -1;
    }
    csp_packet_t *qp = csp_buffer_get(0);
    if (qp == NULL) {
        csp_close(conn);
        return -1;
    }
    memcpy(qp->data, buf, nbytes);
    qp->length = (uint16_t)nbytes;
    csp_send(conn, qp);

    csp_packet_t *hp = csp_read(conn, 10000);
    if (hp == NULL || hp->length < SVU_RESP_HDR ||
        svu_get32(hp->data + 0) != SVU_MAGIC_RESP) {
        if (hp != NULL) {
            csp_buffer_free(hp);
        }
        csp_close(conn);
        return -1;
    }
    uint32_t total = svu_get32(hp->data + 8);
    uint32_t block = svu_get32(hp->data + 12);
    uint32_t nblocks = svu_get32(hp->data + 16);
    csp_buffer_free(hp);

    /* Manifest chunks are offset-tagged ([u32 offset][payload]) and assembled
     * by offset, not arrival position. The ctrl conn is plain CSP: a retried
     * meta exchange can reuse the ephemeral source port while the previous
     * attempt's chunks are still in flight, and positional assembly would
     * interleave the two identical streams into a wrong manifest. By offset,
     * duplicates and stragglers are idempotent. Coverage is tracked per fixed
     * 200-byte stride so only new bytes count toward completion. */
    uint32_t man_bytes = nblocks * CI_SVU_HASH_LEN;
    const uint32_t man_chunk = 200u;
    uint32_t nchunks = (man_bytes + man_chunk - 1u) / man_chunk;
    uint8_t *manifest = malloc(man_bytes);
    uint8_t *have = calloc(1u, nchunks);
    if (manifest == NULL || have == NULL) {
        free(manifest);
        free(have);
        csp_close(conn);
        return -1;
    }
    uint32_t got = 0u;
    while (got < man_bytes) {
        csp_packet_t *mp = csp_read(conn, 10000);
        if (mp == NULL) {
            free(manifest);
            free(have);
            csp_close(conn);
            return -1;
        }
        if (mp->length > 4u) {
            uint32_t off = svu_get32(mp->data + 0);
            uint32_t len = (uint32_t)mp->length - 4u;
            if (off < man_bytes && (off % man_chunk) == 0u &&
                len <= man_bytes - off) {
                uint32_t idx = off / man_chunk;
                if (have[idx] == 0u) {
                    memcpy(manifest + off, mp->data + 4, len);
                    have[idx] = 1u;
                    got += len;
                }
            }
        }
        csp_buffer_free(mp);
    }
    free(have);
    csp_close(conn);

    *total_out = total;
    *block_out = block;
    *nblocks_out = nblocks;
    *manifest_out = manifest;
    return 0;
}

int svu_client_run(uint16_t server_addr, uint32_t block_size, uint32_t mtu,
                   uint32_t max_rounds, const char *outfile)
{
    return svu_client_run_hooked(server_addr, block_size, mtu, max_rounds,
                                 outfile, NULL);
}

int svu_client_run_hooked(uint16_t server_addr, uint32_t block_size, uint32_t mtu,
                          uint32_t max_rounds, const char *outfile,
                          const svu_client_hooks_t *hooks)
{
    /* Connectionless RX socket for the fire-and-forget data blast: needs both
     * CSP_SO_CONN_LESS and csp_listen() (which allocates the rx_queue). */
    csp_socket_t data_sock = {0};
    data_sock.opts = CSP_SO_CONN_LESS;
    csp_bind(&data_sock, SVU_DATA_PORT);
    csp_listen(&data_sock, 10);

    ci_svu_t *recv = NULL;
    uint32_t total = 0u, block = 0u, nblocks = 0u;
    ci_svu_interval_t ivs[SVU_MAX_INTERVALS];
    uint32_t nreq = 0u; /* 0 on the first round => request the whole file */
    int result = -1;    /* single cleanup at `done` frees the reusable data socket */

    /* A prior pass's partial data, if the caller kept any. With a seed in
     * hand the first request carries one empty interval instead of the
     * whole-file request: the server sends the manifest and blasts nothing,
     * and the first verify decides what is actually missing. */
    uint8_t *seed = NULL;
    uint32_t seed_total = 0u;
    if (hooks != NULL && hooks->restore != NULL) {
        seed = hooks->restore(hooks->ctx, &seed_total);
        if (seed != NULL) {
            nreq = 1u;
            ivs[0].start = 0u;
            ivs[0].end = 0u;
        }
    }

    ci_svu_status_t st = CI_SVU_INCOMPLETE;
    uint32_t round = 0u;
    while (round < max_rounds) {
        round++;

        svu_req_t req;
        memset(&req, 0, sizeof(req));
        req.magic = SVU_MAGIC_REQ;
        req.session_id = 1u;
        req.mtu = mtu;
        req.block_size = block_size;
        req.client_addr = 0u; /* server replies via the accepted conn; unused */
        req.nof_intervals = nreq;
        for (uint32_t i = 0u; i < nreq; i++) {
            req.intervals[i].start = ivs[i].start;
            req.intervals[i].end = ivs[i].end;
        }

        uint8_t *manifest = NULL;
        uint32_t t2 = 0u, b2 = 0u, nb2 = 0u;
        /* The control channel is a plain CSP conn (no RDP), so a single dropped or
         * late control packet would otherwise abort the whole transfer. Retry a few
         * times: covers a lossy link's control-packet loss and a fresh ZMQ link's
         * slow-joiner (SUB/PUB not yet connected on the first send). */
        int ctrl_ok = -1;
        for (int attempt = 0; attempt < 8; attempt++) {
            ctrl_ok = ctrl_exchange(server_addr, &req, &t2, &b2, &nb2, &manifest);
            if (ctrl_ok == 0) {
                break;
            }
            usleep(500000); /* 500 ms between attempts */
        }
        if (ctrl_ok != 0) {
            csp_print("svu: ctrl exchange failed after retries (round %u)\n", round);
            goto done;
        }
        if (recv == NULL) {
            total = t2;
            block = b2;
            nblocks = nb2;
            recv = ci_svu_new(total, block, manifest, nblocks);
            if (recv == NULL) {
                csp_print("svu: ci_svu_new failed\n");
                free(manifest);
                goto done; /* seed freed at done */
            }
            if (seed != NULL) {
                /* Feed the whole restored buffer in one accept. Coverage is
                 * deliberately not restored: bytes that never arrived are
                 * zero-holes that fail their block hash and get re-requested,
                 * and a hole inside a genuinely all-zero block verifies --
                 * correctly, because content is what the manifest attests. */
                if (seed_total == total) {
                    ci_svu_accept(recv, 0u, seed, total);
                    csp_print("svu: restored %u bytes from a prior pass\n",
                              seed_total);
                } else {
                    csp_print("svu: discarding stale sidecar (%u bytes, "
                              "artifact is %u)\n", seed_total, total);
                }
                free(seed);
                seed = NULL;
            }
            if (hooks != NULL && hooks->meta != NULL) {
                hooks->meta(hooks->ctx, total, block);
            }
        }
        free(manifest);

        /* Drain until the link is genuinely quiet. Verifying while frames are
         * still arriving (server mid-blast, or a host stall longer than one
         * idle window) would burn a recovery round on data already in flight,
         * making the rounds metric sensitive to host load rather than to loss.
         * Re-verify as long as a drain made progress; a re-request goes out
         * only when a full idle window passed with nothing new AND gaps remain. */
        uint32_t nout = 0u;
        uint32_t accepted = 0u;
        for (;;) {
            uint32_t got = drain_data(&data_sock, recv, 2000, hooks);
            accepted += got;
            st = ci_svu_verify(recv, ivs, SVU_MAX_INTERVALS, &nout);
            if (st == CI_SVU_COMPLETE_VERIFIED || got == 0u) {
                break;
            }
        }
        if (st == CI_SVU_COMPLETE_VERIFIED) {
            break;
        }
        nreq = (nout < SVU_MAX_INTERVALS) ? nout : SVU_MAX_INTERVALS;
        csp_print("svu: round %u -> %s (%u frame(s) accepted, %u bad range(s), "
                  "first [%u,%u)), re-requesting %u\n",
                  round, (st == CI_SVU_CORRUPT) ? "CORRUPT" : "INCOMPLETE",
                  accepted, nout, (nreq > 0u) ? ivs[0].start : 0u,
                  (nreq > 0u) ? ivs[0].end : 0u, nreq);
    }

    if (st != CI_SVU_COMPLETE_VERIFIED) {
        csp_print("svu: gave up after %u rounds (status %d)\n", round, st);
        goto done;
    }

    FILE *fp = fopen(outfile, "wb");
    if (fp == NULL) {
        csp_print("svu: cannot open '%s' for writing\n", outfile);
        goto done;
    }
    fwrite(ci_svu_data(recv), 1, total, fp);
    fclose(fp);
    csp_print("svu: VERIFIED %u bytes in %u round(s) -> %s\n", total, round, outfile);
    if (hooks != NULL && hooks->complete != NULL) {
        hooks->complete(hooks->ctx);
    }
    result = 0;

done:
    free(seed); /* NULL once consumed; owned here if ctrl failed first */
    ci_svu_free(recv);
    csp_socket_close(&data_sock); /* free the port so a persistent daemon can reuse it */
    return result;
}

/*
 * svu_serve.c - SVU sender/serve loop (see svu_serve.h). Extracted verbatim from
 * svu_server.c's main so the standalone binary and the CSH `svu_put` APM share one
 * implementation. Does NOT call csp_init / add an interface -- the caller owns the
 * CSP stack.
 */
#include "svu_serve.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <csp/csp.h>
#include <csp/csp_debug.h>

#include "svu_proto.h"
#include "ci_svu.h"

uint8_t *svu_load_file(const char *path, uint32_t *size_out)
{
    FILE *fp = fopen(path, "rb");
    if (fp == NULL) {
        return NULL;
    }
    if (fseek(fp, 0, SEEK_END) != 0) {
        fclose(fp);
        return NULL;
    }
    long n = ftell(fp);
    if (n < 0) {
        fclose(fp);
        return NULL;
    }
    rewind(fp);
    uint8_t *buf = malloc((size_t)n);
    if (buf == NULL) {
        fclose(fp);
        return NULL;
    }
    if (fread(buf, 1, (size_t)n, fp) != (size_t)n) {
        free(buf);
        fclose(fp);
        return NULL;
    }
    fclose(fp);
    *size_out = (uint32_t)n;
    return buf;
}

/* Optional inter-packet delay for the blast and the manifest send. Real link
 * drivers (KISS, CAN, a radio) pace the sender through backpressure; ZMQ PUB
 * does not -- beyond its high-water mark it silently DROPS, so an unpaced
 * multi-MB blast on the dev loopback loses most of its packets and recovery
 * crawls through capped re-request rounds. SVU_BLAST_PACE_US=<microseconds
 * between packets> models a paced link there; unset/0 keeps full speed. */
static uint32_t blast_pace_us(void)
{
    static int cached = -1;
    if (cached < 0) {
        const char *s = getenv("SVU_BLAST_PACE_US");
        long v = (s != NULL) ? strtol(s, NULL, 10) : 0;
        cached = (v > 0 && v <= 1000000) ? (int)v : 0;
    }
    return (uint32_t)cached;
}

/* Send the manifest (nblocks * 32 bytes) over the ctrl conn in chunks.
 *
 * Each chunk is offset-tagged ([u32 offset][payload]) for the same reason
 * data packets are: the ctrl conn is plain CSP, and a failed+retried meta
 * exchange can reuse the ephemeral source port while the failed attempt's
 * chunks are still in flight. Positional assembly would interleave the two
 * streams into a full-length but wrong manifest -- which then poisons every
 * recovery round, since the manifest is consumed once. Offset-tagged chunks
 * make duplicates and stragglers idempotent. */
static void send_manifest(csp_conn_t *conn, const uint8_t *manifest, uint32_t bytes)
{
    const uint32_t chunk = 200u;
    uint32_t pace = blast_pace_us();
    for (uint32_t off = 0u; off < bytes; off += chunk) {
        uint32_t len = (off + chunk <= bytes) ? chunk : (bytes - off);
        csp_packet_t *pkt = csp_buffer_get(0);
        if (pkt == NULL) {
            return;
        }
        svu_put32(pkt->data + 0, off);
        memcpy(pkt->data + 4, manifest + off, len);
        pkt->length = (uint16_t)(4u + len);
        csp_send(conn, pkt);
        if (pace != 0u) {
            usleep(pace);
        }
    }
}

/* Blast one byte interval [start,end) as fire-and-forget data packets. */
static void blast_interval(uint16_t client_addr, uint32_t session, uint32_t mtu,
                           const uint8_t *src, uint32_t start, uint32_t end)
{
    uint32_t pace = blast_pace_us();
    uint32_t pay = (mtu > SVU_DATA_HDR) ? (mtu - SVU_DATA_HDR) : 1u;
    for (uint32_t off = start; off < end; off += pay) {
        uint32_t len = (off + pay <= end) ? pay : (end - off);
        csp_packet_t *pkt = csp_buffer_get(0);
        if (pkt == NULL) {
            return;
        }
        svu_put32(pkt->data + 0, off);
        svu_put32(pkt->data + 4, session);
        memcpy(pkt->data + SVU_DATA_HDR, src + off, len);
        pkt->length = (uint16_t)(SVU_DATA_HDR + len);
        /* fire-and-forget: no RDP, no CRC option -- the corrected DTP data path */
        csp_sendto(CSP_PRIO_NORM, client_addr, SVU_DATA_PORT, SVU_DATA_PORT, 0, pkt);
        if (pace != 0u) {
            usleep(pace);
        }
    }
}

int svu_serve_loop(const uint8_t *src, uint32_t total, uint32_t block_size,
                   volatile int *stop)
{
    uint32_t nblocks = ci_svu_nblocks(total, block_size);
    uint8_t *manifest = malloc((size_t)nblocks * CI_SVU_HASH_LEN);
    if (manifest == NULL) {
        return -1;
    }
    ci_svu_manifest(src, total, block_size, manifest);

    csp_socket_t ctrl = {0};
    if (csp_bind(&ctrl, SVU_CTRL_PORT) != CSP_ERR_NONE) {
        free(manifest);
        return -1;
    }
    csp_listen(&ctrl, 10);

    while (1) {
        csp_conn_t *conn = csp_accept(&ctrl, 2000);
        if (conn == NULL) {
            if (stop != NULL && *stop != 0) {
                break;
            }
            continue;
        }
        csp_packet_t *pkt = csp_read(conn, 5000);
        if (pkt == NULL) {
            csp_close(conn);
            continue;
        }
        svu_req_t req;
        if (svu_req_decode(pkt->data, pkt->length, &req) != 0) {
            csp_buffer_free(pkt);
            csp_close(conn);
            continue;
        }
        csp_buffer_free(pkt);

        uint32_t mtu = (req.mtu > SVU_DATA_HDR) ? req.mtu : 256u;
        /* Learn the client's address from the CONNECTION, not a self-reported field. */
        uint16_t client_addr = (uint16_t)csp_conn_src(conn);

        /* meta response header + manifest, over the reliable conn */
        csp_packet_t *resp = csp_buffer_get(0);
        if (resp != NULL) {
            svu_put32(resp->data + 0, SVU_MAGIC_RESP);
            svu_put32(resp->data + 4, req.session_id);
            svu_put32(resp->data + 8, total);
            svu_put32(resp->data + 12, block_size);
            svu_put32(resp->data + 16, nblocks);
            resp->length = (uint16_t)SVU_RESP_HDR;
            csp_send(conn, resp);
            send_manifest(conn, manifest, nblocks * CI_SVU_HASH_LEN);
        }
        csp_close(conn);

        /* blast: 0 intervals means "the whole file" (initial request) */
        uint32_t blasted = 0u;
        if (req.nof_intervals == 0u) {
            blast_interval(client_addr, req.session_id, mtu, src, 0u, total);
            blasted = total;
        } else {
            for (uint32_t i = 0u; i < req.nof_intervals; i++) {
                uint32_t s = req.intervals[i].start;
                uint32_t e = req.intervals[i].end;
                if (e > total) {
                    e = total;
                }
                if (s < e) {
                    blast_interval(client_addr, req.session_id, mtu, src, s, e);
                    blasted += e - s;
                }
            }
        }
        csp_print("svu-server: served a request from node %u (%u intervals, "
                  "%u bytes blasted)\n",
                  client_addr, req.nof_intervals, blasted);
    }

    csp_socket_close(&ctrl); /* free the port so this file (or the next) can be re-served */
    free(manifest);
    return 0;
}

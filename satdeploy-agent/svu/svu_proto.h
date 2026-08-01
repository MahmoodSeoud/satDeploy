/*
 * svu_proto.h - the Self-Verifying Uploader wire format over CSP.
 *
 * This is DTP's transfer shape with the one thing DTP lacks bolted on: an
 * integrity manifest and a verified-completion handshake. Two ports, mirroring
 * DTP's split:
 *   SVU_CTRL_PORT - reliable meta handshake (RDP + CRC32): request -> manifest.
 *   SVU_DATA_PORT - connectionless, fire-and-forget bulk data (no per-packet CRC),
 *                   so we keep DTP's throughput on a lossy half-duplex link.
 *
 * The data packet is byte-for-byte DTP's: [u32 offset][u32 session_id][payload].
 * That is deliberate -- SVU is the corrected DTP, not a new transport. All wire
 * integers are little-endian (matching lib/ci_dtp's observed offset encoding).
 */
#ifndef SVU_PROTO_H
#define SVU_PROTO_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

/* Service ports MUST be <= the build's csp port_max_bind (16 here); ports above it
 * are CSP's dynamic/ephemeral source-port range (the bench uses 17-24), so binding
 * a service there collides with connection source ports and packets never reach
 * csp_accept. Ports must be <= CSP_PORT_MAX_BIND (16 here). Avoid what the DISCO/csh
 * stack already binds: 0-6 (CSP services), 7/8/13 (DTP), 10 (PARAM_PORT_SERVER),
 * 12 (PARAM_PORT_LIST -- csh's vmem_server binds it, so a data socket there returns
 * CSP_ERR_USED and the blast is swallowed), 14 (vmem). 11 (ctrl) and 9 (data) are
 * free, low, and bindable in a live csh. */
#define SVU_CTRL_PORT   11u    /* meta handshake                          */
#define SVU_DATA_PORT    9u    /* connectionless bulk data                */
#define SVU_PUSH_PORT   15u    /* daemon announce: source hands over the dest path */
#define SVU_DATA_HDR     8u    /* [u32 offset][u32 session_id]            */
#define SVU_MAGIC_REQ   0x51555653u /* "SVUQ" */
#define SVU_MAGIC_RESP  0x52555653u /* "SVUR" */
#define SVU_MAX_INTERVALS 32u  /* intervals carried per meta request      */

/* Fixed byte sizes on the wire (explicit, not struct-packed). */
#define SVU_REQ_HDR    24u     /* magic,session,mtu,block,nof_intervals,client_addr */
#define SVU_RESP_HDR   20u     /* magic,session,total_size,block_size,nblocks       */

/* --- little-endian 32-bit accessors --- */
static inline void svu_put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
    p[2] = (uint8_t)((v >> 16) & 0xFFu);
    p[3] = (uint8_t)((v >> 24) & 0xFFu);
}

static inline uint32_t svu_get32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/* Byte interval [start,end) on the wire (mirrors ci_svu_interval_t). */
typedef struct {
    uint32_t start;
    uint32_t end;
} svu_wire_interval_t;

/* A parsed meta request. */
typedef struct {
    uint32_t magic;
    uint32_t session_id;
    uint32_t mtu;
    uint32_t block_size;
    uint32_t nof_intervals;
    uint32_t client_addr;
    svu_wire_interval_t intervals[SVU_MAX_INTERVALS];
} svu_req_t;

/* Serialize a meta request into buf (>= SVU_REQ_HDR + nof*8). Returns byte count. */
static inline size_t svu_req_encode(const svu_req_t *r, uint8_t *buf)
{
    uint32_t n = r->nof_intervals;
    if (n > SVU_MAX_INTERVALS) {
        n = SVU_MAX_INTERVALS;
    }
    svu_put32(buf + 0, r->magic);
    svu_put32(buf + 4, r->session_id);
    svu_put32(buf + 8, r->mtu);
    svu_put32(buf + 12, r->block_size);
    svu_put32(buf + 16, n);
    svu_put32(buf + 20, r->client_addr);
    for (uint32_t i = 0u; i < n; i++) {
        svu_put32(buf + SVU_REQ_HDR + (i * 8u) + 0u, r->intervals[i].start);
        svu_put32(buf + SVU_REQ_HDR + (i * 8u) + 4u, r->intervals[i].end);
    }
    return (size_t)SVU_REQ_HDR + ((size_t)n * 8u);
}

/* Parse a meta request from buf/len. Returns 0 on success, -1 if malformed. */
static inline int svu_req_decode(const uint8_t *buf, size_t len, svu_req_t *r)
{
    if (buf == NULL || r == NULL || len < SVU_REQ_HDR) {
        return -1;
    }
    memset(r, 0, sizeof(*r));
    r->magic = svu_get32(buf + 0);
    r->session_id = svu_get32(buf + 4);
    r->mtu = svu_get32(buf + 8);
    r->block_size = svu_get32(buf + 12);
    r->nof_intervals = svu_get32(buf + 16);
    r->client_addr = svu_get32(buf + 20);
    if (r->magic != SVU_MAGIC_REQ || r->nof_intervals > SVU_MAX_INTERVALS) {
        return -1;
    }
    if (len < (size_t)SVU_REQ_HDR + ((size_t)r->nof_intervals * 8u)) {
        return -1;
    }
    for (uint32_t i = 0u; i < r->nof_intervals; i++) {
        r->intervals[i].start = svu_get32(buf + SVU_REQ_HDR + (i * 8u) + 0u);
        r->intervals[i].end = svu_get32(buf + SVU_REQ_HDR + (i * 8u) + 4u);
    }
    return 0;
}

/* --- push announce framing (the `svu` client -> daemon dest-path handover) --- */
#define SVU_PUSH_MAGIC 0x53565550u /* [magic][u32 mode][path]; absent = legacy path-only */

/* Encode a push announce into buf. When mode == 0 (no -p) we emit the LEGACY path-only
 * announce, so a new client still interoperates with an old daemon (and bootstraps the
 * new daemon onto a board). When mode != 0 (-p) we emit [u32 SVU_PUSH_MAGIC][u32 mode]
 * [path]; that requires a daemon new enough to decode it. Returns the byte count. */
static inline size_t svu_announce_encode(uint32_t mode, const char *path,
                                         uint8_t *buf, size_t bufcap)
{
    size_t plen = strlen(path);
    if (mode == 0u) {
        if (plen > bufcap) {
            plen = bufcap;
        }
        memcpy(buf, path, plen);
        return plen;
    }
    if (plen + 8u > bufcap) {
        plen = (bufcap > 8u) ? (bufcap - 8u) : 0u;
    }
    svu_put32(buf + 0, SVU_PUSH_MAGIC);
    svu_put32(buf + 4, mode);
    memcpy(buf + 8, path, plen);
    return 8u + plen;
}

/* Decode a push announce. New format yields the sent mode; a legacy (no-magic)
 * announce is treated as path-only with mode 0, so an old client still works. */
static inline void svu_announce_decode(const uint8_t *buf, size_t len,
                                       uint32_t *mode, char *path, size_t pathcap)
{
    const uint8_t *psrc;
    size_t plen;
    if (len >= 8u && svu_get32(buf) == SVU_PUSH_MAGIC) {
        *mode = svu_get32(buf + 4);
        psrc = buf + 8;
        plen = len - 8u;
    } else {
        *mode = 0u; /* legacy: the whole packet is the path */
        psrc = buf;
        plen = len;
    }
    if (pathcap == 0u) {
        return;
    }
    if (plen >= pathcap) {
        plen = pathcap - 1u;
    }
    memcpy(path, psrc, plen);
    path[plen] = '\0';
}

#endif /* SVU_PROTO_H */

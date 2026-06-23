/**
 * dtp-pull - raw DTP transport reference for the thesis evaluation.
 *
 * This is the "raw DTP" mechanism of RQ2: DTP as libdtp gives it to you, with
 * NONE of the uploader layer that DIPP and satDeploy add. It does exactly one
 * dtp_start_transfer() over a single prepared session with resume disabled, and
 * writes each received data fragment straight to the output file at its byte
 * offset. There is:
 *
 *   - no selective-repeat retry loop (that is satpush_pull's round loop),
 *   - no cross-session resume sidecar (resume = false),
 *   - no end-to-end SHA-256 or per-packet CRC32 (no integrity layer at all),
 *   - no protobuf metadata wrapper (that is DIPP).
 *
 * What lands on disk is therefore whatever the data plane (CSP port 8) actually
 * delivered in one pass. A dropped middle fragment leaves a zero hole; a dropped
 * tail fragment leaves the file short. The external `sha256sum` oracle is the
 * authority on whether the artifact is intact -- this tool deliberately does not
 * check, so it stands as the unguarded transport baseline against which the
 * uploaders' recovery and integrity features are measured.
 *
 * CSP bring-up (interface, address, router) mirrors satdeploy-agent/src/main.c
 * verbatim so the reference runs on the same node as the agent under test.
 *
 * Usage:
 *   dtp-pull -o OUT [-i TYPE] [-p DEV] [-a ADDR] [-m MASK]
 *            -s SERVER [-I PAYLOAD_ID] [-M MTU] [-T THROUGHPUT] [-t TIMEOUT]
 *
 * Exit status: 0 = DTP_OK (transfer ran to completion), 1 = setup/IO failure,
 * 2 = DTP error or cancel (see stderr for dtp_strerror). Note that 0 does NOT
 * mean the file is intact -- that is the sha256sum oracle's call, by design.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <pthread.h>
#include <unistd.h>
#include <getopt.h>
#include <stdint.h>
#include <stdbool.h>

#include <csp/csp.h>
#include <csp/csp_iflist.h>
#include <csp/csp_rtable.h>
#include <csp/interfaces/csp_if_zmqhub.h>
#include <csp/drivers/can_socketcan.h>
#include <csp/drivers/usart.h>

#include "dtp/dtp.h"

#define DEFAULT_NODE_ADDR   5425
#define DEFAULT_INTERFACE   "CAN"
#define DEFAULT_ZMQ_HOST    "localhost"
#define DEFAULT_CAN_DEVICE  "can0"
#define DEFAULT_KISS_DEVICE "/dev/ttyS1"
#define DEFAULT_BAUDRATE    115200
#define DEFAULT_NETMASK     8

/* DTP defaults: a single MTU-256 pass, paced externally by the injector. The
 * throughput here is libdtp's server-side send-rate cap; the CSP-aware pacing
 * that converts bus time into a pass budget is applied by the instrument, not
 * by this client. */
#define DEFAULT_MTU         256
#define DEFAULT_THROUGHPUT  720000   /* B/s; DTP_DEFAULT_THROUGHPUT */
#define DEFAULT_TIMEOUT_S   30
#define DEFAULT_PAYLOAD_ID  0

volatile int running = 1;

static void signal_handler(int sig) {
    (void)sig;
    running = 0;
}

static void *router_task(void *param) {
    (void)param;
    while (running) {
        csp_route_work();
    }
    return NULL;
}

/* on_data_packet sink: write the fragment at its byte offset. No bitmap, no
 * progress callback, no resume bookkeeping -- the raw transport baseline. */
typedef struct {
    FILE    *fp;
    uint64_t bytes_written;
    int      error;
} pull_sink_t;

static bool on_data(dtp_t *session, csp_packet_t *packet) {
    pull_sink_t *sink = (pull_sink_t *)dtp_session_get_user_ctx(session);
    if (sink == NULL || sink->fp == NULL) {
        return false;
    }
    if (packet == NULL || packet->length == 0) {
        return true;
    }
    /* dtp_get_data_info() needs the 8-byte data-plane header (2 x uint32);
     * guard against the underflow the hardened monitor also guards. */
    if (packet->length < 2 * sizeof(uint32_t)) {
        return true;
    }

    dtp_on_data_info_t info = dtp_get_data_info(session, packet);

    if (fseek(sink->fp, info.data_offset, SEEK_SET) != 0) {
        sink->error = -1;
        return false;
    }
    size_t written = fwrite(info.data, 1, info.data_length, sink->fp);
    if (written != info.data_length) {
        sink->error = -1;
        return false;
    }
    sink->bytes_written += written;
    return true;
}

static csp_iface_t *iface_init(const char *interface, const char *port,
                                int node_addr, int netmask, uint32_t baudrate,
                                uint16_t zmq_sub_port, uint16_t zmq_pub_port) {
    csp_iface_t *iface = NULL;

    if (strcmp(interface, "ZMQ") == 0) {
        int result = csp_zmqhub_init_filter2("zmq", port, node_addr, netmask,
                                             true, &iface, NULL,
                                             zmq_sub_port, zmq_pub_port);
        if (result != CSP_ERR_NONE || iface == NULL) {
            fprintf(stderr, "Error: Failed to initialize ZMQ interface\n");
            return NULL;
        }
        iface->name = "zmq";
    }
    else if (strcmp(interface, "CAN") == 0) {
        int error = csp_can_socketcan_open_and_add_interface(
            port, "CAN", node_addr, 1000000, 0, &iface);
        if (error != CSP_ERR_NONE) {
            fprintf(stderr, "Error: Failed to add CAN interface [%s], error: %d\n", port, error);
            return NULL;
        }
        iface->name = "can";
    }
    else if (strcmp(interface, "KISS") == 0) {
        csp_usart_conf_t conf = {
            .device = port, .baudrate = baudrate,
            .databits = 8, .stopbits = 1, .paritysetting = 0,
        };
        int error = csp_usart_open_and_add_kiss_interface(
            &conf, CSP_IF_KISS_DEFAULT_NAME, node_addr, &iface);
        if (error != CSP_ERR_NONE) {
            fprintf(stderr, "Error: Failed to add KISS interface [%s], error: %d\n", port, error);
            return NULL;
        }
        iface->name = "kiss";
    }
    else {
        fprintf(stderr, "Error: Unknown interface type '%s'\n", interface);
        return NULL;
    }

    iface->addr = node_addr;
    iface->netmask = netmask;
    csp_rtable_set(0, 0, iface, CSP_NO_VIA_ADDRESS);
    csp_iflist_add(iface);
    return iface;
}

static void print_usage(const char *prog) {
    printf("Usage: %s -o OUT -s SERVER [OPTIONS]\n", prog);
    printf("\nRequired:\n");
    printf("  -o, --out FILE         Output file for the received artifact\n");
    printf("  -s, --server ADDR      DTP server CSP node address (serves the payload)\n");
    printf("\nDTP options:\n");
    printf("  -I, --payload-id N     Payload id to pull (default: %d)\n", DEFAULT_PAYLOAD_ID);
    printf("  -M, --mtu N            DTP MTU (default: %d)\n", DEFAULT_MTU);
    printf("  -T, --throughput N     Max throughput B/s (default: %d)\n", DEFAULT_THROUGHPUT);
    printf("  -t, --timeout N        Idle timeout seconds (default: %d)\n", DEFAULT_TIMEOUT_S);
    printf("\nCSP options (mirror satdeploy-agent):\n");
    printf("  -i, --interface TYPE   ZMQ, CAN, KISS (default: %s)\n", DEFAULT_INTERFACE);
    printf("  -p, --port DEV         Port/device (default: %s for CAN)\n", DEFAULT_CAN_DEVICE);
    printf("  -a, --address ADDR     This node's CSP address (default: %d)\n", DEFAULT_NODE_ADDR);
    printf("  -m, --netmask MASK     CSP netmask (default: %d)\n", DEFAULT_NETMASK);
    printf("  -h, --help             Show this help\n");
}

int main(int argc, char *argv[]) {
    static struct option long_options[] = {
        {"out",        required_argument, 0, 'o'},
        {"server",     required_argument, 0, 's'},
        {"payload-id", required_argument, 0, 'I'},
        {"mtu",        required_argument, 0, 'M'},
        {"throughput", required_argument, 0, 'T'},
        {"timeout",    required_argument, 0, 't'},
        {"interface",  required_argument, 0, 'i'},
        {"port",       required_argument, 0, 'p'},
        {"address",    required_argument, 0, 'a'},
        {"netmask",    required_argument, 0, 'm'},
        {"help",       no_argument,       0, 'h'},
        {0, 0, 0, 0}
    };

    const char *out_path  = NULL;
    int      server       = -1;
    int      payload_id   = DEFAULT_PAYLOAD_ID;
    int      mtu          = DEFAULT_MTU;
    uint32_t throughput   = DEFAULT_THROUGHPUT;
    int      timeout_s    = DEFAULT_TIMEOUT_S;
    char    *interface    = DEFAULT_INTERFACE;
    char    *port         = NULL;
    int      node_addr    = DEFAULT_NODE_ADDR;
    int      netmask      = DEFAULT_NETMASK;
    uint32_t baudrate     = DEFAULT_BAUDRATE;

    int opt;
    while ((opt = getopt_long(argc, argv, "o:s:I:M:T:t:i:p:a:m:h", long_options, NULL)) != -1) {
        switch (opt) {
            case 'o': out_path = optarg; break;
            case 's': server = atoi(optarg); break;
            case 'I': payload_id = atoi(optarg); break;
            case 'M': mtu = atoi(optarg); break;
            case 'T': throughput = (uint32_t)strtoul(optarg, NULL, 10); break;
            case 't': timeout_s = atoi(optarg); break;
            case 'i': interface = optarg; break;
            case 'p': port = optarg; break;
            case 'a': node_addr = atoi(optarg); break;
            case 'm': netmask = atoi(optarg); break;
            case 'h': print_usage(argv[0]); return 0;
            default:  print_usage(argv[0]); return 1;
        }
    }

    if (out_path == NULL || server < 0) {
        fprintf(stderr, "Error: -o OUT and -s SERVER are required\n\n");
        print_usage(argv[0]);
        return 1;
    }
    if (mtu <= 8) {
        fprintf(stderr, "Error: MTU must exceed the 8-byte DTP data header\n");
        return 1;
    }

    if (port == NULL) {
        if (strcmp(interface, "ZMQ") == 0)      port = DEFAULT_ZMQ_HOST;
        else if (strcmp(interface, "CAN") == 0) port = DEFAULT_CAN_DEVICE;
        else if (strcmp(interface, "KISS") == 0) port = DEFAULT_KISS_DEVICE;
    }

    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);

    /* CSP init: identical config to satdeploy-agent so the two nodes interop. */
    csp_conf.hostname = "dtp-pull";
    csp_conf.model    = "DTPREF";
    csp_conf.revision = "1";
    csp_conf.version  = 2;
    csp_conf.dedup    = CSP_DEDUP_OFF;
    csp_init();

    csp_iface_t *iface = iface_init(interface, port, node_addr, netmask, baudrate,
                                     CSP_ZMQPROXY_SUBSCRIBE_PORT, CSP_ZMQPROXY_PUBLISH_PORT);
    if (iface == NULL) {
        return 1;
    }

    csp_bind_callback(csp_service_handler, CSP_ANY);

    pthread_t router_handle;
    pthread_create(&router_handle, NULL, &router_task, NULL);

    FILE *fp = fopen(out_path, "wb");
    if (fp == NULL) {
        fprintf(stderr, "Error: cannot open output '%s'\n", out_path);
        running = 0;
        pthread_join(router_handle, NULL);
        return 1;
    }

    pull_sink_t sink = { .fp = fp, .bytes_written = 0, .error = 0 };

    /* Raw DTP session: resume = false, keep_alive = 0, fixed session id. */
    dtp_t *session = dtp_prepare_session((uint32_t)server, 0x900dface, throughput,
                                         (uint8_t)timeout_s, (uint8_t)payload_id,
                                         NULL, (uint16_t)mtu, false /* resume */, 0);
    if (session == NULL) {
        fprintf(stderr, "Error: dtp_prepare_session failed\n");
        fclose(fp);
        running = 0;
        pthread_join(router_handle, NULL);
        return 2;
    }

    dtp_session_set_user_ctx(session, &sink);

    dtp_opt_session_hooks_cfg hooks = { 0 };
    hooks.on_data_packet = on_data;
    dtp_params hp; hp.hooks = hooks;
    dtp_set_opt(session, DTP_SESSION_HOOKS_CFG, &hp);

    fprintf(stderr, "dtp-pull: pulling payload %d from node %d (mtu=%d) -> %s\n",
            payload_id, server, mtu, out_path);

    /* Exactly one pass. No retry loop -- that is the uploader layer. */
    dtp_result rc = dtp_start_transfer(session);

    fflush(fp);
    fclose(fp);

    int exit_code;
    if (sink.error != 0) {
        fprintf(stderr, "dtp-pull: write error during transfer\n");
        exit_code = 1;
    } else if (rc == DTP_OK) {
        /* Ran to completion. Whether the bytes are intact is the sha256sum
         * oracle's call, not ours. */
        fprintf(stderr, "dtp-pull: DTP_OK, %llu bytes written\n",
                (unsigned long long)sink.bytes_written);
        exit_code = 0;
    } else {
        fprintf(stderr, "dtp-pull: transfer ended rc=%d errno=%s, %llu bytes written\n",
                rc, dtp_strerror(dtp_errno(session)),
                (unsigned long long)sink.bytes_written);
        exit_code = 2;
    }

    dtp_release_session(session);

    running = 0;
    pthread_join(router_handle, NULL);
    return exit_code;
}

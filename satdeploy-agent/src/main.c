/**
 * satdeploy-agent - Satellite-side deployment agent
 *
 * Receives deployment commands from ground via CSP and manages
 * binary deployments, backups, and rollbacks.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <pthread.h>
#include <unistd.h>
#include <getopt.h>

#include <csp/csp.h>
#include <csp/csp_iflist.h>
#include <csp/csp_rtable.h>
#include <csp/interfaces/csp_if_zmqhub.h>
#include <csp/drivers/can_socketcan.h>
#include <csp/drivers/usart.h>

#include "satdeploy_agent.h"

#define DEFAULT_NODE_ADDR 5424
#define DEFAULT_INTERFACE "ZMQ"
#define DEFAULT_ZMQ_HOST "localhost"
#define DEFAULT_CAN_DEVICE "can0"
#define DEFAULT_KISS_DEVICE "/dev/ttyS1"
#define DEFAULT_BAUDRATE 115200
#define DEFAULT_NETMASK 8

static volatile int running = 1;

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

static void print_usage(const char *prog) {
    printf("Usage: %s [OPTIONS]\n", prog);
    printf("\nOptions:\n");
    printf("  -i, --interface TYPE   Interface type: ZMQ, CAN, KISS (default: %s)\n", DEFAULT_INTERFACE);
    printf("  -p, --port DEV         Port/device (default: %s for ZMQ, %s for CAN)\n", DEFAULT_ZMQ_HOST, DEFAULT_CAN_DEVICE);
    printf("  -a, --address ADDR     CSP node address (default: %d)\n", DEFAULT_NODE_ADDR);
    printf("  -b, --baudrate BAUD    Baudrate for KISS (default: %d)\n", DEFAULT_BAUDRATE);
    printf("  -m, --netmask MASK     CSP netmask (default: %d)\n", DEFAULT_NETMASK);
    printf("  -h, --help             Show this help\n");
    printf("\nExamples:\n");
    printf("  %s -i ZMQ -p localhost -a 5424      # ZMQ for testing\n", prog);
    printf("  %s -i CAN -p can0 -a 5424           # CAN for satellite\n", prog);
    printf("  %s -i KISS -p /dev/ttyS1 -a 5424    # KISS serial\n", prog);
}

static csp_iface_t *iface_init(const char *interface, const char *port,
                                int node_addr, int netmask, uint32_t baudrate) {
    csp_iface_t *iface = NULL;

    if (strcmp(interface, "ZMQ") == 0) {
        int result = csp_zmqhub_init_filter2(
            "zmq",
            port,
            node_addr,
            netmask,
            true,   /* promisc */
            &iface,
            NULL,   /* via table */
            CSP_ZMQPROXY_SUBSCRIBE_PORT,
            CSP_ZMQPROXY_PUBLISH_PORT
        );

        if (result != CSP_ERR_NONE || iface == NULL) {
            printf("Error: Failed to initialize ZMQ interface\n");
            return NULL;
        }
        iface->name = "zmq";
    }
    else if (strcmp(interface, "CAN") == 0) {
        int error = csp_can_socketcan_open_and_add_interface(
            port,
            "CAN",
            node_addr,
            1000000,  /* bitrate */
            0,        /* promisc */
            &iface
        );

        if (error != CSP_ERR_NONE) {
            printf("Error: Failed to add CAN interface [%s], error: %d\n", port, error);
            return NULL;
        }
        iface->name = "can";
    }
    else if (strcmp(interface, "KISS") == 0) {
        csp_usart_conf_t conf = {
            .device = port,
            .baudrate = baudrate,
            .databits = 8,
            .stopbits = 1,
            .paritysetting = 0
        };

        int error = csp_usart_open_and_add_kiss_interface(
            &conf,
            CSP_IF_KISS_DEFAULT_NAME,
            node_addr,
            &iface
        );

        if (error != CSP_ERR_NONE) {
            printf("Error: Failed to add KISS interface [%s], error: %d\n", port, error);
            return NULL;
        }
        iface->name = "kiss";
    }
    else {
        printf("Error: Unknown interface type '%s'\n", interface);
        return NULL;
    }

    iface->addr = node_addr;
    iface->netmask = netmask;
    csp_rtable_set(0, 0, iface, CSP_NO_VIA_ADDRESS);
    csp_iflist_add(iface);

    return iface;
}

int main(int argc, char *argv[]) {
    static struct option long_options[] = {
        {"interface", required_argument, 0, 'i'},
        {"port", required_argument, 0, 'p'},
        {"address", required_argument, 0, 'a'},
        {"baudrate", required_argument, 0, 'b'},
        {"netmask", required_argument, 0, 'm'},
        {"help", no_argument, 0, 'h'},
        {0, 0, 0, 0}
    };

    char *interface = DEFAULT_INTERFACE;
    char *port = NULL;  /* Will be set based on interface if not specified */
    int node_addr = DEFAULT_NODE_ADDR;
    int netmask = DEFAULT_NETMASK;
    uint32_t baudrate = DEFAULT_BAUDRATE;

    int opt;
    while ((opt = getopt_long(argc, argv, "i:p:a:b:m:h", long_options, NULL)) != -1) {
        switch (opt) {
            case 'i':
                interface = optarg;
                break;
            case 'p':
                port = optarg;
                break;
            case 'a':
                node_addr = atoi(optarg);
                break;
            case 'b':
                baudrate = atoi(optarg);
                break;
            case 'm':
                netmask = atoi(optarg);
                break;
            case 'h':
                print_usage(argv[0]);
                return 0;
            default:
                print_usage(argv[0]);
                return 1;
        }
    }

    /* Set default port based on interface if not specified */
    if (port == NULL) {
        if (strcmp(interface, "ZMQ") == 0) {
            port = DEFAULT_ZMQ_HOST;
        } else if (strcmp(interface, "CAN") == 0) {
            port = DEFAULT_CAN_DEVICE;
        } else if (strcmp(interface, "KISS") == 0) {
            port = DEFAULT_KISS_DEVICE;
        }
    }

    printf("satdeploy-agent v0.1.0\n");
    printf("  Interface: %s\n", interface);
    printf("  Port/Device: %s\n", port);
    printf("  CSP node: %d\n", node_addr);
    printf("  Netmask: %d\n", netmask);

    /* Setup signal handler */
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);

    /* Initialize CSP */
    csp_conf.hostname = "satdeploy-agent";
    csp_conf.model = "AGENT";
    csp_conf.revision = "1";
    csp_conf.version = 2;
    csp_conf.dedup = CSP_DEDUP_OFF;
    csp_init();

    /* Initialize interface */
    csp_iface_t *iface = iface_init(interface, port, node_addr, netmask, baudrate);
    if (iface == NULL) {
        return 1;
    }

    /* Initialize deploy handler */
    if (deploy_handler_init() != 0) {
        printf("Error: Failed to initialize deploy handler\n");
        return 1;
    }

    /* Start router task */
    pthread_t router_handle;
    pthread_create(&router_handle, NULL, &router_task, NULL);

    printf("Agent running. Press Ctrl+C to exit.\n");

    /* Main loop */
    while (running) {
        sleep(1);
    }

    printf("\nShutting down...\n");

    /* Wait for router to finish */
    running = 0;
    pthread_join(router_handle, NULL);

    return 0;
}

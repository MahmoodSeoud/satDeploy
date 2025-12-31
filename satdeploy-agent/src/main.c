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
#include <csp/interfaces/csp_if_zmqhub.h>

#include "satdeploy_agent.h"

#define DEFAULT_NODE_ADDR 5424
#define DEFAULT_ZMQ_HOST "localhost"

static volatile int running = 1;
static int node_addr = DEFAULT_NODE_ADDR;
static const char *zmq_host = DEFAULT_ZMQ_HOST;

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
    printf("  -n, --node ADDR    CSP node address (default: %d)\n", DEFAULT_NODE_ADDR);
    printf("  -z, --zmq HOST     ZMQ hub host (default: %s)\n", DEFAULT_ZMQ_HOST);
    printf("  -h, --help         Show this help\n");
}

int main(int argc, char *argv[]) {
    static struct option long_options[] = {
        {"node", required_argument, 0, 'n'},
        {"zmq", required_argument, 0, 'z'},
        {"help", no_argument, 0, 'h'},
        {0, 0, 0, 0}
    };

    int opt;
    while ((opt = getopt_long(argc, argv, "n:z:h", long_options, NULL)) != -1) {
        switch (opt) {
            case 'n':
                node_addr = atoi(optarg);
                break;
            case 'z':
                zmq_host = optarg;
                break;
            case 'h':
                print_usage(argv[0]);
                return 0;
            default:
                print_usage(argv[0]);
                return 1;
        }
    }

    printf("satdeploy-agent v0.1.0\n");
    printf("  CSP node: %d\n", node_addr);
    printf("  ZMQ host: %s\n", zmq_host);

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

    /* Initialize ZMQ interface */
    csp_iface_t *iface = NULL;
    int result = csp_zmqhub_init_filter2(
        "zmq",
        zmq_host,
        node_addr,
        8,      /* netmask */
        true,   /* promisc */
        &iface,
        NULL,   /* via table */
        CSP_ZMQPROXY_SUBSCRIBE_PORT,
        CSP_ZMQPROXY_PUBLISH_PORT
    );

    if (result != CSP_ERR_NONE || iface == NULL) {
        printf("Error: Failed to initialize ZMQ interface\n");
        return 1;
    }

    iface->addr = node_addr;
    iface->netmask = 8;
    csp_rtable_set(0, 0, iface, CSP_NO_VIA_ADDRESS);
    csp_iflist_add(iface);

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

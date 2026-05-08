#include "csp_init.h"

#include <pthread.h>
#include <stdio.h>
#include <string.h>

#include <csp/csp.h>
#include <csp/csp_iflist.h>
#include <csp/csp_rtable.h>
#include <csp/interfaces/csp_if_zmqhub.h>

static volatile int router_running = 1;

static void *router_task(void *param) {
	(void)param;
	while (router_running) {
		csp_route_work();
	}
	return NULL;
}

int bare_csp_bringup(const char *hostname,
                     int node_addr,
                     int netmask,
                     uint16_t zmq_sub_port,
                     uint16_t zmq_pub_port) {
	csp_conf.hostname = hostname;
	csp_conf.model = "BARE";
	csp_conf.revision = "1";
	csp_conf.version = 2;
	csp_conf.dedup = CSP_DEDUP_OFF;
	csp_init();

	csp_iface_t *iface = NULL;
	int rc = csp_zmqhub_init_filter2(
		"zmq",
		"localhost",
		node_addr,
		netmask,
		true,                /* promisc */
		&iface,
		NULL,                /* via table */
		zmq_sub_port,
		zmq_pub_port);

	if (rc != CSP_ERR_NONE || iface == NULL) {
		fprintf(stderr, "bare_csp_bringup: zmqhub init failed (rc=%d)\n", rc);
		return -1;
	}
	iface->name = "zmq";
	iface->addr = node_addr;
	iface->netmask = netmask;
	csp_rtable_set(0, 0, iface, CSP_NO_VIA_ADDRESS);
	csp_iflist_add(iface);

	pthread_t router_handle;
	if (pthread_create(&router_handle, NULL, router_task, NULL) != 0) {
		fprintf(stderr, "bare_csp_bringup: router thread spawn failed\n");
		return -1;
	}
	pthread_detach(router_handle);

	return 0;
}

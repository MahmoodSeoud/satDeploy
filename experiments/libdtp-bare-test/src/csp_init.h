#ifndef BARE_DTP_CSP_INIT_H
#define BARE_DTP_CSP_INIT_H

#include <stdbool.h>
#include <stdint.h>

/*
 * Bring up CSP with a single ZMQ interface bound to localhost via zmqproxy.
 * Mirrors satdeploy-agent/src/main.c so we exercise libdtp under the exact
 * same transport as the production agent. Returns 0 on success, -1 on error.
 *
 * Spawns a router thread internally. The caller does not need to manage it;
 * the process exits when main returns.
 */
int bare_csp_bringup(const char *hostname,
                     int node_addr,
                     int netmask,
                     uint16_t zmq_sub_port,
                     uint16_t zmq_pub_port);

#endif

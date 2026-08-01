/**
 * session_state.h - cross-pass DTP transfer persistence.
 *
 * Designed to wrap the within-pass selective-repeat retry loop in
 * dtp_client.c (commit 5fbe1b1). When that loop exhausts its 8 retry rounds
 * without reaching full coverage, this module saves the receive-bitmap to a
 * sidecar so the next satdeploy push for the same app picks up where the
 * previous attempt left off — across operator Ctrl-C, APM exit, agent reboot,
 * or pass-window boundaries.
 *
 * Storage: /var/lib/satdeploy/state/<app_name>.dtpstate, mode 0600.
 *
 * On-disk format (little-endian, native struct layout assumed):
 *   uint32_t  format_version    (bump on incompatible schema changes)
 *   uint32_t  expected_size     (must match caller's expected_size on resume)
 *   char[65]  expected_hash     (full SHA256 hex + NUL; gates resume)
 *   uint32_t  nof_packets       (== ceil(expected_size / effective_mtu))
 *   uint16_t  effective_mtu     (mtu - 8, the DTP per-packet payload bytes)
 *   uint16_t  reserved          (alignment / future use; set to 0)
 *   uint8_t   bitmap[bitmap_bytes]   (1 bit per packet, LSB-first)
 *
 * Reject-and-discard on: short read, format-version mismatch, expected_size
 * mismatch, expected_hash mismatch, nof_packets mismatch, effective_mtu
 * mismatch. Strict equality is the right call: a ground rebuild between
 * passes (different SHA256 for the same app), an MTU re-tune, or a
 * different file size all mean the on-disk bitmap refers to bytes that no
 * longer match the new transfer.
 */

#ifndef SATDEPLOY_SESSION_STATE_H
#define SATDEPLOY_SESSION_STATE_H

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

#define SESSION_STATE_DIR  "/var/lib/satdeploy/state"
#define SESSION_STATE_EXT  ".dtpstate"

/* The SVU transfer's cross-pass sidecar: a 16-byte header plus the raw
 * partial reassembly buffer, written incrementally as fragments arrive (see
 * svu_transfer.c). Distinct extension from the DTP bitmap sidecar so the two
 * arms never read each other's state. */
#define SESSION_SVU_EXT    ".svupart"

/* On-disk schema version. Bump on incompatible state-file format changes. */
#define SESSION_STATE_FORMAT_VERSION 1u

/**
 * Compute the on-disk path for an app's session state file.
 *
 * Sanitizes app_name (rejects '/', '\\', '..', control chars).
 * Returns 0 on success, -1 on bad name or buffer-too-small.
 */
int session_state_path(const char *app_name, char *out, size_t out_size);

/**
 * Same as session_state_path but for the SVU data sidecar
 * (<dir>/<app_name>.svupart). Same app_name sanitization rules.
 */
int session_state_svu_path(const char *app_name, char *out, size_t out_size);

/**
 * Ensure SESSION_STATE_DIR exists with mode 0700. Creates parents as needed.
 * Idempotent: returns 0 if the directory already exists.
 */
int session_state_dir_ensure(void);

/**
 * Compute a deterministic session_id for an (app_name, expected_hash) pair.
 *
 * Returns the first 4 bytes of SHA256(app_name || ":" || expected_hash) as a
 * uint32_t. Stable across processes and reboots, so ground and agent agree on
 * the session_id without negotiation. Never returns 0 (libdtp uses 0 as a
 * sentinel in some places).
 */
uint32_t session_state_compute_id(const char *app_name, const char *expected_hash);

/**
 * Returns 1 if the state file exists as a regular file, 0 otherwise.
 */
int session_state_exists(const char *path);

/**
 * Best-effort delete of a state file. Safe to call when the file is absent.
 */
void session_state_unlink(const char *path);

/**
 * Save bitmap to the state file at `path` atomically (tmpfile + rename(2)).
 *
 * @param path             Sidecar path (computed via session_state_path).
 * @param expected_size    Total payload size in bytes.
 * @param expected_hash    Full SHA256 hex (NUL-terminated).
 * @param nof_packets      Total expected packet count.
 * @param effective_mtu    Bytes of payload per packet (mtu - 8).
 * @param bitmap           Receive bitmap.
 * @param bitmap_bytes     ceil(nof_packets / 8).
 * @return 0 on success, -1 on I/O failure (non-fatal: caller proceeds, next
 *         pass just starts over instead of resuming).
 */
int session_state_save(const char *path,
                       uint32_t expected_size,
                       const char *expected_hash,
                       uint32_t nof_packets,
                       uint16_t effective_mtu,
                       const uint8_t *bitmap,
                       size_t bitmap_bytes);

/**
 * Load bitmap from state file at `path`, validating against caller's
 * expected values.
 *
 * @param path             Sidecar path.
 * @param expected_size    Caller's current expected_size; must match on disk.
 * @param expected_hash    Caller's current expected SHA256 hex; must match.
 * @param nof_packets      Caller's computed packet count; must match.
 * @param effective_mtu    Caller's effective MTU; must match.
 * @param bitmap_out       Pre-allocated bitmap buffer to fill on success.
 * @param bitmap_bytes     Size of bitmap_out.
 * @return 1 if state was loaded into bitmap_out (caller resumes from these
 *           received packets);
 *         0 if no state or any validation failed (caller treats as fresh
 *           transfer; the file is unlinked on validation failure to prevent
 *           repeated stale-state loads).
 */
int session_state_load(const char *path,
                       uint32_t expected_size,
                       const char *expected_hash,
                       uint32_t nof_packets,
                       uint16_t effective_mtu,
                       uint8_t *bitmap_out,
                       size_t bitmap_bytes);

#endif /* SATDEPLOY_SESSION_STATE_H */

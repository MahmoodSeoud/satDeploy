/**
 * session_state.h - cross-pass DTP session persistence.
 *
 * libdtp can resume an interrupted transfer if the session state is restored
 * before dtp_start_transfer(). The library exposes serialize/deserialize hooks
 * but leaves on-disk persistence to the application. This module centralizes
 * the state-file path, on-disk format, and the deterministic session_id
 * derivation used to keep ground and agent in lockstep across passes.
 *
 * Storage: /var/lib/satdeploy/state/<app_name>.dtpstate, mode 0600.
 *
 * Format (little-endian, packed manually via fwrite):
 *   uint32_t  on_disk_format_version  (bump on incompatible changes)
 *   uint32_t  dtp_session_version     (libdtp's DTP_SESSION_VERSION at write time)
 *   char[65]  expected_hash           (full SHA256 hex + NUL; gates resume)
 *   uint32_t  bytes_received
 *   uint32_t  payload_size
 *   dtp_meta_req_t request_meta       (session_id, intervals[], etc.)
 *   dtp_opt_remote_cfg remote_cfg
 *
 * Deserialize rejects on any of: file too short, format-version mismatch,
 * dtp-session-version mismatch, expected_hash mismatch with caller's hash.
 * On rejection the file is unlinked so the next attempt starts fresh.
 */

#ifndef SATDEPLOY_SESSION_STATE_H
#define SATDEPLOY_SESSION_STATE_H

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

#define SESSION_STATE_DIR  "/var/lib/satdeploy/state"
#define SESSION_STATE_EXT  ".dtpstate"

/* On-disk schema version. Bump on incompatible state-file format changes. */
#define SESSION_STATE_FORMAT_VERSION 1u

/**
 * Persistence context passed to libdtp's serialize/deserialize hooks.
 *
 * The hooks receive a `void *ctx` that points to one of these. They use it to
 * find the state-file path and validate that the on-disk hash matches what the
 * caller currently expects (preventing resume across a ground rebuild that
 * reused the same payload_id but for different bytes).
 */
typedef struct {
    char path[512];        /* on-disk state file path */
    char expected_hash[65]; /* full SHA256 hex of the payload we expect */
    bool resumed;          /* set true by on_deserialize on successful restore */
} session_state_ctx_t;

/**
 * Compute the on-disk path for an app's session state file.
 *
 * Sanitizes app_name (rejects '/', '..'). Returns 0 on success, -1 on bad name.
 */
int session_state_path(const char *app_name, char *out, size_t out_size);

/**
 * Ensure SESSION_STATE_DIR exists with mode 0700. Creates parents as needed.
 * Idempotent: returns 0 if the directory already exists.
 */
int session_state_dir_ensure(void);

/**
 * Compute the deterministic session_id for an (app_name, expected_hash) pair.
 *
 * Returns the first 4 bytes of SHA256(app_name || ":" || expected_hash) as a
 * uint32_t. Stable across processes and reboots, so ground and agent agree on
 * the session_id without negotiation.
 *
 * expected_hash should be the full 64-char SHA256 hex, but any NUL-terminated
 * string is accepted.
 */
uint32_t session_state_compute_id(const char *app_name, const char *expected_hash);

/**
 * Returns 1 if the state file at path exists and is at least minimally valid,
 * 0 otherwise. Cheap stat()-based check; full validation happens on deserialize.
 */
int session_state_exists(const char *path);

/**
 * Best-effort delete of a state file. Safe to call when the file is absent.
 */
void session_state_unlink(const char *path);

/* Forward decl: full type lives in libdtp's dtp_session.h. */
struct dtp_t;
typedef struct dtp_t dtp_t;

/**
 * libdtp serialize hook. Writes session state to ctx->path atomically
 * (tmpfile + rename(2)). Logs but does not abort on I/O failure — a missed
 * checkpoint just means the next pass starts over, not data corruption.
 */
void session_state_on_serialize(dtp_t *session, void *ctx);

/**
 * libdtp deserialize hook. Reads ctx->path, validates format and session
 * versions and the expected_hash, and on any mismatch unlinks the file and
 * leaves the session unchanged (caller treats as fresh transfer).
 *
 * Sets ctx->resumed = true on a successful restore.
 */
void session_state_on_deserialize(dtp_t *session, void *ctx);

#endif /* SATDEPLOY_SESSION_STATE_H */

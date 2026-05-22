/*
 * session_state_internal.h - cross-pass DTP transfer persistence (internal).
 *
 * Wraps the within-pass selective-repeat retry loop in satpush_pull.c. When
 * the loop exhausts its retry rounds without reaching full coverage, this
 * module saves the receive-bitmap to a sidecar so the next pull for the
 * same (resume_key, expected_hash) picks up where the previous attempt
 * stopped - across operator Ctrl-C, host process exit, agent reboot, or
 * pass-window boundaries.
 *
 * Adapted from satdeploy-agent/include/session_state.h during the libsatpush
 * extract (Step 2 of /plan-eng-review 2026-05-21). Changes from the
 * pre-extract version:
 *   - Hardcoded SESSION_STATE_DIR constant removed; state_dir now flows in
 *     from the caller via the libsatpush context.
 *   - mkdir_p is now inlined into session_state.c so the module no longer
 *     depends on satdeploy_agent.h.
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
 * mismatch. Strict equality is the right call: a re-staged binary (different
 * SHA256 for the same resume_key), an MTU re-tune, or a different file size
 * all mean the on-disk bitmap refers to bytes that no longer match the new
 * transfer.
 *
 * Not part of the public libsatpush API. Adopters who need to clear a
 * sidecar use satpush_resume_unlink() from <satpush/satpush.h>.
 */

#ifndef SATPUSH_SESSION_STATE_INTERNAL_H
#define SATPUSH_SESSION_STATE_INTERNAL_H

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

#define SESSION_STATE_EXT  ".dtpstate"

/* On-disk schema version. Bump on incompatible state-file format changes. */
#define SESSION_STATE_FORMAT_VERSION 1u

/**
 * Compute the on-disk path for a resume key's session state file.
 *
 * Sanitizes resume_key (rejects '/', '\\', '..', control chars).
 * Returns 0 on success, -1 on bad name or buffer-too-small.
 */
int session_state_path(const char *state_dir,
                       const char *resume_key,
                       char *out, size_t out_size);

/**
 * Ensure state_dir exists with mode 0700. Creates parents as needed.
 * Idempotent: returns 0 if the directory already exists.
 */
int session_state_dir_ensure(const char *state_dir);

/**
 * Compute a deterministic session_id for a (resume_key, expected_hash) pair.
 *
 * Returns the first 4 bytes of SHA256(resume_key || ":" || expected_hash) as
 * a uint32_t. Stable across processes and reboots, so puller and server agree
 * on the session_id without negotiation. Never returns 0 (libdtp uses 0 as a
 * sentinel in some places).
 */
uint32_t session_state_compute_id(const char *resume_key, const char *expected_hash);

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
 * Returns 0 on success, -1 on I/O failure (non-fatal: caller proceeds; the
 * next pass just starts over instead of resuming).
 */
int session_state_save(const char *path,
                       uint32_t expected_size,
                       const char *expected_hash,
                       uint32_t nof_packets,
                       uint16_t effective_mtu,
                       const uint8_t *bitmap,
                       size_t bitmap_bytes);

/**
 * Load bitmap from state file at `path`, validating against caller's expected
 * values. Returns 1 on successful load, 0 on missing file or any validation
 * failure (the file is unlinked on validation failure to prevent repeated
 * stale-state loads).
 */
int session_state_load(const char *path,
                       uint32_t expected_size,
                       const char *expected_hash,
                       uint32_t nof_packets,
                       uint16_t effective_mtu,
                       uint8_t *bitmap_out,
                       size_t bitmap_bytes);

#endif /* SATPUSH_SESSION_STATE_INTERNAL_H */

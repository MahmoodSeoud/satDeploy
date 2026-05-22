/**
 * satpush - cross-pass resumable file delivery over libcsp / libdtp.
 *
 * A minimal C library that turns intermittent CSP/DTP links into a reliable
 * file-delivery channel. Transfers survive operator Ctrl-C, agent reboot, and
 * pass-window boundaries; the receive bitmap persists to a sidecar on disk and
 * the next contact picks up exactly where the last one stopped.
 *
 * Two roles, one library:
 *   SERVE side has the file and registers it for incoming pulls via
 *     satpush_serve_register(). Server activity happens passively in libdtp's
 *     background server task; the register call returns immediately.
 *   PULL side actively connects, downloads, verifies, and writes the file via
 *     satpush_pull_file(). Blocks until completion, partial-with-resume, or
 *     hard error.
 *
 * The puller initiates the CSP connection. The server is passive. This matches
 * DTP wire semantics; see lib/dtp/README.rst for the protocol.
 *
 * SHA256 at the application layer guarantees the received file matches the
 * expected hash or the call fails loudly with SATPUSH_HASH_MISMATCH.
 *
 * Intended for CubeSats and other spacecraft running CSP-based stacks with
 * slow or intermittent uplinks (UHF 9600 baud, CAN, KISS-framed serial).
 * Not for IP-connected fleets; use Mender or balena for those.
 *
 * See https://github.com/MahmoodSeoud/satdeploy/tree/main/libsatpush
 */

#pragma once

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Opaque library context. Holds the local CSP node id, sidecar directory,
 * default throughput configuration, and per-process state shared across
 * transfers. Create one per process; share between calls.
 */
typedef struct satpush_ctx satpush_ctx_t;

/**
 * Result codes returned by satpush_pull_file() / satpush_serve_register().
 *
 * SATPUSH_PARTIAL is not an error in the operational sense: it signals that
 * the transfer was interrupted but the receive bitmap was persisted to the
 * sidecar. Calling satpush_pull_file() again with the same resume_key and
 * expected_sha256_hex continues from the saved state.
 */
typedef enum {
    SATPUSH_OK            =  0,  /**< Transfer complete, SHA256 verified.       */
    SATPUSH_PARTIAL       =  1,  /**< Interrupted, state saved for resume.      */
    SATPUSH_HASH_MISMATCH = -1,  /**< All bytes received but SHA256 wrong.      */
    SATPUSH_CANCELLED     = -2,  /**< Cancelled via progress callback.          */
    SATPUSH_HARD_ERROR    = -3,  /**< Connection / IO failure; see errno.       */
    SATPUSH_INVALID       = -4,  /**< Bad arguments to a call.                  */
} satpush_result;

/**
 * Progress callback. Invoked on every received data packet (puller) or once
 * per pull request the server answers (server). Return false to cancel the
 * transfer; the bitmap is persisted before the call returns.
 *
 * Called from the same thread that invoked the satpush API. Must not block
 * for long; slow callbacks throttle the transfer.
 */
typedef bool (*satpush_progress_cb)(void* user_ctx,
                                    uint32_t bytes_done,
                                    uint32_t bytes_total,
                                    uint32_t retry_round);

/**
 * Pull-side options. Initialize with `{0}` and set the required fields; every
 * other field with a 0 / NULL value falls back to library defaults documented
 * inline.
 */
typedef struct {
    /* === required === */
    uint16_t    server_node;          /**< CSP node id of the serving peer.    */
    uint8_t     payload_id;           /**< Registered payload id on the server.*/
    const char* dest_file;            /**< Local path where the file lands.    */
    uint32_t    expected_size;        /**< Expected file size in bytes.        */
    const char* expected_sha256_hex;  /**< 64-hex SHA256 to verify against.    */

    /* === optional === */
    const char* resume_key;           /**< NULL disables resume. Sidecar is
                                           keyed on (resume_key, sha256).      */

    /* tuning; 0 = library default */
    uint16_t    mtu;                  /**< 0 -> 1024                           */
    uint32_t    throughput_bps;       /**< 0 -> ctx default                    */
    uint8_t     timeout_s;            /**< 0 -> 60                             */
    uint8_t     max_retry_rounds;     /**< 0 -> 8                              */

    satpush_progress_cb on_progress;  /**< NULL = no callback                  */
    void*       user_ctx;             /**< Passed to on_progress.              */
} satpush_pull_opts;

/**
 * Serve-side options. Registers a file as available for incoming pulls at
 * payload_id. The registration is passive; libdtp's background server task
 * handles incoming pull requests using the registered payload table.
 */
typedef struct {
    /* === required === */
    uint8_t     payload_id;
    const char* local_file;
    uint32_t    file_size;
} satpush_serve_opts;

/* =====================================================================
 * Lifecycle
 * ===================================================================== */

/**
 * Create a satpush context.
 *
 * Assumes libcsp has already been initialized by the caller (csp_init,
 * csp_route_start_task, interface bindings). satpush does not touch CSP
 * configuration; it uses whatever route table is already in place.
 *
 * @param local_node      CSP node id of this process (informational).
 * @param state_dir       Directory for resume sidecar files. Must exist and
 *                        be writable. Suggested:
 *                          agent:  "/var/lib/satpush/"
 *                          ground: "~/.satpush/state/"
 * @param throughput_bps  Default throughput in bytes per second. Feeds
 *                        libdtp's internal watchdog; setting it wrong causes
 *                        transfers to abort before any packets arrive. Use
 *                        measured link throughput, not the radio's nominal
 *                        baud rate. UHF 9600 baud: pass 1200. S-band 1 Mbps:
 *                        125000. ZMQ loopback / dev: 10000000.
 * @return                Opaque context, or NULL on failure.
 */
satpush_ctx_t* satpush_create(uint16_t local_node,
                              const char* state_dir,
                              uint32_t throughput_bps);

/**
 * Destroy a satpush context. Does not cancel in-flight transfers; callers
 * must ensure all pull / serve calls have returned first.
 */
void satpush_destroy(satpush_ctx_t* ctx);

/* =====================================================================
 * Transfer
 * ===================================================================== */

/**
 * Pull a file from a remote server over CSP/DTP.
 *
 * Blocks the calling thread until the transfer completes, is interrupted, or
 * hits a hard error. On SATPUSH_PARTIAL, the bitmap is persisted to
 * <state_dir>/<resume_key>.dtpstate and a subsequent call with matching
 * resume_key + expected_sha256_hex continues from the saved state.
 *
 * On SATPUSH_OK, the file at dest_file matches expected_sha256_hex
 * byte-for-byte. On SATPUSH_HASH_MISMATCH, the bytes arrived but the verify
 * failed; the dest_file contents are not safe to install.
 *
 * The file is written via fseek+fwrite at the byte offsets carried by each
 * DTP packet, so re-received packets are idempotent overwrites. fsync() is
 * called before the persisted bitmap is updated, so the on-disk file is
 * always consistent with the sidecar claim.
 */
satpush_result satpush_pull_file(satpush_ctx_t* ctx, const satpush_pull_opts* opts);

/**
 * Register a file as available for incoming pulls at payload_id.
 *
 * Returns immediately. Actual serving happens in libdtp's background server
 * task whenever a peer issues a pull request for the registered payload_id.
 *
 * Registering payload_id again replaces the previous registration (del-then-
 * add semantics, matching satdeploy-apm's existing behavior). This is how a
 * re-staged binary at the same payload_id refreshes the served file.
 *
 * @param ctx  May be NULL for serve-only callers. Serve operations do not
 *             read any ctx fields; the parameter is retained for API
 *             symmetry with satpush_pull_file. Callers that only register
 *             and unregister payloads (e.g. satdeploy-apm in v0.1) can
 *             skip satpush_create entirely.
 */
satpush_result satpush_serve_register(satpush_ctx_t* ctx, const satpush_serve_opts* opts);

/**
 * Unregister a previously registered payload. After this call, incoming pulls
 * for payload_id return an error. A no-op if payload_id was not registered.
 *
 * @param ctx  May be NULL for serve-only callers (see satpush_serve_register).
 */
satpush_result satpush_serve_unregister(satpush_ctx_t* ctx, uint8_t payload_id);

/* =====================================================================
 * Helpers
 * ===================================================================== */

/**
 * Compute SHA256 of a file and write it to `out` as a 65-byte (64 hex chars +
 * NUL) lowercase hex string. Convenience wrapper around the integrity layer
 * used internally for verification.
 */
satpush_result satpush_sha256_file(const char* path, char out[65]);

/**
 * Delete the resume sidecar for a given resume_key. Useful when an operator
 * explicitly abandons a partially-uploaded version (e.g. switching to a
 * different binary mid-deploy).
 */
void satpush_resume_unlink(satpush_ctx_t* ctx, const char* resume_key);

/**
 * Human-readable description of a result code. Returned strings are static;
 * do not free.
 */
const char* satpush_strerror(satpush_result r);

/**
 * Returns the library version as a static string (e.g. "0.1.0").
 */
const char* satpush_version_string(void);

#ifdef __cplusplus
}
#endif

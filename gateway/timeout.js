/**
 * Pure timeout policy for the gateway's HTTP calls.
 *
 * Kept free of DSH/Node imports so it can be unit-tested with plain
 * `node tests/test_gateway_timeout.mjs` (no Host, no network).
 */

export const DEFAULT_STARTUP_TIMEOUT_MS = 3_000
export const DEFAULT_REQUEST_TIMEOUT_MS = 120_000

/**
 * A remote command may run for up to 600s (the receiver's max_timeout_sec).
 * The gateway has to wait longer than that, or it aborts the HTTP call while
 * the command keeps running on the PC and the user retries it.
 */
export const COMMAND_TIMEOUT_SLACK_MS = 30_000
export const MAX_COMMAND_TIMEOUT_SEC = 600

/**
 * HTTP timeout for one forwarded tool call.
 *
 * `pc_run` carries its own `timeout_sec`; the HTTP call must outlive it.
 * Other tools keep the per-PC `requestTimeoutMs`, so a hung receiver still
 * fails fast instead of holding every call for ten minutes.
 */
export function callTimeoutMs(spec, args, pc) {
  if (spec.remote === 'run_command') {
    const requested = Number(args?.timeout_sec)
    if (Number.isFinite(requested) && requested > 0) {
      const bounded = Math.min(requested, MAX_COMMAND_TIMEOUT_SEC)
      return Math.ceil(bounded * 1000) + COMMAND_TIMEOUT_SLACK_MS
    }
  }
  return pc.requestTimeoutMs
}

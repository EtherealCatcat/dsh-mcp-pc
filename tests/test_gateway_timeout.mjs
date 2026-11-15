#!/usr/bin/env node
/**
 * P1 regression: the gateway's HTTP timeout must outlive the receiver's own
 * command timeout, and must not inflate timeouts for every other tool.
 *
 *   node tests/test_gateway_timeout.mjs
 */
import assert from 'node:assert/strict'
import {
  COMMAND_TIMEOUT_SLACK_MS,
  DEFAULT_REQUEST_TIMEOUT_MS,
  MAX_COMMAND_TIMEOUT_SEC,
  callTimeoutMs,
} from '../gateway/timeout.js'

const pc = { alias: 'office', requestTimeoutMs: DEFAULT_REQUEST_TIMEOUT_MS }
const runCommand = { remote: 'run_command' }
const captureScreen = { remote: 'capture_screen' }

// A 600s command (the receiver's cap) must be awaited past its own deadline.
assert.equal(
  callTimeoutMs(runCommand, { timeout_sec: 600 }, pc),
  600_000 + COMMAND_TIMEOUT_SLACK_MS,
  'pc_run(timeout_sec=600) must use 600s + slack',
)

// Shorter commands scale with the request instead of always waiting 10 minutes.
assert.equal(callTimeoutMs(runCommand, { timeout_sec: 30 }, pc), 30_000 + COMMAND_TIMEOUT_SLACK_MS)
assert.equal(callTimeoutMs(runCommand, { timeout_sec: 0 }, pc), pc.requestTimeoutMs, 'no explicit timeout keeps the per-PC default')

// Anything above the receiver's cap is clamped to the same cap.
assert.equal(
  callTimeoutMs(runCommand, { timeout_sec: 5_000 }, pc),
  MAX_COMMAND_TIMEOUT_SEC * 1000 + COMMAND_TIMEOUT_SLACK_MS,
  'timeout_sec above the receiver cap must clamp, not extend indefinitely',
)

// Other tools keep the configured timeout so a hung receiver fails fast.
assert.equal(callTimeoutMs(captureScreen, { timeout_sec: 600 }, pc), pc.requestTimeoutMs)
assert.equal(callTimeoutMs({ remote: 'get_system_info' }, {}, pc), pc.requestTimeoutMs)

// Non-numeric input falls back to the per-PC default instead of NaN.
assert.equal(callTimeoutMs(runCommand, { timeout_sec: 'abc' }, pc), pc.requestTimeoutMs)

// Exactly the boundary: 600s is still allowed without clamping surprise.
assert.equal(callTimeoutMs(runCommand, { timeout_sec: 600 }, pc), MAX_COMMAND_TIMEOUT_SEC * 1000 + COMMAND_TIMEOUT_SLACK_MS)

console.log('gateway timeout policy: OK')

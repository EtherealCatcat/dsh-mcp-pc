/**
 * DSH Remote PC — gateway mode.
 *
 * One Host plugin exposes the whole fleet behind seven `pc_*` tools. Every
 * remote call takes a `target` alias and forwards `tools/call` to that PC's
 * stateless Streamable HTTP MCP endpoint, so adding a PC is a config edit and
 * a Host restart — not another mcp-client instance and another tool prefix.
 */
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { createMcpToolDefinition } from '@deepseek-ai/dsh-mcp-client'

export const name = 'pc-gateway'
export const inject = ['tools']

const DEFAULT_STARTUP_TIMEOUT_MS = 3_000
const DEFAULT_REQUEST_TIMEOUT_MS = 120_000

/** Public tool -> upstream MCP tool. Descriptions are intentionally short; the
 * upstream schema carries the detailed parameter docs. */
const REMOTE_TOOLS = {
  pc_sysinfo: {
    remote: 'get_system_info',
    description: 'Read one remote PC: hostname, OS, CPU/memory/disk, displays and DPI, lock state, enabled features, server health. Call this first for a target that has not been probed in this session.',
  },
  pc_run: {
    remote: 'run_command',
    description: 'Run a PowerShell, cmd or Python command on one remote PC; returns exit code, stdout, stderr and timeout state.',
  },
  pc_screenshot: {
    remote: 'capture_screen',
    description: 'Capture one remote PC screen (all monitors or one). The image passes through as native image content when the current model accepts images; otherwise it degrades to a text diagnostic.',
  },
  pc_input: {
    remote: 'input_control',
    description: 'Inject mouse moves/clicks/scroll, text (including Chinese) and key chords (ctrl+c, alt+tab, …) into one remote PC, with optional waits.',
  },
  pc_sleep: {
    remote: 'set_sleep_block',
    description: 'Block or release idle system sleep on one remote PC; optionally keep the display awake.',
  },
  pc_features: {
    remote: 'control_features',
    description: 'Inspect or toggle the remote server feature switches (info, shell, screenshot, input, sleep).',
  },
}

const ALIAS_PATTERN = /^[A-Za-z0-9_-]{1,16}$/
const ENV_REFERENCE = /\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}/g

function expandEnv(value, field) {
  return value.replace(ENV_REFERENCE, (_match, name) => {
    const resolved = process.env[name]
    if (resolved === undefined || resolved === '') throw new Error(`pc-gateway: ${field} references unset environment variable ${name}`)
    return resolved
  })
}

function defaultPcsFile() {
  return fileURLToPath(new URL('./pcs.json', import.meta.url))
}

async function readPcsFile(config) {
  const file = config?.pcsFile ? String(config.pcsFile) : defaultPcsFile()
  let parsed
  try {
    parsed = JSON.parse(await readFile(file, 'utf8'))
  } catch (error) {
    throw new Error(`pc-gateway: cannot read ${file}: ${error?.message ?? error}. Copy pcs.example.json to pcs.json and fill in each PC.`)
  }
  if (!Array.isArray(parsed?.pcs) || parsed.pcs.length === 0) throw new Error(`pc-gateway: ${file} must contain a non-empty "pcs" array`)
  return parsed.pcs
}

async function loadPcs(config) {
  const raw = await readPcsFile(config)
  const defaultRequestTimeout = Number.isFinite(config?.requestTimeoutMs) ? config.requestTimeoutMs : DEFAULT_REQUEST_TIMEOUT_MS
  const seen = new Set()
  return raw.map((entry, index) => {
    const alias = String(entry?.alias ?? '').trim()
    if (!ALIAS_PATTERN.test(alias)) throw new Error(`pc-gateway: pcs[${index}].alias must match [A-Za-z0-9_-]{1,16}`)
    if (seen.has(alias)) throw new Error(`pc-gateway: duplicate alias "${alias}"`)
    seen.add(alias)
    const url = expandEnv(String(entry?.url ?? '').trim(), `pcs[${index}].url`)
    if (!/^https?:\/\//.test(url)) throw new Error(`pc-gateway: pcs[${index}].url must be http(s)`)
    const token = expandEnv(String(entry?.token ?? '').trim(), `pcs[${index}].token`)
    if (!token) throw new Error(`pc-gateway: pcs[${index}].token is empty`)
    return {
      alias,
      displayName: String(entry?.displayName ?? alias),
      url,
      token,
      requestTimeoutMs: Number.isFinite(entry?.requestTimeoutMs) ? entry.requestTimeoutMs : defaultRequestTimeout,
    }
  })
}

let sequence = 0

/** One stateless JSON-RPC request. The remote server has no session state. */
async function rpc(pc, method, params, signal, timeoutMs = pc.requestTimeoutMs) {
  const timeout = AbortSignal.timeout(timeoutMs)
  const composed = signal === undefined ? timeout : AbortSignal.any([signal, timeout])
  let response
  try {
    response = await fetch(pc.url, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        accept: 'application/json, text/event-stream',
        authorization: `Bearer ${pc.token}`,
      },
      body: JSON.stringify({ jsonrpc: '2.0', id: ++sequence, method, params }),
      signal: composed,
    })
  } catch (error) {
    throw new Error(`${pc.alias}: ${method} failed: ${error?.message ?? error}`)
  }
  const text = await response.text()
  if (!response.ok) {
    const hint = response.status === 401 ? ' (check the bearer token)' : ''
    throw new Error(`${pc.alias}: ${method} HTTP ${response.status}${hint}: ${text.slice(0, 300)}`)
  }
  let body
  try {
    body = JSON.parse(text)
  } catch {
    throw new Error(`${pc.alias}: ${method} returned non-JSON: ${text.slice(0, 300)}`)
  }
  if (body?.error) throw new Error(`${pc.alias}/${method}: ${body.error.message ?? JSON.stringify(body.error)}`)
  if (body?.result === undefined) throw new Error(`${pc.alias}/${method}: response has no result`)
  return body.result
}

/** Fetch the remote catalog once, with a short startup budget so an offline PC
 * cannot delay Host startup. Falls back to the bundled snapshot. */
async function loadTools(pcs, ctx, startupTimeoutMs) {
  const settled = await Promise.allSettled(pcs.map(pc => rpc(pc, 'tools/list', {}, undefined, startupTimeoutMs)))
  for (const [index, outcome] of settled.entries()) {
    if (outcome.status === 'fulfilled' && Array.isArray(outcome.value?.tools) && outcome.value.tools.length > 0) {
      ctx.logger?.info?.(`pc-gateway: loaded ${outcome.value.tools.length} remote tools from ${pcs[index].alias}`)
      return outcome.value.tools
    }
  }
  const reasons = settled.map((outcome, index) => `${pcs[index].alias}: ${outcome.status === 'rejected' ? outcome.reason?.message ?? outcome.reason : 'empty tools/list'}`)
  try {
    const fallback = JSON.parse(await readFile(fileURLToPath(new URL('./tools.json', import.meta.url)), 'utf8'))
    if (Array.isArray(fallback) && fallback.length > 0) {
      ctx.logger?.warn?.(`pc-gateway: no PC answered tools/list; using bundled tools.json (${reasons.join('; ')})`)
      return fallback
    }
  } catch {
    // fall through to the loud error below
  }
  throw new Error(`pc-gateway: cannot load the remote tool catalog: ${reasons.join('; ')}`)
}

function withTarget(tool, pcs) {
  const base = tool.inputSchema && typeof tool.inputSchema === 'object' ? tool.inputSchema : { type: 'object' }
  return {
    ...base,
    type: 'object',
    properties: {
      ...(base.properties ?? {}),
      target: {
        type: 'string',
        enum: pcs.map(pc => pc.alias),
        description: `Which PC: ${pcs.map(pc => `${pc.alias} (${pc.displayName})`).join(', ')}.`,
      },
    },
    required: [...new Set([...(Array.isArray(base.required) ? base.required : []), 'target'])],
  }
}

function textResult(value) {
  return { content: [{ type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value, null, 2) }] }
}

function targetOf(args, pcs) {
  const target = args?.target
  const pc = pcs.find(candidate => candidate.alias === target)
  if (pc === undefined) throw new Error(`unknown target "${String(target)}"; use one of: ${pcs.map(candidate => candidate.alias).join(', ')}`)
  return pc
}

export async function apply(ctx, config = {}) {
  const pcs = await loadPcs(config)
  const startupTimeoutMs = Number.isFinite(config.startupTimeoutMs) ? config.startupTimeoutMs : DEFAULT_STARTUP_TIMEOUT_MS
  const tools = await loadTools(pcs, ctx, startupTimeoutMs)
  const byRemote = new Map(tools.map(tool => [tool.name, tool]))
  const disposers = []

  try {
    const definitions = [
      createMcpToolDefinition(ctx, {
        name: 'pc_list',
        rawName: 'pc_list',
        description: 'List the configured remote PCs (alias, display name, endpoint). Use the alias as the target of every other pc_* tool. Set probe=true to check which PCs answer tools/list right now.',
        inputSchema: {
          type: 'object',
          properties: { probe: { type: 'boolean', description: 'Also check each PC with a short tools/list request.' } },
          additionalProperties: false,
        },
        call: async (args, exec) => {
          if (args?.probe === true) {
            const rows = await Promise.all(pcs.map(async pc => {
              try {
                const result = await rpc(pc, 'tools/list', {}, exec?.signal, 3_000)
                return { alias: pc.alias, displayName: pc.displayName, url: pc.url, online: true, tools: result?.tools?.length ?? 0 }
              } catch (error) {
                return { alias: pc.alias, displayName: pc.displayName, url: pc.url, online: false, error: error?.message ?? String(error) }
              }
            }))
            return textResult(rows)
          }
          return textResult(pcs.map(pc => ({ alias: pc.alias, displayName: pc.displayName, url: pc.url })))
        },
      }),
    ]

    for (const [publicName, spec] of Object.entries(REMOTE_TOOLS)) {
      const tool = byRemote.get(spec.remote)
      if (tool === undefined) {
        ctx.logger?.warn?.(`pc-gateway: upstream tool ${spec.remote} is missing; skipping ${publicName}`)
        continue
      }
      definitions.push(createMcpToolDefinition(ctx, {
        name: publicName,
        rawName: spec.remote,
        description: `[remote PC: ${pcs.map(pc => pc.alias).join(', ')}] ${spec.description} ${tool.description ?? ''}`.trim(),
        inputSchema: withTarget(tool, pcs),
        call: async (args, exec) => {
          const pc = targetOf(args, pcs)
          const { target: _target, ...rest } = args ?? {}
          if (spec.remote === 'capture_screen') {
            rest.include_image ??= true
            rest.format ??= 'png'
          }
          return rpc(pc, 'tools/call', { name: spec.remote, arguments: rest }, exec?.signal)
        },
      }))
    }

    for (const definition of definitions) disposers.push(ctx.tools.register(definition))
    ctx.effect(() => () => {
      for (const dispose of disposers.splice(0)) dispose()
    }, 'pc-gateway.tools')
    ctx.logger?.info?.(`pc-gateway: registered ${definitions.length} tools for ${pcs.length} PC(s)`)
  } catch (error) {
    for (const dispose of disposers.splice(0)) dispose()
    throw error
  }
}

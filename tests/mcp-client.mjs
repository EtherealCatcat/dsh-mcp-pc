/**
 * Locate the MCP SDK client that ships with the installed DSH build.
 *
 * The SDK is not a dependency of this repository; it lives inside the Host
 * installation.  Its path differs per platform, per install location and per
 * DSH version (asar vs unpacked), so never hardcode one:
 *
 *   DSH_MCP_CLIENT=<path to dist/index.mjs>   explicit override, always wins
 *   ~/.dsh/profiles/<profile>/node_modules    installed profile dependencies
 *   Windows LOCALAPPDATA install              app.asar/dsh/node_modules
 *   macOS /Applications install               app.asar.unpacked/dsh/node_modules
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const PACKAGE = path.join('@modelcontextprotocol', 'client', 'dist', 'index.mjs');

function profileCandidates() {
  const root = path.join(os.homedir(), '.dsh', 'profiles');
  let profiles = [];
  try {
    profiles = fs.readdirSync(root);
  } catch {
    return [];
  }
  return profiles.map((name) => path.join(root, name, 'node_modules', PACKAGE));
}

function installCandidates() {
  const roots = [];
  if (process.env.LOCALAPPDATA) {
    roots.push(path.join(process.env.LOCALAPPDATA, 'Programs', 'DeepSeek Harness', 'resources'));
  }
  if (process.platform === 'darwin') {
    roots.push('/Applications/DeepSeek Harness.app/Contents/Resources');
  }
  const out = [];
  for (const resources of roots) {
    out.push(path.join(resources, 'app.asar', 'dsh', 'node_modules', PACKAGE));
    out.push(path.join(resources, 'app.asar.unpacked', 'dsh', 'node_modules', PACKAGE));
  }
  return out;
}

/** Every candidate path, in priority order. */
export function clientCandidates() {
  const explicit = process.env.DSH_MCP_CLIENT;
  return [...(explicit ? [explicit] : []), ...profileCandidates(), ...installCandidates()];
}

/** First existing candidate, or throw with the full list. */
export function resolveClientPath() {
  const candidates = clientCandidates();
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new Error(
    `MCP SDK client not found. Set DSH_MCP_CLIENT to dist/index.mjs of @modelcontextprotocol/client. Tried:\n  ${candidates.join('\n  ')}`
  );
}

/** Import the SDK from the resolved path. */
export async function loadMcpClient() {
  return import(pathToFileURL(resolveClientPath()).href);
}

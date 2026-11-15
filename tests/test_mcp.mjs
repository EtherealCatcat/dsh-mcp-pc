import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadMcpClient } from './mcp-client.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

function argValue(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : undefined;
}

// --config / --url / --port (or DSHPC_CONFIG / DSHPC_URL / DSHPC_PORT) make this
// file usable against any locally started server, not just .tmp/server-data.
const configPath =
  process.env.DSHPC_CONFIG || argValue('--config') || path.join(ROOT, '.tmp', 'server-data', 'config.json');
const port = process.env.DSHPC_PORT || argValue('--port') || '18765';
const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
const url = new URL(process.env.DSHPC_URL || argValue('--url') || `http://127.0.0.1:${port}/mcp`);
const { Client, StreamableHTTPClientTransport } = await loadMcpClient();
const transport = new StreamableHTTPClientTransport(url, {
  requestInit: { headers: { Authorization: 'Bearer ' + config.token } },
});
const client = new Client({ name: 'dshpc-test-client', version: '1.0.0' }, { capabilities: {} });

function text(result) {
  if (!result || !Array.isArray(result.content)) return JSON.stringify(result);
  return result.content.filter((c) => c.type === 'text').map((c) => c.text).join('\n');
}

async function call(name, args) {
  const result = await client.callTool({ name, arguments: args });
  console.log(`\n--- ${name} isError=${result.isError === true} ---`);
  console.log(text(result).slice(0, 900));
  return result;
}

try {
  await client.connect(transport);
  console.log('connected; serverVersion=', JSON.stringify(client.getServerVersion()));
  const instructions = client.getInstructions?.() ?? '';
  console.log('instructions bytes=', Buffer.byteLength(instructions, 'utf8'));

  const listed = await client.listTools();
  console.log('tools=', listed.tools.map((t) => t.name).join(', '));
  console.log('tool count=', listed.tools.length);

  await call('run_command', { command: 'Write-Output should-be-blocked' });
  const info = await call('get_system_info', {});
  let infoJson = null;
  try {
    infoJson = JSON.parse(text(info));
    console.log('info host=', infoJson.hostname, 'locked=', infoJson.session.locked, 'features=', JSON.stringify(infoJson.features));
  } catch {}

  await call('control_features', { action: 'status' });
  await call('run_command', { command: "Write-Output 'mcp-ok'; $env:COMPUTERNAME", shell: 'pwsh' });

  const shot = await call('capture_screen', { max_width: 480, include_image: false });
  try {
    const shotJson = JSON.parse(text(shot));
    console.log('shot url=', shotJson.url);
    const downloaded = await fetch(shotJson.url);
    console.log('shot download status=', downloaded.status, 'bytes=', (await downloaded.arrayBuffer()).byteLength);
  } catch (error) {
    console.log('shot url check failed:', error.message);
  }

  await call('input_control', { actions: [{ type: 'wait', ms: 20 }] });
  await call('set_sleep_block', { enable: true });
  await call('set_sleep_block', { enable: false });

  await call('control_features', { action: 'disable', features: ['shell'] });
  await call('run_command', { command: 'Write-Output blocked' });
  await call('control_features', { action: 'enable', features: ['shell'] });

  await call('control_features', { action: 'reset_info_gate' });
  await call('run_command', { command: 'Write-Output blocked-again' });
  await call('get_system_info', {});

  await call('control_features', { action: 'disable_all' });
  await call('control_features', { action: 'enable_all' });

  console.log('\nALL CALLS COMPLETED');
} catch (error) {
  console.error('TEST FAILED:', error);
  process.exitCode = 1;
} finally {
  await client.close().catch(() => {});
}

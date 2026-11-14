import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const clientPackage =
  'C:/Users/Administrator/AppData/Local/Programs/DeepSeek Harness/resources/app.asar/dsh/node_modules/@modelcontextprotocol/client/dist/index.mjs';
const { Client, StreamableHTTPClientTransport } = await import('file:///' + clientPackage);

const configPath =
  process.env.DSHPC_CONFIG ||
  path.join(os.homedir(), 'AppData', 'Local', 'DSHRemotePC', 'data', 'config.json');
const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
const url = new URL(process.env.DSHPC_URL || 'http://127.0.0.1:8765/mcp');

const transport = new StreamableHTTPClientTransport(url, {
  requestInit: { headers: { Authorization: 'Bearer ' + config.token } },
});
const client = new Client({ name: 'dshpc-deployed-sdk-test', version: '1.0.0' }, { capabilities: {} });

function text(result) {
  return (result.content || [])
    .filter((block) => block.type === 'text')
    .map((block) => block.text)
    .join('\n');
}

async function call(name, args) {
  const result = await client.callTool({ name, arguments: args });
  console.log(`${name}: isError=${result.isError === true} ${text(result).slice(0, 160).replace(/\n/g, ' ')}`);
  return result;
}

try {
  await client.connect(transport);
  console.log('connected:', JSON.stringify(client.getServerVersion()));
  console.log('instructions bytes:', Buffer.byteLength(client.getInstructions() || '', 'utf8'));
  const listed = await client.listTools();
  console.log('tools:', listed.tools.map((tool) => tool.name).join(','));

  const reset = await call('control_features', { action: 'reset_info_gate' });
  const gate = await call('run_command', { command: 'hostname' });
  if (gate.isError !== true) throw new Error('info gate did not block run_command');
  const info = await call('get_system_info', {});
  const run = await call('run_command', { command: 'hostname' });
  if (run.isError === true) throw new Error('run_command still blocked after info');
  const shot = await call('capture_screen', { max_width: 480, include_image: false });
  const shotMeta = JSON.parse(text(shot));
  const downloaded = await fetch(shotMeta.url);
  console.log('shot url status:', downloaded.status, 'bytes:', (await downloaded.arrayBuffer()).byteLength);
  await call('input_control', { actions: [{ type: 'wait', ms: 10 }] });
  await call('set_sleep_block', { enable: true });
  await call('set_sleep_block', { enable: false });
  await call('control_features', { action: 'status' });
  console.log('DEPLOYED SDK TEST PASSED');
} catch (error) {
  console.error('DEPLOYED SDK TEST FAILED:', error);
  process.exitCode = 1;
} finally {
  await client.close().catch(() => {});
}

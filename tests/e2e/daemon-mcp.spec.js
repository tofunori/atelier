/**
 * Real JSON-RPC e2e against the atelier-mcp binary in daemon mode.
 */
import { test, expect } from '@playwright/test';
import { spawn } from 'node:child_process';
import {
  mkdtempSync,
  writeFileSync,
  rmSync,
  mkdirSync,
  readFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const DAEMON_BIN = path.join(REPO, 'rust', 'target', 'debug', 'atelier-daemon');
const MCP_BIN = path.join(REPO, 'rust', 'target', 'debug', 'atelier-mcp');
const ASSETS = path.join(REPO, 'assets');

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function shortStateDir() {
  const dir = path.join(tmpdir(), `adm${process.pid % 10000}${Date.now() % 100000}`);
  mkdirSync(dir, { recursive: true });
  return dir;
}

async function waitHealth(port) {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/healthz`);
      if (res.ok) return;
    } catch {
      /* retry */
    }
    await new Promise((r) => setTimeout(r, 50));
  }
  throw new Error('daemon not healthy');
}

function startMcp(env) {
  const child = spawn(MCP_BIN, [], {
    env: { ...process.env, ...env },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  const rl = createInterface({ input: child.stdout });
  let nextId = 1;
  const pending = new Map();
  rl.on('line', (line) => {
    try {
      const msg = JSON.parse(line);
      if (msg.id != null && pending.has(msg.id)) {
        pending.get(msg.id)(msg);
        pending.delete(msg.id);
      }
    } catch {
      /* ignore non-json */
    }
  });
  function request(method, params = {}) {
    const id = nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`MCP timeout on ${method}`));
      }, 8000);
      pending.set(id, (msg) => {
        clearTimeout(timer);
        resolve(msg);
      });
      child.stdin.write(
        JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n'
      );
    });
  }
  return { child, request };
}

test('atelier-mcp JSON-RPC opens project via daemon and lists annotations', async () => {
  const port = await freePort();
  const stateDir = shortStateDir();
  const project = mkdtempSync(path.join(tmpdir(), 'mcp-e2e-'));
  writeFileSync(path.join(project, 'notes.md'), '# mcp e2e\n');
  writeFileSync(path.join(project, 'figures_index.html'), '<html><body>mcp</body></html>');
  writeFileSync(path.join(project, 'figures_data.json'), '{"files":[]}');

  const daemon = spawn(
    DAEMON_BIN,
    [
      '--host',
      '127.0.0.1',
      '--port',
      String(port),
      '--state-dir',
      stateDir,
      '--assets',
      ASSETS,
      '--log-level',
      'error',
    ],
    { stdio: ['ignore', 'ignore', 'ignore'] }
  );

  try {
    await waitHealth(port);
    // Force daemon mode even if auto-detect races.
    const mcp = startMcp({
      ATELIER_RUNTIME: 'daemon',
      ATELIER_DAEMON_STATE_DIR: stateDir,
      CODEX_THREAD_ID: 'mcp-e2e-thread',
    });

    try {
      const init = await mcp.request('initialize', {
        protocolVersion: '2025-03-26',
        capabilities: {},
        clientInfo: { name: 'e2e', version: '0' },
      });
      expect(init.result?.serverInfo?.name).toBe('atelier');

      const tools = await mcp.request('tools/list', {});
      const names = (tools.result?.tools || []).map((t) => t.name);
      expect(names).toContain('atelier_open');
      expect(names).toContain('atelier_list_annotations');

      const open = await mcp.request('tools/call', {
        name: 'atelier_open',
        arguments: { root: project, label: 'MCP E2E', automatic: false },
      });
      expect(open.result?.isError, JSON.stringify(open)).toBeFalsy();
      const structured = open.result?.structuredContent;
      expect(structured?.ok).toBe(true);
      expect(structured?.runtime).toBe('daemon');
      expect(structured?.openUrl).toContain('/open/');
      expect(structured?.url).toContain('/p/');
      expect(structured?.url).toContain('figures_index.html');
      expect(structured?.projectKey).toMatch(/^[a-f0-9]{24}$/);

      // Consume open ticket → session cookie path works for the key.
      const openRes = await fetch(structured.openUrl, { redirect: 'manual' });
      expect([302, 303, 307]).toContain(openRes.status);
      const setCookie = openRes.headers.get('set-cookie') || '';
      expect(setCookie.toLowerCase()).toContain('atelier_session=');
      expect(setCookie).toContain(`/p/${structured.projectKey}/`);

      const list = await mcp.request('tools/call', {
        name: 'atelier_list_annotations',
        arguments: { root: project, limit: 10 },
      });
      expect(list.result?.isError, JSON.stringify(list)).toBeFalsy();
      const bank = list.result?.structuredContent;
      expect(bank?.ok).toBe(true);
      expect(Array.isArray(bank?.pending)).toBe(true);
      expect(Array.isArray(bank?.history)).toBe(true);
    } finally {
      mcp.child.kill('SIGTERM');
      await new Promise((r) => setTimeout(r, 200));
    }
  } finally {
    daemon.kill('SIGTERM');
    await new Promise((r) => setTimeout(r, 200));
    try {
      rmSync(stateDir, { recursive: true, force: true });
    } catch {
      /* ignore */
    }
    try {
      rmSync(project, { recursive: true, force: true });
    } catch {
      /* ignore */
    }
  }
});

/**
 * Daemon-mode E2E: real atelier-daemon + Chromium.
 * Verifies ticket/cookie redirect, asset 200s, no console/network failures,
 * and opening a file from the gallery surface.
 */
import { test, expect } from '@playwright/test';
import { spawn, execFileSync } from 'node:child_process';
import {
  mkdtempSync,
  writeFileSync,
  rmSync,
  mkdirSync,
  readFileSync,
  existsSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const DAEMON_BIN = path.join(REPO, 'rust', 'target', 'debug', 'atelier-daemon');
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
  // Unix socket path length limit on macOS.
  const dir = path.join(tmpdir(), `ad${process.pid % 10000}${Date.now() % 100000}`);
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
  throw new Error(`daemon on ${port} never became healthy`);
}

function controlCall(stateDir, token, method, params = {}) {
  return new Promise((resolve, reject) => {
    const sock = path.join(stateDir, 'daemon.sock');
    const netUnix = net.connect(sock);
    let buf = '';
    netUnix.on('connect', () => {
      const line =
        JSON.stringify({
          id: '1',
          protocol: 1,
          method,
          params,
          token,
        }) + '\n';
      netUnix.write(line);
    });
    netUnix.on('data', (chunk) => {
      buf += chunk.toString();
      if (buf.includes('\n')) {
        netUnix.end();
        try {
          resolve(JSON.parse(buf.split('\n')[0]));
        } catch (e) {
          reject(e);
        }
      }
    });
    netUnix.on('error', reject);
    setTimeout(() => reject(new Error('control timeout')), 5000);
  });
}

function writeProject(root) {
  mkdirSync(root, { recursive: true });
  writeFileSync(
    path.join(root, 'notes.md'),
    '# daemon e2e notes\n\nhello from fixture\n'
  );
  writeFileSync(
    path.join(root, 'script.py'),
    'print("daemon-e2e")\n'
  );
  // Build a real gallery index (fills __FOLDERS__/__DATA__/… placeholders).
  const cli = path.join(REPO, 'rust', 'target', 'debug', 'atelier-cli');
  execFileSync(cli, ['build', '--root', root], {
    env: { ...process.env, ATELIER_ASSETS_DIR: ASSETS },
    stdio: 'pipe',
  });
  if (!existsSync(path.join(root, 'figures_index.html'))) {
    throw new Error('atelier-cli build did not produce figures_index.html');
  }
}

test('daemon gallery loads runtime/CM6/agent_bridge and opens a file', async ({
  page,
}) => {
  const port = await freePort();
  const stateDir = shortStateDir();
  const project = mkdtempSync(path.join(tmpdir(), 'de2e-'));
  const projectB = mkdtempSync(path.join(tmpdir(), 'de2e-switch-'));
  writeProject(project);
  writeProject(projectB);

  const child = spawn(
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
    { stdio: ['ignore', 'ignore', 'pipe'] }
  );
  let stderr = '';
  child.stderr.on('data', (d) => {
    stderr += d.toString();
  });

  try {
    await waitHealth(port);
    const token = readFileSync(path.join(stateDir, 'daemon.token'), 'utf8').trim();
    const opened = await controlCall(stateDir, token, 'project.open', {
      root: project,
      consumer: 'playwright-e2e',
      nativeFs: true,
      theme: 'Codex',
    });
    expect(opened.ok, JSON.stringify(opened)).toBe(true);
    const openUrl = opened.result.openUrl;
    const canonicalUrl = opened.result.url;
    const projectKey = opened.result.key;
    expect(openUrl).toContain('/open/');
    expect(canonicalUrl).toContain(`/p/${projectKey}/`);

    const failed = [];
    const consoleErrors = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });
    page.on('response', (res) => {
      const url = res.url();
      if (!url.includes(`127.0.0.1:${port}`)) return;
      if (res.status() >= 400) {
        failed.push(`${res.status()} ${url}`);
      }
    });
    page.on('pageerror', (err) => consoleErrors.push(String(err)));

    // Follow open ticket → Set-Cookie → redirect to gallery.
    await page.goto(openUrl, { waitUntil: 'domcontentloaded', timeout: 15000 });
    await page.waitForURL(
      (url) => url.pathname.includes(`/p/${projectKey}/`),
      { timeout: 10000 }
    );
    expect(page.url()).toContain(`/p/${projectKey}/figures_index.html`);

    // Bootstrap + runtime present.
    await expect(page.locator('#atelier-bootstrap')).toHaveCount(1);
    const boot = await page.locator('#atelier-bootstrap').textContent();
    expect(boot).toContain(projectKey);
    expect(boot).toContain('"assetBase":"/assets"');
    expect(boot).not.toContain('/assets/assets');

    await page.waitForFunction(
      () => window.AtelierRuntime && window.AtelierRuntime.ready === true,
      null,
      { timeout: 10000 }
    );

    // Register a second project and switch to it from the project-name menu.
    const registeredB = await controlCall(stateDir, token, 'project.register', {
      root: projectB,
    });
    expect(registeredB.ok, JSON.stringify(registeredB)).toBe(true);
    const projectKeyB = registeredB.result.key;
    await page.locator('#projectSwitch').click();
    await expect(page.locator('#projectMenu')).toHaveClass(/show/);
    await expect(page.locator(`[data-project-key="${projectKey}"]`)).toContainText('Projet actuel');
    await expect(page.locator(`[data-project-key="${projectKeyB}"]`)).toBeVisible();
    await page.locator(`[data-project-key="${projectKeyB}"]`).click();
    await page.waitForURL((url) => url.pathname.includes(`/p/${projectKeyB}/`), {
      timeout: 10000,
    });
    expect(page.url()).toContain(`/p/${projectKeyB}/figures_index.html`);

    // The destination cookie is scoped correctly, and the same menu can
    // return to the original project without restarting the daemon.
    await page.locator('#projectSwitch').click();
    await expect(page.locator(`[data-project-key="${projectKeyB}"]`)).toContainText('Projet actuel');
    await page.locator(`[data-project-key="${projectKey}"]`).click();
    await page.waitForURL((url) => url.pathname.includes(`/p/${projectKey}/`), {
      timeout: 10000,
    });
    await page.waitForFunction(
      () => window.AtelierRuntime && window.AtelierRuntime.ready === true,
      null,
      { timeout: 10000 }
    );

    // Critical resources must be 200 (probed via page.evaluate fetch with credentials).
    const probes = await page.evaluate(async (key) => {
      const paths = [
        '/assets/atelier_runtime.js',
        '/assets/atelier_events.js',
        `/.fig_thumbs/agent_bridge_ui.js`,
        `/.fig_thumbs/cm6/editor.bundle.js`,
        `/.fig_thumbs/ts/atelier-client.js`,
      ];
      const out = [];
      for (const p of paths) {
        // Relative to current origin; AtelierRuntime rewrites .fig_thumbs.
        const url =
          window.AtelierRuntime && window.AtelierRuntime.rewriteUrl
            ? window.AtelierRuntime.rewriteUrl(p)
            : p;
        const res = await fetch(url, { credentials: 'same-origin' });
        out.push({ path: p, url, status: res.status, ok: res.ok, len: (await res.text()).length });
      }
      return out;
    }, projectKey);

    for (const p of probes) {
      expect(p.ok, JSON.stringify(p)).toBeTruthy();
      expect(p.status).toBe(200);
      expect(p.len).toBeGreaterThan(0);
      expect(p.url).not.toContain('/assets/assets/');
      if (p.path.includes('.fig_thumbs')) {
        expect(p.url).toContain(`/p/${projectKey}/.fig_thumbs/`);
      }
    }

    // Open notes from explorer-style API path under project scope.
    const editorUrl = await page.evaluate(() => {
      return window.AtelierRuntime.openEditor('notes.md', 'code');
    });
    expect(editorUrl).toContain(`/p/${projectKey}/`);
    expect(editorUrl).toContain('notes.md');

    await page.goto(new URL(editorUrl, `http://127.0.0.1:${port}`).href, {
      waitUntil: 'domcontentloaded',
      timeout: 15000,
    });
    // Editor surface should load without 404 on its own scripts.
    await page.waitForTimeout(500);

    const hardFailures = failed.filter(
      (f) =>
        !f.includes('favicon') &&
        !f.includes('.map') &&
        // Some optional gallery endpoints may 404 without data; fail on assets/thumbs/runtime only.
        (f.includes('/assets/') ||
          f.includes('.fig_thumbs') ||
          f.includes('atelier_runtime') ||
          f.includes('editor.bundle') ||
          f.includes('agent_bridge') ||
          f.includes('atelier-client'))
    );
    expect(hardFailures, hardFailures.join('\n')).toEqual([]);

    const hardConsole = consoleErrors.filter(
      (t) =>
        !/favicon/i.test(t) &&
        !/DevTools/i.test(t) &&
        !/ResizeObserver/i.test(t) &&
        // Network 404s are asserted via the response listener above.
        !/Failed to load resource/i.test(t)
    );
    expect(hardConsole, hardConsole.join('\n')).toEqual([]);
  } finally {
    child.kill('SIGTERM');
    await new Promise((resolve) => {
      const t = setTimeout(resolve, 2000);
      child.once('exit', () => {
        clearTimeout(t);
        resolve();
      });
    });
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
    try {
      rmSync(projectB, { recursive: true, force: true });
    } catch {
      /* ignore */
    }
    if (stderr && process.env.DEBUG_DAEMON_E2E) {
      console.error(stderr);
    }
  }
});

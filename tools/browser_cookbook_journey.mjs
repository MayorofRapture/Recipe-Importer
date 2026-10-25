#!/usr/bin/env node
// Synthetic range selection, extraction, review, and import exclusion journey.
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdtemp, readFile, rm, mkdir, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { assertBrowserFixtureAttestation, prepareEvidenceDirectory,
  startIsolatedBrowserFixture } from './browser_fixture_runner.mjs';

const evidenceArg = process.argv.slice(2).find((value) => value.startsWith('--evidence-dir='))?.slice(15);
if (!evidenceArg || process.argv.slice(2).some((value) => /^--url(?:=|$)/i.test(value))) {
  throw new Error('Provide --evidence-dir under .test-tmp. This journey owns its isolated app and accepts no URL.');
}
const evidence = await prepareEvidenceDirectory(evidenceArg);
const executableArg = process.argv.slice(2).find((value) => value.startsWith('--executable='))?.slice(13);
const cachedChromium = join(process.env.LOCALAPPDATA || '', 'ms-playwright',
  'chromium_headless_shell-1200', 'chrome-headless-shell-win64', 'chrome-headless-shell.exe');
const installedChrome = join(process.env.PROGRAMFILES || 'C:\\Program Files',
  'Google', 'Chrome', 'Application', 'chrome.exe');
const executable = resolve(executableArg || (existsSync(cachedChromium) ? cachedChromium : installedChrome));
const fixture = await startIsolatedBrowserFixture({ scenario: 'cookbook', requiredCandidates: [] });

class CDP {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map();
    ws.addEventListener('message', ({ data }) => {
      const message = JSON.parse(data); if (!message.id) return;
      const pending = this.pending.get(message.id); this.pending.delete(message.id);
      message.error ? pending.reject(new Error(message.error.message)) : pending.resolve(message.result);
    });
  }
  send(method, params = {}) {
    const id = ++this.id; this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolvePromise, reject) => {
      const timer = setTimeout(() => reject(new Error(`CDP timeout: ${method}`)), 15000);
      this.pending.set(id, { resolve: (value) => { clearTimeout(timer); resolvePromise(value); },
        reject: (error) => { clearTimeout(timer); reject(error); } });
    });
  }
}
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
async function evaluate(cdp, expression) {
  const result = await cdp.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || 'Browser evaluation failed');
  return result.result?.value;
}
async function waitFor(cdp, expression, label, timeout = 30000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await evaluate(cdp, expression).catch(() => false)) return; await sleep(300); }
  throw new Error(`Timed out waiting for ${label}: ${await evaluate(cdp, 'document.body.innerText.slice(-1800)')}`);
}
async function click(cdp, text, selector = 'button,a,[role="button"]') {
  const pos = await evaluate(cdp, `(() => {const e=[...document.querySelectorAll(${JSON.stringify(selector)})].find(x=>{
    const r=x.getBoundingClientRect();return r.width&&r.height&&(x.innerText||x.getAttribute('aria-label')||'').replace(/\\s+/g,' ').includes(${JSON.stringify(text)});
  });e?.scrollIntoView({block:'center'});if(!e)return null;const r=e.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2,disabled:e.disabled};})()`);
  if (!pos) throw new Error(`Could not find control: ${text}`);
  if (pos.disabled) throw new Error(`Control is disabled: ${text}`);
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: pos.x, y: pos.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: pos.x, y: pos.y, button: 'left', clickCount: 1 });
  await sleep(250);
}
async function setNumber(cdp, label, value) {
  const pos = await evaluate(cdp, `(() => {const e=[...document.querySelectorAll('input')].find(x=>
    x.getAttribute('aria-label')===${JSON.stringify(label)}||x.closest('[data-testid="stNumberInput"]')?.innerText.includes(${JSON.stringify(label)}));if(!e)return null;
    e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2};})()`);
  if (!pos) throw new Error(`Could not find numeric input ${label}`);
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: pos.x, y: pos.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: pos.x, y: pos.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'a', code: 'KeyA', modifiers: 2, windowsVirtualKeyCode: 65 });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'a', code: 'KeyA', modifiers: 2, windowsVirtualKeyCode: 65 });
  await cdp.send('Input.insertText', { text: String(value) });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
}
async function screenshot(cdp, name) {
  const shot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  const path = join(evidence, name); await mkdir(evidence, { recursive: true });
  await writeFile(path, Buffer.from(shot.data, 'base64')); return path;
}
const profile = await mkdtemp(join(fixture.root, 'browser-profile-'));
let browser;
let ws;
try {
  browser = spawn(executable, ['--headless=new', '--disable-gpu', '--disable-background-networking',
    '--disable-component-update', '--disable-sync', '--disable-extensions', '--no-first-run',
    '--proxy-server=http://127.0.0.1:9', '--proxy-bypass-list=127.0.0.1;localhost;[::1]',
    '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1',
    `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore', windowsHide: true });
  browser.on('error', (error) => console.error(`Chromium spawn error: ${error.message}`));
  const activePort = join(profile, 'DevToolsActivePort'); let port;
  for (let i = 0; i < 200; i += 1) { try { port = Number((await readFile(activePort, 'utf8')).split(/\r?\n/)[0]); break; } catch { await sleep(100); } }
  if (!port) throw new Error('Chromium did not expose local DevTools.');
  const targetResponse = await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(fixture.url)}`, { method: 'PUT' });
  if (!targetResponse.ok) throw new Error(`Could not create browser target (${targetResponse.status}).`);
  ws = new WebSocket((await targetResponse.json()).webSocketDebuggerUrl);
  await new Promise((ok, fail) => { ws.addEventListener('open', ok, { once: true }); ws.addEventListener('error', fail, { once: true }); });
  const cdp = new CDP(ws);
  await cdp.send('Page.enable'); await cdp.send('Runtime.enable'); await cdp.send('DOM.enable');
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  await cdp.send('Page.navigate', { url: fixture.url });
  await waitFor(cdp, `document.body.innerText.includes('Isolated browser fixture: ${fixture.runId}:${fixture.nonce}')`, 'attested synthetic application');
  await assertBrowserFixtureAttestation(cdp, fixture);
  await waitFor(cdp, `document.body.innerText.includes('synthetic-whole-cookbook.pdf') && document.body.innerText.includes('run_cookbook_selector.bat')`, 'staged cookbook discovery');
  await waitFor(cdp, `[...document.querySelectorAll('input')].some(x=>x.closest('[data-testid="stNumberInput"]')?.innerText.includes('Last page'))`, 'page range controls');
  const artifacts = [await screenshot(cdp, 'cookbook-discovered-1440x1000.png')];
  await setNumber(cdp, 'Last page', 2);
  await click(cdp, 'Add range to selection');
  await waitFor(cdp, `document.body.innerText.includes('Pages 1-2')`, 'inclusive page selection');
  await click(cdp, 'Commit selected page ranges');
  await waitFor(cdp, `document.body.innerText.includes('Previously committed ranges')`, 'durable range commit');
  artifacts.push(await screenshot(cdp, 'cookbook-range-committed-1440x1000.png'));
  await click(cdp, 'Ranges to extract', 'input,[role="combobox"]');
  await click(cdp, 'Pages 1-2', '[role="option"],li,[data-baseweb="menu"] *');
  await click(cdp, 'Start selected range extraction');
  await waitFor(cdp, `document.body.innerText.includes('Extraction finished') || document.body.innerText.includes('ready for review')`, 'synthetic cookbook extraction');
  await waitFor(cdp, `document.body.innerText.includes('Review')`, 'review navigation');
  artifacts.push(await screenshot(cdp, 'cookbook-extraction-complete-1440x1000.png'));
  await click(cdp, 'Review', 'a,button,[role="tab"],[role="button"]');
  await waitFor(cdp, `document.body.innerText.includes('Cookbook: synthetic-whole-cookbook.pdf')`, 'range candidate review and import block');
  artifacts.push(await screenshot(cdp, 'cookbook-review-import-blocked-1440x1000.png'));
  const result = await evaluate(cdp, `(() => ({text:document.body.innerText.slice(-2200),
    viewport:{width:innerWidth,height:innerHeight,documentWidth:document.documentElement.scrollWidth},
    controls:[...document.querySelectorAll('button,input,textarea,a')].map(e=>({tag:e.tagName,name:e.getAttribute('aria-label')||e.labels?.[0]?.innerText?.trim()||e.innerText?.trim()||'',role:e.getAttribute('role')||'',disabled:e.disabled||e.getAttribute('aria-disabled')==='true'}))}))()`);
  console.log(JSON.stringify({ completed: true, artifacts, final: result }, null, 2));
  ws.close();
} finally {
  if (browser && browser.exitCode === null && process.platform === 'win32') {
    const { spawnSync } = await import('node:child_process');
    spawnSync('taskkill.exe', ['/PID', String(browser.pid), '/T', '/F'], { windowsHide: true });
  } else browser?.kill('SIGTERM');
  await sleep(150); await rm(profile, { recursive: true, force: true }).catch(() => {});
  await fixture.close();
}

#!/usr/bin/env node
// Synthetic-only Chromium flow for identity reconciliation screens and recovery.
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm, mkdir, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { assertBrowserFixtureAttestation, rejectArbitraryUrl,
  prepareEvidenceDirectory, startIsolatedBrowserFixture } from './browser_fixture_runner.mjs';

function arg(name, fallback = undefined) {
  const prefix = `--${name}=`;
  const found = process.argv.slice(2).find((value) => value.startsWith(prefix));
  return found ? found.slice(prefix.length) : fallback;
}

rejectArbitraryUrl();
const evidenceArg = arg('evidence-dir');
const executable = resolve(arg('executable', join(process.env.LOCALAPPDATA || '', 'ms-playwright',
  'chromium_headless_shell-1200', 'chrome-headless-shell-win64', 'chrome-headless-shell.exe')));
if (!evidenceArg) {
  throw new Error('Provide a synthetic --evidence-dir.');
}
const evidenceDir = await prepareEvidenceDirectory(evidenceArg);
const fixture = await startIsolatedBrowserFixture({ scenario: 'identity', requiredCandidates: ['Fried Rice'] });
const url = fixture.url;

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    ws.addEventListener('message', async (event) => {
      const raw = typeof event.data === 'string' ? event.data
        : event.data instanceof Blob ? await event.data.text() : Buffer.from(event.data).toString('utf8');
      const message = JSON.parse(raw);
      if (message.id && this.pending.has(message.id)) {
        const { resolve: ok, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        message.error ? reject(new Error(message.error.message)) : ok(message.result);
      }
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((ok, reject) => {
      const timer = setTimeout(() => reject(new Error(`CDP timeout: ${method}`)), 12000);
      this.pending.set(id, {
        resolve: (value) => { clearTimeout(timer); ok(value); },
        reject,
      });
    });
  }
}

const sleep = (ms) => new Promise((resolveSleep) => setTimeout(resolveSleep, ms));
async function evaluate(cdp, expression) {
  const result = await cdp.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || 'Browser evaluation failed.');
  return result.result?.value;
}
async function waitFor(cdp, expression, description, timeout = 30000) {
  const until = Date.now() + timeout;
  while (Date.now() < until) {
    if (await evaluate(cdp, expression).catch(() => false)) return;
    await sleep(250);
  }
  const pageText = await evaluate(cdp, 'document.body?.innerText?.slice(0,1600)')
    .catch(() => 'page unavailable');
  throw new Error(`Timed out waiting for ${description}. Page: ${pageText}`);
}
async function clickText(cdp, label) {
  const point = await evaluate(cdp, `(() => {
    const node=[...document.querySelectorAll('button')].find(e=>e.innerText.trim()===${JSON.stringify(label)}
      && !e.disabled && e.getAttribute('aria-disabled')!=='true' && e.getClientRects().length);
    if(!node)return null; node.scrollIntoView({block:'center'});
    const r=node.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2};
  })()`);
  if (!point) throw new Error(`Enabled browser button not found: ${label}`);
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', ...point, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...point, button: 'left', clickCount: 1 });
}
async function clickLastText(cdp, label) {
  const point = await evaluate(cdp, `(() => {
    const nodes=[...document.querySelectorAll('button')].filter(e=>e.innerText.trim()===${JSON.stringify(label)}
      && !e.disabled && e.getAttribute('aria-disabled')!=='true' && e.getClientRects().length);
    const node=nodes.at(-1); if(!node)return null; node.scrollIntoView({block:'center'});
    const r=node.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2};
  })()`);
  if (!point) throw new Error(`Enabled browser button not found: ${label}`);
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', ...point, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...point, button: 'left', clickCount: 1 });
}
async function capture(cdp, name, visibleText) {
  await evaluate(cdp, `(() => {
    const text=${JSON.stringify(visibleText)};
    const node=[...document.querySelectorAll('h1,h2,h3,p,button,[data-testid="stAlert"]')]
      .find(e=>(e.innerText||'').includes(text));
    node?.scrollIntoView({block:'center',inline:'nearest'});
  })()`);
  await sleep(450);
  const shot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  const path = join(evidenceDir, name);
  await writeFile(path, Buffer.from(shot.data, 'base64'));
  console.error(`Captured ${path}`);
}

const profile = await mkdtemp(join(fixture.root, 'browser-profile-'));
let browser;
let ws;
try {
  browser = spawn(executable, [
    '--headless=new', '--disable-gpu', '--disable-dev-shm-usage', '--disable-background-networking',
    '--disable-component-update', '--disable-sync', '--disable-default-apps', '--disable-extensions',
    '--no-first-run', '--metrics-recording-only', '--disable-quic', '--disable-async-dns',
    '--proxy-server=http://127.0.0.1:9', '--proxy-bypass-list=127.0.0.1;localhost;[::1]',
    '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1',
    `--user-data-dir=${profile}`, 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });
  const activePort = join(profile, 'DevToolsActivePort');
  let port;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try { port = Number((await readFile(activePort, 'utf8')).split(/\r?\n/)[0]); break; }
    catch { if (browser.exitCode !== null) throw new Error(`Chromium exited with code ${browser.exitCode}.`); await sleep(100); }
  }
  if (!port) throw new Error('Chromium did not expose its loopback DevTools endpoint.');
  const targetResponse = await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(url)}`, { method: 'PUT' });
  if (!targetResponse.ok) throw new Error(`Chromium could not open the local fixture (${targetResponse.status}).`);
  const target = await targetResponse.json();
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((ok, fail) => { ws.addEventListener('open', ok, { once: true }); ws.addEventListener('error', fail, { once: true }); });
  const cdp = new CDP(ws);
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false });
  await cdp.send('Page.navigate', { url });
  await waitFor(cdp, `document.readyState==='complete' && document.body.innerText.includes('Demonstration library')
    && document.body.innerText.includes('Recipe title')
    && document.body.innerText.includes('Recipe identity requires reconciliation')`, 'synthetic identity warning');
  await assertBrowserFixtureAttestation(cdp, fixture);
  await mkdir(evidenceDir, { recursive: true });

  await capture(cdp, '01-identity-warning.png', 'Recipe identity requires reconciliation');
  await clickText(cdp, 'Resolve identity');
  await waitFor(cdp, `document.body.innerText.includes('The recipe content matches an earlier extraction.')`,
    'identity comparison panel');
  await capture(cdp, '02-suggestion-only-comparison.png', 'The recipe content matches an earlier extraction.');
  await capture(cdp, '03-confirm-identity-action.png', 'Confirm identity match');
  await waitFor(cdp, `document.body.innerText.includes('Confirm all 18 safe matches')`, 'safe batch overview');
  await capture(cdp, '05-safe-batch-overview.png', 'Confirm all 18 safe matches');
  const batchCountShown = await evaluate(cdp, `document.body.innerText.includes('18 safe identity matches available across the review queue.')`);
  await clickText(cdp, 'Confirm identity match');
  await waitFor(cdp, `document.body.innerText.includes('1 suggestion awaiting review')
    && !document.body.innerText.includes('Recipe identity requires reconciliation')`, 'resolved identity with remaining blocker');
  await capture(cdp, '04-resolved-with-review-blocker.png', '1 suggestion awaiting review');
  const body = await evaluate(cdp, 'document.body.innerText');
  if (!body.includes('Approve & next')) throw new Error('Review controls did not return after identity confirmation.');
  await clickLastText(cdp, 'Review');
  await waitFor(cdp, `document.body.innerText.includes('Accept inferred suggestion')`,
    'the linked extraction suggestion controls');
  await clickText(cdp, 'Accept inferred suggestion');
  await waitFor(cdp, `(() => [...document.querySelectorAll('button')].some(e=>e.innerText.trim()==='Approve & next'
    && !e.disabled && e.getAttribute('aria-disabled')!=='true'))()`, 'enabled approval after explicit suggestion review');
  await capture(cdp, '06-linked-review-ready-for-approval.png', 'Approve & next');
  await clickText(cdp, 'Approve & next');
  await sleep(800);
  const afterApproval = await evaluate(cdp, 'document.body.innerText');
  if (/approval was not recorded|candidate identity requires explicit reconciliation/i.test(afterApproval)) {
    throw new Error('Browser approval did not pass the backend identity gate.');
  }
  console.log(JSON.stringify({ screenshots: 6, identityResolved: true, suggestionExplicitlyAccepted: true,
    approvalClicked: true, approvalErrorShown: false, batchCountShown }));
} finally {
  ws?.close();
  browser?.kill();
  await sleep(150);
  await rm(profile, { recursive: true, force: true }).catch(() => {});
  await fixture.close();
}

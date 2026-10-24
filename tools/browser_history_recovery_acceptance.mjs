#!/usr/bin/env node
// Synthetic-only Chromium flow for historical import reconciliation.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { assertBrowserFixtureAttestation, prepareEvidenceDirectory,
  rejectArbitraryUrl, startIsolatedBrowserFixture } from './browser_fixture_runner.mjs';

function arg(name, fallback = undefined) {
  const prefix = `--${name}=`;
  const value = process.argv.slice(2).find((item) => item.startsWith(prefix));
  return value ? value.slice(prefix.length) : fallback;
}

rejectArbitraryUrl();
const evidenceArg = arg('evidence-dir');
const executable = resolve(arg('executable', join(process.env.LOCALAPPDATA || '', 'ms-playwright',
  'chromium_headless_shell-1200', 'chrome-headless-shell-win64', 'chrome-headless-shell.exe')));
if (!evidenceArg) throw new Error('Provide a synthetic --evidence-dir.');
const evidenceDir = await prepareEvidenceDirectory(evidenceArg);
const fixture = await startIsolatedBrowserFixture({ scenario: 'history', requiredCandidates: ['Fried Rice'] });
const profile = await mkdtemp(join(fixture.root, 'browser-profile-'));

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    ws.addEventListener('message', async (event) => {
      const raw = typeof event.data === 'string' ? event.data
        : event.data instanceof Blob ? await event.data.text() : Buffer.from(event.data).toString('utf8');
      const message = JSON.parse(raw);
      if (!message.id || !this.pending.has(message.id)) return;
      const { resolve: ok, reject } = this.pending.get(message.id);
      this.pending.delete(message.id);
      message.error ? reject(new Error(message.error.message)) : ok(message.result);
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((ok, reject) => {
      const timer = setTimeout(() => reject(new Error(`Timed out waiting for CDP ${method}.`)), 12000);
      this.pending.set(id, {
        resolve: (value) => { clearTimeout(timer); ok(value); },
        reject: (error) => { clearTimeout(timer); reject(error); },
      });
    });
  }
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
async function evaluate(cdp, expression) {
  const result = await cdp.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || 'Browser evaluation failed.');
  return result.result?.value;
}
async function waitFor(cdp, check, description, timeout = 30000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await check()) return;
    await sleep(200);
  }
  const diagnostic = await evaluate(cdp, `JSON.stringify({text:document.body?.innerText?.slice(0,1800),controls:[...document.querySelectorAll('input,textarea,button')]
    .map(control=>({tag:control.tagName,type:control.type||'',aria:control.getAttribute('aria-label'),labels:[...(control.labels||[])].map(label=>label.innerText.trim()),
      value:control.value??null,checked:control.checked??null,disabled:control.disabled??null,text:control.innerText?.trim().slice(0,100)||''}))})`)
    .catch(() => 'page unavailable');
  throw new Error(`Timed out waiting for ${description}. Browser diagnostic: ${diagnostic}`);
}
async function accessibleNode(cdp, role, name) {
  const { nodes } = await cdp.send('Accessibility.getFullAXTree');
  const matches = nodes.filter((node) => node.role?.value === role && node.name?.value === name && !node.ignored);
  if (matches.length !== 1) {
    const available = nodes.filter((node) => node.role?.value === role && !node.ignored)
      .map((node) => node.name?.value || '').filter(Boolean).slice(0, 40);
    const rendered = await evaluate(cdp, `JSON.stringify({text:document.body?.innerText?.slice(-2200),buttons:[...document.querySelectorAll('button')]
      .map(button=>({text:button.innerText.trim(),aria:button.getAttribute('aria-label'),disabled:button.disabled,
        visible:button.getClientRects().length>0})),fields:[...document.querySelectorAll('input,textarea')]
      .map(field=>({tag:field.tagName,aria:field.getAttribute('aria-label'),value:field.value,visible:field.getClientRects().length>0}))})`);
    throw new Error(`Expected one accessible ${role} named ${JSON.stringify(name)}, found ${matches.length}. Available names: ${JSON.stringify(available)}. Rendered controls: ${rendered}`);
  }
  return matches[0];
}
async function elementDetails(cdp, node) {
  if (!node.backendDOMNodeId) throw new Error('Accessible control has no DOM backing node.');
  const { object } = await cdp.send('DOM.resolveNode', { backendNodeId: node.backendDOMNodeId });
  const result = await cdp.send('Runtime.callFunctionOn', { objectId: object.objectId, returnByValue: true,
    functionDeclaration: `function(){this.scrollIntoView({block:'center',inline:'nearest'});const r=this.getBoundingClientRect();
      return {x:r.x+r.width/2,y:r.y+r.height/2,visible:r.width>0&&r.height>0,
        disabled:!!this.disabled||this.getAttribute('aria-disabled')==='true',value:this.value??null};}` });
  return { objectId: object.objectId, ...result.result.value };
}
async function clickAccessible(cdp, role, name) {
  await waitFor(cdp, async () => {
    try { await accessibleNode(cdp, role, name); return true; }
    catch { return false; }
  }, `accessible ${role} ${JSON.stringify(name)}`);
  const node = await accessibleNode(cdp, role, name);
  const control = await elementDetails(cdp, node);
  if (!control.visible || control.disabled) throw new Error(`Accessible ${role} ${JSON.stringify(name)} is not visible and enabled.`);
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: control.x, y: control.y });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: control.x, y: control.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: control.x, y: control.y, button: 'left', clickCount: 1 });
}
async function fillTextbox(cdp, name, value, { pressEnter = false } = {}) {
  await waitFor(cdp, async () => {
    try { await accessibleNode(cdp, 'textbox', name); return true; }
    catch { return false; }
  }, `accessible textbox ${JSON.stringify(name)}`);
  const node = await accessibleNode(cdp, 'textbox', name);
  const field = await elementDetails(cdp, node);
  if (!field.visible || field.disabled) throw new Error(`Textbox ${JSON.stringify(name)} is not visible and enabled.`);
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: field.x, y: field.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: field.x, y: field.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'a', code: 'KeyA', modifiers: 2, windowsVirtualKeyCode: 65 });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'a', code: 'KeyA', modifiers: 2, windowsVirtualKeyCode: 65 });
  await cdp.send('Input.insertText', { text: value });
  if (pressEnter) {
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
  }
  await waitFor(cdp, async () => {
    const current = await elementDetails(cdp, await accessibleNode(cdp, 'textbox', name)).catch(() => null);
    return current?.value === value;
  }, `typed value in textbox ${JSON.stringify(name)}`);
}
async function capture(cdp, fileName) {
  const shot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  await writeFile(join(evidenceDir, fileName), Buffer.from(shot.data, 'base64'));
}
async function bodyIncludes(cdp, text) {
  return (await evaluate(cdp, 'document.body?.innerText || ""')).includes(text);
}

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
  const portFile = join(profile, 'DevToolsActivePort');
  let port;
  const portDeadline = Date.now() + 15000;
  while (Date.now() < portDeadline && !port) {
    port = Number((await readFile(portFile, 'utf8').catch(() => '')).split(/\r?\n/)[0]) || undefined;
    if (!port) await sleep(100);
  }
  if (!port) throw new Error('Chromium did not expose its loopback DevTools endpoint.');
  const targets = await fetch(`http://127.0.0.1:${port}/json/list`).then((response) => response.json());
  const page = targets.find((target) => target.type === 'page');
  if (!page) throw new Error('Chromium did not create a page target.');
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((ok, fail) => { ws.addEventListener('open', ok, { once: true }); ws.addEventListener('error', fail, { once: true }); });
  const cdp = new CDP(ws);
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  await cdp.send('Accessibility.enable');
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false });
  await cdp.send('Page.navigate', { url: fixture.url });
  await waitFor(cdp, () => bodyIncludes(cdp, 'Demonstration library'), 'isolated synthetic app startup');
  await assertBrowserFixtureAttestation(cdp, fixture);
  await clickAccessible(cdp, 'link', 'Import & history');
  await waitFor(cdp, () => bodyIncludes(cdp, 'Creation is blocked until reconciliation.'), 'uncertain historical attempt');
  await fillTextbox(cdp, 'Existing Mealie recipe slug', 'historical-existing', { pressEnter: true });
  await waitFor(cdp, async () => {
    try { return !(await elementDetails(cdp, await accessibleNode(cdp, 'button', 'Inspect existing recipe'))).disabled; }
    catch { return false; }
  }, 'enabled recipe inspection action');
  await clickAccessible(cdp, 'button', 'Inspect existing recipe');
  const disclosure = 'did not preserve the submitted recipe snapshot or immutable content digest';
  await waitFor(cdp, () => bodyIncludes(cdp, disclosure), 'historical evidence disclosure');
  const confirmationName = 'Confirm this existing recipe';
  await waitFor(cdp, async () => {
    try { await accessibleNode(cdp, 'button', confirmationName); return true; }
    catch { return false; }
  }, 'confirmation control after comparison rerender');
  const initiallyDisabled = (await elementDetails(cdp, await accessibleNode(cdp, 'button', confirmationName))).disabled;
  assert.equal(initiallyDisabled, true, 'confirmation must stay disabled before evidence and acknowledgment');
  await capture(cdp, 'historical-evidence-required.png');
  await fillTextbox(cdp, 'Evidence for linking this Mealie recipe to the uncertain import',
    'Matched the archived scan filename, source date, and ingredient wording against my kitchen notes.');
  await clickAccessible(cdp, 'checkbox', 'I understand the original submitted content evidence is unavailable.');
  await clickAccessible(cdp, 'checkbox', 'I understand the listed fields were not returned by Mealie and cannot be compared.');
  await waitFor(cdp, async () => {
    try { return !(await elementDetails(cdp, await accessibleNode(cdp, 'button', confirmationName))).disabled; }
    catch { return false; }
  }, 'confirmation enabled after evidence and acknowledgment');
  await clickAccessible(cdp, 'button', confirmationName);
  await waitFor(cdp, () => bodyIncludes(cdp, 'Existing recipe linked for this attempt: historical-existing'), 'durable historical reconciliation');
  await capture(cdp, 'historical-reconciliation-linked.png');

  const events = (await readFile(join(fixture.output, 'ui_import_events.jsonl'), 'utf8'))
    .trim().split(/\r?\n/).map((line) => JSON.parse(line));
  const linked = events.find((event) => event.event === 'reconciled_existing');
  assert.equal(linked?.attempt_id, 'synthetic-historical-cli-attempt');
  assert.equal(linked?.mealie_recipe_id, 'synthetic-mealie-recipe-id');
  assert.equal(linked?.historical_content_ack, true);
  console.log(JSON.stringify({ historicalDisclosure: true, confirmationGated: true,
    durableAttemptAndMealieIdentity: true, screenshots: 2 }));
} finally {
  ws?.close();
  browser?.kill();
  await sleep(150);
  await rm(profile, { recursive: true, force: true }).catch(() => {});
  await fixture.close();
}

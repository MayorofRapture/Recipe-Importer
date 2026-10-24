#!/usr/bin/env node
// Real browser checks for saved manual approval and one-shot instruction navigation.
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { assertBrowserFixtureAttestation, prepareEvidenceDirectory, rejectArbitraryUrl,
  startIsolatedBrowserFixture } from './browser_fixture_runner.mjs';

function arg(name, fallback = undefined) {
  const prefix = `--${name}=`;
  return process.argv.slice(2).find((item) => item.startsWith(prefix))?.slice(prefix.length) ?? fallback;
}

rejectArbitraryUrl();
const evidenceArg = arg('evidence-dir');
if (!evidenceArg) throw new Error('Provide --evidence-dir under project .test-tmp.');
const evidenceDir = await prepareEvidenceDirectory(evidenceArg);
const executable = resolve(arg('executable', join(process.env.LOCALAPPDATA || '', 'ms-playwright',
  'chromium_headless_shell-1200', 'chrome-headless-shell-win64', 'chrome-headless-shell.exe')));
const fixture = await startIsolatedBrowserFixture({ scenario: 'p0',
  requiredCandidates: ['Fried Rice', 'Synthetic Manual Directions'] });
const profile = await mkdtemp(join(fixture.root, 'browser-profile-'));
let browser;

class CDP {
  constructor(ws) {
    this.ws = ws; this.nextId = 0; this.pending = new Map();
    ws.addEventListener('message', async (event) => {
      const raw = typeof event.data === 'string' ? event.data
        : event.data instanceof Blob ? await event.data.text() : Buffer.from(event.data).toString('utf8');
      const message = JSON.parse(raw);
      if (message.id && this.pending.has(message.id)) {
        const request = this.pending.get(message.id); this.pending.delete(message.id);
        message.error ? request.reject(new Error(message.error.message)) : request.resolve(message.result);
      }
    });
  }
  send(method, params = {}) {
    const id = ++this.nextId; this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolveRequest, reject) => {
      const timer = setTimeout(() => reject(new Error(`CDP timeout: ${method}`)), 12000);
      this.pending.set(id, { resolve: (value) => { clearTimeout(timer); resolveRequest(value); },
        reject: (error) => { clearTimeout(timer); reject(error); } });
    });
  }
}
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
async function evaluate(cdp, expression) {
  const result = await cdp.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || 'Browser evaluation failed.');
  return result.result?.value;
}
async function waitFor(cdp, expression, description, timeout = 30000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await evaluate(cdp, expression).catch(() => false)) return;
    await sleep(200);
  }
  throw new Error(`Timed out waiting for ${description}: ${(await evaluate(cdp, 'document.body.innerText').catch(() => '')).slice(0, 5000)}`);
}
async function click(cdp, text, selector = 'button,a,[role="button"],[role="radio"],[role="tab"]') {
  const point = await evaluate(cdp, `(() => {const text=${JSON.stringify(text)};const e=[...document.querySelectorAll(${JSON.stringify(selector)})].find(x=>{const r=x.getBoundingClientRect();return r.width>0&&r.height>0&&(x.innerText||x.getAttribute('aria-label')||'').replace(/\\s+/g,' ').trim().includes(text)&&!x.disabled&&x.getAttribute('aria-disabled')!=='true'});if(!e)return null;e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2,text:e.innerText.trim()}})()`);
  if (!point) throw new Error(`Visible enabled control not found: ${text}. Page: ${(await evaluate(cdp, 'document.body.innerText')).slice(0, 1000)}`);
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1 });
  await sleep(350);
}
async function jumpTo(cdp, title) {
  if (!await evaluate(cdp, `(()=>{const e=document.querySelector('input[aria-label="Jump to recipe"]');if(!e)return false;const r=e.getBoundingClientRect();return r.width>0&&r.height>0})()`)) {
    await click(cdp, 'Queue');
  }
  const point = await evaluate(cdp, `(() => {const e=document.querySelector('input[aria-label="Jump to recipe"]');if(!e)return null;e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}})()`);
  if (!point) throw new Error('Recipe selector was not available.');
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1 });
  await waitFor(cdp, `!![...document.querySelectorAll('[role="option"]')].find(e=>e.innerText.includes(${JSON.stringify(title)}))`, `recipe option ${title}`);
  await click(cdp, title, '[role="option"]');
  await waitFor(cdp, `document.querySelector('input[aria-label="Recipe title"]')?.value===${JSON.stringify(title)}`, `selected recipe ${title}`);
}
async function screenshot(cdp, file) {
  const image = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  const path = join(evidenceDir, file); await writeFile(path, Buffer.from(image.data, 'base64')); return path;
}

try {
  browser = spawn(executable, [
    '--headless=new', '--disable-gpu', '--disable-background-networking', '--disable-component-update',
    '--disable-sync', '--disable-default-apps', '--disable-extensions', '--no-first-run',
    '--proxy-server=http://127.0.0.1:9', '--proxy-bypass-list=127.0.0.1;localhost;[::1]',
    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1',
    '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
    `--user-data-dir=${profile}`, 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });
  let port;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try { port = Number((await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split(/\r?\n/)[0]); break; }
    catch { if (browser.exitCode !== null) throw new Error(`Chromium exited with code ${browser.exitCode}.`); await sleep(100); }
  }
  if (!port) throw new Error('Chromium did not expose its local DevTools endpoint.');
  const targetResponse = await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(fixture.url)}`, { method: 'PUT' });
  if (!targetResponse.ok) throw new Error(`DevTools target failed: ${targetResponse.status}`);
  const target = await targetResponse.json(); const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((ok, fail) => { ws.addEventListener('open', ok, { once: true }); ws.addEventListener('error', fail, { once: true }); });
  const cdp = new CDP(ws);
  await cdp.send('Page.enable'); await cdp.send('Runtime.enable');
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await cdp.send('Page.navigate', { url: fixture.url });
  await waitFor(cdp, `!!document.querySelector('input[aria-label="Recipe title"]')`, 'initial recipe editor');
  await assertBrowserFixtureAttestation(cdp, fixture);

  if (arg('skip-instruction-navigation') !== 'true') {
    // The no-instructions radio moves the editor tab once; a normal rerun must not trap the page there.
    await jumpTo(cdp, 'Synthetic Manual Directions');
    await waitFor(cdp, `document.querySelector('input[aria-label="Recipe title"]')?.value==='Synthetic Manual Directions'`, 'no-instructions recipe');
    await click(cdp, 'Add instructions manually', '[role="radio"],label');
    await waitFor(cdp, `document.body.innerText.includes('Instructions section is open')`, 'stable one-shot navigation');
    await sleep(500);
    const tabState = await evaluate(cdp, `JSON.stringify([...document.querySelectorAll('[role="tab"]')].map(e=>({text:e.innerText,selected:e.getAttribute('aria-selected'),base:e.getAttribute('data-baseweb')})))`);
    const tabs = JSON.parse(tabState);
    const instructionTab = tabs.find((item) => item.text.includes('Instructions'));
    if (!instructionTab || instructionTab.selected !== 'true') {
      throw new Error(`The one-shot navigation did not select the Instructions tab: ${tabState}`);
    }
  }

  await jumpTo(cdp, 'Browser saved manual title');
  await waitFor(cdp, `document.body.innerText.includes('Original source unavailable')`, 'manual source-unavailable preview');
  await assertBrowserFixtureAttestation(cdp, fixture);
  await waitFor(cdp, `document.body.innerText.includes('Saved revision 1: Browser saved manual title')`, 'saved manual preview revision');
  const savedPreview = await evaluate(cdp, `document.body.innerText`);
  await screenshot(cdp, 'p0-manual-approval-saved-preview.png');
  for (const value of ['Saved revision 1: Browser saved manual title', 'Description Synthetic saved description.',
    'Yield 3 servings', '4 cups synthetic rice', '2 synthetic eggs', 'Heat the synthetic rice.',
    'Fold in the synthetic eggs.', 'Synthetic saved note.']) {
    if (!savedPreview.includes(value)) {
      throw new Error(`Saved manual preview omitted persisted field ${value}: ${savedPreview.slice(0, 3000)}`);
    }
  }
  const initialApprovalState = await evaluate(cdp, `JSON.stringify({ack:[...document.querySelectorAll('input[type="checkbox"]')].find(e=>e.labels&&[...e.labels].some(l=>l.innerText.includes('I have reviewed the recipe')))?.checked,approve:[...document.querySelectorAll('button')].filter(b=>b.innerText.includes('Approve manually')).map(b=>b.disabled)})`);
  const initialState = JSON.parse(initialApprovalState);
  if (initialState.ack !== false || !initialState.approve.length || initialState.approve.some((disabled) => !disabled)) {
    const buttons = await evaluate(cdp, `JSON.stringify([...document.querySelectorAll('button')].map(b=>b.innerText.trim()).filter(Boolean))`);
    throw new Error(`Manual approval did not start disabled pending acknowledgment and rationale: ${initialApprovalState}; buttons=${buttons}`);
  }
  await click(cdp, 'I have reviewed the recipe', 'label');
  await waitFor(cdp, `[...document.querySelectorAll('input[type="checkbox"]')].find(e=>e.labels&&[...e.labels].some(l=>l.innerText.includes('I have reviewed the recipe')))?.checked===true`, 'explicit manual-review acknowledgment');
  const rationalePoint = await evaluate(cdp, `(()=>{const e=[...document.querySelectorAll('textarea')].find(x=>x.getAttribute('aria-label')?.includes('Why are you approving'));if(!e)return null;e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}})()`);
  if (!rationalePoint) throw new Error('Manual approval rationale field was unavailable.');
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: rationalePoint.x, y: rationalePoint.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: rationalePoint.x, y: rationalePoint.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.insertText', { text: 'Reviewed the saved synthetic revision.' });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', modifiers: 2, windowsVirtualKeyCode: 13 });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', modifiers: 2, windowsVirtualKeyCode: 13 });
  await waitFor(cdp, `(()=>{const why=[...document.querySelectorAll('textarea')].find(e=>e.getAttribute('aria-label')?.includes('Why are you approving'));const button=[...document.querySelectorAll('button')].find(e=>e.innerText.includes('Approve manually'));return why?.value.includes('Reviewed the saved synthetic revision.')&&button&&!button.disabled})()`, 'manual approval readiness after acknowledgment and rationale');
  await click(cdp, 'Approve manually & next');
  await waitFor(cdp, `[...document.querySelectorAll('input[aria-label="Recipe title"]')].some(e=>e.getClientRects().length&&e.value!=='Browser saved manual title')`, 'manual approval navigation');
  const events = (await readFile(join(fixture.output, 'review_events.jsonl'), 'utf8')).split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
  if (!fixture.manualCandidateId || !events.some((event) => event.event === 'review_approval'
      && event.candidate_id === fixture.manualCandidateId
      && event.approval_method === 'manual_source_unavailable'
      && event.reviewer_acknowledged === true)) {
    throw new Error('Manual source-unavailable approval did not persist against the saved candidate.');
  }

  console.log(JSON.stringify({ status: 'passed', attestation: fixture.attestation,
    instructionNavigation: arg('skip-instruction-navigation') === 'true' ? 'skipped' : 'one-shot and stable',
    savedRevisionPreview: 1, previewFields: ['title', 'yield', 'description', 'ingredients', 'instructions', 'notes'],
    approvalBlockedUntilAcknowledgment: true, savedRevisionApproved: true, fakeServicesOnly: true,
    screenshot: join(evidenceDir, 'p0-manual-approval-saved-preview.png') }, null, 2));
  ws.close();
} finally {
  if (browser && browser.exitCode === null) {
    if (process.platform === 'win32') {
      const { spawnSync } = await import('node:child_process');
      spawnSync('taskkill.exe', ['/PID', String(browser.pid), '/T', '/F'], { windowsHide: true });
    } else browser.kill('SIGTERM');
  }
  await sleep(100);
  await rm(profile, { recursive: true, force: true }).catch(() => {});
  await fixture.close();
}

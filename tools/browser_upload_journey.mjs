#!/usr/bin/env node
// Browser-only UI journey using cached Chromium, Node's built-in WebSocket,
// CDP mouse/keyboard/file APIs, and the same loopback-only request policy.
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm, mkdir, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { assertBrowserFixtureAttestation, rejectArbitraryUrl,
  prepareEvidenceDirectory, startIsolatedBrowserFixture } from './browser_fixture_runner.mjs';

function arg(name, fallback = undefined) {
  const prefix = `--${name}=`;
  const found = process.argv.slice(2).find((item) => item.startsWith(prefix));
  return found ? found.slice(prefix.length) : fallback;
}
rejectArbitraryUrl();
const evidenceArg = arg('evidence-dir');
const viewport = (arg('viewport', '1440x900').match(/^(\d+)x(\d+)$/) || []).slice(1).map(Number);
const executable = resolve(arg('executable', join(process.env.LOCALAPPDATA || '', 'ms-playwright',
  'chromium_headless_shell-1200', 'chrome-headless-shell-win64', 'chrome-headless-shell.exe')));
if (!evidenceArg || viewport.length !== 2) {
  throw new Error('Provide --evidence-dir and optional --viewport=1440x900. Upload files are generated inside the isolated fixture.');
}
const evidence = await prepareEvidenceDirectory(evidenceArg);
const fixture = await startIsolatedBrowserFixture({ scenario: 'upload', requiredCandidates: [] });
const url = fixture.url;
const validFile = fixture.validFile;
const invalidFile = fixture.invalidFile;

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); ws.addEventListener('message', async (event) => {
    const raw = typeof event.data === 'string' ? event.data : event.data instanceof Blob ? await event.data.text() : Buffer.from(event.data).toString('utf8');
    const message = JSON.parse(raw);
    if (message.id && this.pending.has(message.id)) {
      const { resolve: ok, reject } = this.pending.get(message.id); this.pending.delete(message.id);
      message.error ? reject(new Error(message.error.message)) : ok(message.result);
    }
  }); }
  send(method, params = {}) {
    const id = ++this.id; this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((ok, reject) => { const timer = setTimeout(() => reject(new Error(`CDP timeout: ${method}`)), 12000);
      this.pending.set(id, { resolve: (value) => { clearTimeout(timer); ok(value); }, reject }); });
  }
}
const sleep = (ms) => new Promise((resolveSleep) => setTimeout(resolveSleep, ms));
async function evaluate(cdp, expression) {
  const value = await cdp.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (value.exceptionDetails) throw new Error(value.exceptionDetails.text || 'Browser evaluation failed');
  return value.result?.value;
}
async function waitFor(cdp, expression, description, timeout = 30000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await evaluate(cdp, expression).catch(() => false)) return; await sleep(300); }
  const body = await evaluate(cdp, 'document.body.innerText.slice(0,1600)').catch(() => 'unavailable');
  throw new Error(`Timed out waiting for ${description}. Visible text: ${body}`);
}
async function clickText(cdp, text, selector = 'button,a,[role="button"]') {
  const box = await evaluate(cdp, `(() => {
    const match=[...document.querySelectorAll(${JSON.stringify(selector)})].find(e=>{
      const r=e.getBoundingClientRect(), value=(e.innerText||e.getAttribute('aria-label')||'').replace(/\\s+/g,' ').trim();
      return r.width>0 && r.height>0 && value.includes(${JSON.stringify(text)});
    });
    match?.scrollIntoView({block:'center', inline:'nearest'});
    if(!match)return null; const r=match.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2,text:match.innerText.trim(),disabled:match.disabled,scrollY};
  })()`);
  if (!box) throw new Error(`Could not find visible control containing: ${text}`);
  console.error(`Mouse click ${JSON.stringify({text:box.text,x:Math.round(box.x),y:Math.round(box.y),disabled:box.disabled,scrollY:Math.round(box.scrollY)})}`);
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: box.x, y: box.y });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: box.x, y: box.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: box.x, y: box.y, button: 'left', clickCount: 1 });
  await sleep(300);
  console.error(`Browser URL after click: ${await evaluate(cdp, 'location.href')}`);
  return box.text;
}
async function fillByLabel(cdp, label, value) {
  const box = await evaluate(cdp, `(() => {
    const e=[...document.querySelectorAll('input,textarea')].find(x=>
      x.getAttribute('aria-label')===${JSON.stringify(label)} || [...(x.labels||[])].some(l=>l.innerText.trim()===${JSON.stringify(label)}) ||
      x.closest('[data-testid="stTextInput"]')?.innerText.includes(${JSON.stringify(label)}));
    e?.scrollIntoView({block:'center', inline:'nearest'});
    if(!e)return null; const r=e.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2};
  })()`);
  if (!box) throw new Error(`Could not find input labelled: ${label}`);
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: box.x, y: box.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: box.x, y: box.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'a', code: 'KeyA', modifiers: 2, windowsVirtualKeyCode: 65 });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'a', code: 'KeyA', modifiers: 2, windowsVirtualKeyCode: 65 });
  await cdp.send('Input.insertText', { text: value });
}
async function chooseFiles(cdp, paths) {
  const document = await cdp.send('DOM.getDocument', { depth: -1 });
  const node = await cdp.send('DOM.querySelector', { nodeId: document.root.nodeId, selector: 'input[type="file"]' });
  if (!node.nodeId) throw new Error('The upload file input is not available.');
  await cdp.send('DOM.setFileInputFiles', { nodeId: node.nodeId, files: paths });
}
async function screenshot(cdp, name) {
  const image = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  const path = join(evidence, name);
  await mkdir(evidence, { recursive: true });
  await writeFile(path, Buffer.from(image.data, 'base64'));
  return path;
}
async function key(cdp, value, code, vk, modifiers = 0) {
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: value, code, windowsVirtualKeyCode: vk, modifiers });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: value, code, windowsVirtualKeyCode: vk, modifiers });
}

const profile = await mkdtemp(join(fixture.root, 'browser-profile-'));
let browser;
let ws;
try {
  console.error(`Chromium starting: ${executable}`);
  browser = spawn(executable, [
    '--headless=new', '--disable-gpu', '--disable-dev-shm-usage',
    '--disable-background-networking', '--disable-component-update', '--disable-sync',
    '--disable-default-apps', '--disable-extensions', '--disable-features=MediaRouter,OptimizationHints',
    '--no-first-run', '--metrics-recording-only', '--disable-quic', '--disable-async-dns',
    '--proxy-server=http://127.0.0.1:9', '--proxy-bypass-list=127.0.0.1;localhost;[::1]',
    '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1',
    `--user-data-dir=${profile}`, 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });
  browser.on('error', (error) => console.error(`Chromium spawn error: ${error.message}`));
  browser.on('exit', (code, signal) => console.error(`Chromium exit: code=${code}, signal=${signal}`));
  const activePort = join(profile, 'DevToolsActivePort');
  let port;
  for (let i = 0; i < 200; i += 1) { try { port = Number((await readFile(activePort, 'utf8')).split(/\r?\n/)[0]); break; } catch { if (browser.exitCode !== null) break; await sleep(100); } }
  if (!port) throw new Error('Chromium did not expose local DevTools.');
  const targetResponse = await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(url)}`, { method: 'PUT' });
  if (!targetResponse.ok) throw new Error(`Could not create a browser target (${targetResponse.status}).`);
  const target = await targetResponse.json();
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((ok, reject) => { ws.addEventListener('open', ok, { once: true }); ws.addEventListener('error', reject, { once: true }); });
  const cdp = new CDP(ws);
  await cdp.send('Page.enable'); await cdp.send('Runtime.enable'); await cdp.send('DOM.enable');
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: viewport[0], height: viewport[1], deviceScaleFactor: 1, mobile: viewport[0] <= 480 });
  await cdp.send('Page.navigate', { url });
  await waitFor(cdp, `document.body.innerText.includes('Demonstration library')`, 'isolated application');
  await assertBrowserFixtureAttestation(cdp, fixture);
  await waitFor(cdp, `document.readyState==='complete' && /Batch name/.test(document.body.innerText)`, 'Upload form');
  const artifacts = [];

  await fillByLabel(cdp, 'Batch name', 'Synthetic browser batch');
  await fillByLabel(cdp, 'Cookbook tag (optional)', 'synthetic-demo-tag');
  await clickText(cdp, 'Create batch');
  await waitFor(cdp, `/Synthetic browser batch/.test(document.body.innerText) && /Add PDF/.test(document.body.innerText)`, 'created batch and uploader');
  artifacts.push(await screenshot(cdp, 'upload-created-1440x900.png'));

  await chooseFiles(cdp, [validFile, invalidFile]);
  await waitFor(cdp, `/Saved synthetic-recipe.png/.test(document.body.innerText) && /Could not add invalid-scan.pdf/.test(document.body.innerText)`, 'partial upload acceptance');
  await waitFor(cdp, `(() => { const b=[...document.querySelectorAll('button')].find(x=>x.innerText.includes('Start extraction')); return !!b && !b.disabled; })()`, 'enabled extraction action');
  artifacts.push(await screenshot(cdp, 'upload-partial-success-1440x900.png'));
  const errorVisible = await evaluate(cdp, `(() => { const e=[...document.querySelectorAll('div,p,span')].find(x=>x.children.length===0 && x.innerText.includes('Could not add invalid-scan.pdf')); e?.scrollIntoView({block:'center'}); return !!e; })()`);
  if (errorVisible) artifacts.push(await screenshot(cdp, 'upload-rejected-file-error-1440x900.png'));
  await evaluate(cdp, 'window.scrollTo(0,0)');

  await clickText(cdp, 'Start extraction');
  await waitFor(cdp, `/Extraction is complete/.test(document.body.innerText)`, 'synthetic background extraction');
  artifacts.push(await screenshot(cdp, 'upload-complete-1440x900.png'));

  await clickText(cdp, 'Review', 'a,button,[role="tab"],[role="button"]');
  await waitFor(cdp, `([...document.querySelectorAll('input')].some(e=>e.value.includes('Synthetic demo from synthetic-recipe')) || document.body.innerText.includes('Synthetic demo from synthetic-recipe')) && /Save draft/.test(document.body.innerText)`, 'review candidate');
  artifacts.push(await screenshot(cdp, 'review-candidate-1440x900.png'));
  await clickText(cdp, 'Save draft');
  await waitFor(cdp, `/Draft saved \\(revision 1\\)/.test(document.body.innerText)`, 'durable draft revision');
  const priorDocumentStart = await evaluate(cdp, 'performance.timeOrigin');
  await cdp.send('Page.reload');
  await waitFor(cdp, `performance.timeOrigin>${priorDocumentStart}`, 'browser reload completion');
  await waitFor(cdp, `!!document.querySelector('input[aria-label="Recipe title"]') && /Save draft/.test(document.body.innerText) && /synthetic-recipe.png/.test(document.body.innerText)`, 'restored saved draft after reload');
  artifacts.push(await screenshot(cdp, 'review-draft-restored-1440x900.png'));
  await clickText(cdp, 'Approve & next');
  await waitFor(cdp, `/You.ve reached the end|queue is clear|No recipes match these filters|Open Import & history/.test(document.body.innerText)`, 'explicit approval');

  await clickText(cdp, 'Import & history', 'a,button,[role="tab"],[role="button"]');
  await waitFor(cdp, `/Ready to import/.test(document.body.innerText) && /Select all eligible recipes/.test(document.body.innerText)`, 'approved recipe import controls');
  await clickText(cdp, 'Select all eligible recipes');
  await waitFor(cdp, `/Synthetic demo from synthetic-recipe/.test(document.body.innerText)`, 'approved recipe selected for import');
  await fillByLabel(cdp, 'Mealie tag (optional)', 'synthetic-demo-tag');
  await clickText(cdp, 'Preview import');
  await waitFor(cdp, `/1 recipes will be sent to Mealie/.test(document.body.innerText)`, 'import preview');
  artifacts.push(await screenshot(cdp, 'import-preview-1440x900.png'));
  await clickText(cdp, 'Import 1 recipe');
  await waitFor(cdp, `/Imported/.test(document.body.innerText) && /History/.test(document.body.innerText)`, 'fake Mealie import and history');
  await evaluate(cdp, `(() => { const text='Synthetic demo from synthetic-recipe'; const items=[...document.querySelectorAll('div,p,span')].filter(e=>e.children.length===0 && e.innerText?.includes(text)); items.sort((a,b)=>a.innerText.length-b.innerText.length)[0]?.scrollIntoView({block:'center'}); })()`);
  artifacts.push(await screenshot(cdp, 'import-history-1440x900.png'));

  const accessible = await evaluate(cdp, `(() => ({
    title:document.title, url:location.href, viewport:{width:innerWidth,height:innerHeight,documentWidth:document.documentElement.scrollWidth,
      horizontalOverflow:document.documentElement.scrollWidth>innerWidth},
    headings:[...document.querySelectorAll('h1,h2,h3,h4')].map(e=>({level:e.tagName,text:e.innerText.trim()})),
    controls:[...document.querySelectorAll('button,input,textarea,[role="combobox"],a')].map(e=>{
      const r=e.getBoundingClientRect(),s=getComputedStyle(e);return{tag:e.tagName,role:e.getAttribute('role'),name:e.getAttribute('aria-label')||e.labels?.[0]?.innerText?.trim()||e.innerText?.trim()||'',
        disabled:e.disabled||e.getAttribute('aria-disabled')==='true',x:Math.round(r.x),y:Math.round(r.y),width:Math.round(r.width),height:Math.round(r.height),
        focusOutline:s.outlineStyle+' '+s.outlineWidth,focusShadow:s.boxShadow};}),
    imagesWithoutAlt:[...document.querySelectorAll('img:not([alt])')].length,text:document.body.innerText.slice(0,3000)
  }))()`);
  console.log(JSON.stringify({ completed: true, artifacts, final: accessible }, null, 2));
  ws.close();
} finally {
  browser?.kill(); await sleep(150); await rm(profile, { recursive: true, force: true }).catch(() => {});
  await fixture.close();
}

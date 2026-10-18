#!/usr/bin/env node
// Synthetic-only Review acceptance flow using cached Chromium and loopback CDP.
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
const candidate = arg('candidate', 'Cranberry & Caramel Date Bars');
const evidenceArg = arg('evidence-dir');
const editValue = arg('edited-value', '10 bars');
const instructionValue = arg('instructions', 'Combine the ingredients.\n\nBake until set.');
const executable = resolve(arg('executable', join(process.env.LOCALAPPDATA || '', 'ms-playwright',
  'chromium_headless_shell-1200', 'chrome-headless-shell-win64', 'chrome-headless-shell.exe')));
if (!evidenceArg) {
  throw new Error('Provide --evidence-dir for synthetic browser evidence.');
}
const evidenceDir = await prepareEvidenceDirectory(evidenceArg);
const fixture = await startIsolatedBrowserFixture({ scenario: 'review', requiredCandidates: [candidate] });
const url = fixture.url;

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
  const response = await cdp.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (response.exceptionDetails) throw new Error(response.exceptionDetails.text || 'Browser evaluation failed.');
  return response.result?.value;
}
async function waitFor(cdp, expression, description, timeout = 30000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await evaluate(cdp, expression).catch(() => false)) return;
    await sleep(250);
  }
  throw new Error(`Timed out waiting for ${description}: ${await evaluate(cdp, 'document.body.innerText.slice(0,1600)').catch(() => 'page unavailable')}`);
}
async function waitForStable(cdp, expression, description, stableMs = 600, timeout = 30000) {
  const deadline = Date.now() + timeout;
  let matchedAt = 0;
  while (Date.now() < deadline) {
    if (await evaluate(cdp, expression).catch(() => false)) {
      if (!matchedAt) matchedAt = Date.now();
      if (Date.now() - matchedAt >= stableMs) return;
    } else matchedAt = 0;
    await sleep(100);
  }
  throw new Error(`Timed out waiting for stable ${description}: ${await evaluate(cdp, 'document.body.innerText.slice(0,1600)').catch(() => 'page unavailable')}`);
}
async function clickVisibleText(cdp, text, selector = 'button,a,[role="button"]') {
  const point = await evaluate(cdp, `(() => {
    const value=${JSON.stringify(text)};
    const matches=[...document.querySelectorAll(${JSON.stringify(selector)})].filter(e=>{
      const r=e.getBoundingClientRect(), t=(e.innerText||e.getAttribute('aria-label')||'').replace(/\\s+/g,' ').trim();
      return r.width>0&&r.height>0&&t.includes(value)&&!e.disabled&&e.getAttribute('aria-disabled')!=='true';
    }).sort((a,b)=>a.innerText.length-b.innerText.length);
    const e=matches[0]; if(!e)return null; e.scrollIntoView({block:'center',inline:'nearest'});
    const r=e.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2,text:e.innerText.trim()};
  })()`);
  if (!point) throw new Error(`Could not find enabled visible control containing: ${text}`);
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1 });
  await sleep(250);
  console.error(`Clicked ${JSON.stringify({ text: point.text, x: Math.round(point.x), y: Math.round(point.y) })}`);
}
async function ensureExpanderOpen(cdp, text) {
  const state = await evaluate(cdp, `(() => {const e=[...document.querySelectorAll('[data-testid="stExpander"]')].find(x=>x.innerText.includes(${JSON.stringify(text)}));return e?{open:e.innerText.includes('keyboard_arrow_down'),text:e.innerText.slice(0,120)}:null})()`);
  if (!state) throw new Error(`Could not find expander containing ${text}.`);
  if (!state.open) {
    await clickVisibleText(cdp, text, '[data-testid="stExpander"]');
    await waitFor(cdp, `(()=>{const e=[...document.querySelectorAll('[data-testid="stExpander"]')].find(x=>x.innerText.includes(${JSON.stringify(text)}));return !!e&&e.innerText.includes('keyboard_arrow_down')})()`, `${text} expander open`);
  }
}

async function clickProposalAction(cdp, proposedValue, action) {
  const point = await evaluate(cdp, `(async () => {
    const value=${JSON.stringify(proposedValue)}, action=${JSON.stringify(action)};
    const targets=[...document.querySelectorAll('p,div,span')].filter(e=>e.children.length===0&&e.innerText.trim()===value)
      .sort((a,b)=>a.getBoundingClientRect().width*a.getBoundingClientRect().height-b.getBoundingClientRect().width*b.getBoundingClientRect().height);
    for(const target of targets){
      let node=target;
      for(let depth=0;node&&depth<12;depth+=1,node=node.parentElement){
        const button=[...node.querySelectorAll('button')].find(e=>e.innerText.trim()===action&&!e.disabled);
        if(button){button.scrollIntoView({block:'center',inline:'nearest'});await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
          const rect=button.getBoundingClientRect(),hit=document.elementFromPoint(rect.x+rect.width/2,rect.y+rect.height/2);
          return {x:rect.x+rect.width/2,y:rect.y+rect.height/2,button:button.innerText.trim(),hit:hit?.innerText?.trim()||hit?.tagName||'',hitIsButton:hit===button||button.contains(hit)};
        }
      }
    }
    return null;
  })()`);
  if (!point) throw new Error(`Could not find ${action} for proposal ${proposedValue}.`);
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1 });
  await sleep(250);
  console.error(`Clicked proposal action ${JSON.stringify(point)}`);
}

async function clickComboOption(cdp, label, optionText) {
  const point = await evaluate(cdp, `(() => {
    const e=document.querySelector('input[aria-label='+${JSON.stringify(JSON.stringify(label))}+']');
    if(!e)return null; e.scrollIntoView({block:'center'}); const r=e.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2};
  })()`);
  if (!point) throw new Error(`Could not find combo box: ${label}`);
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1 });
  await waitFor(cdp, `!![...document.querySelectorAll('[role="option"]')].find(e=>e.innerText.includes(${JSON.stringify(optionText)}))`, `${label} option ${optionText}`);
  await clickVisibleText(cdp, optionText, '[role="option"]');
}
async function fillLabel(cdp, label, value) {
  const point = await evaluate(cdp, `(() => {
    const e=[...document.querySelectorAll('input,textarea')].find(x=>x.getAttribute('aria-label')===${JSON.stringify(label)}||[...(x.labels||[])].some(l=>l.innerText.trim()===${JSON.stringify(label)}));
    if(!e)return null; e.scrollIntoView({block:'center'}); const r=e.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2};
  })()`);
  if (!point) throw new Error(`Could not find input labelled: ${label}`);
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'a', code: 'KeyA', modifiers: 2, windowsVirtualKeyCode: 65 });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'a', code: 'KeyA', modifiers: 2, windowsVirtualKeyCode: 65 });
  await cdp.send('Input.insertText', { text: value });
}
async function screenshot(cdp, name) {
  await mkdir(evidenceDir, { recursive: true });
  const image = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  const path = join(evidenceDir, name);
  await writeFile(path, Buffer.from(image.data, 'base64'));
  return path;
}

const profile = await mkdtemp(join(fixture.root, 'browser-profile-'));
let browser;
try {
  browser = spawn(executable, [
    '--headless=new', '--disable-gpu', '--disable-dev-shm-usage', '--disable-background-networking',
    '--disable-component-update', '--disable-sync', '--disable-default-apps', '--disable-extensions',
    '--disable-features=MediaRouter,OptimizationHints', '--no-first-run', '--metrics-recording-only',
    '--disable-quic', '--disable-async-dns', '--proxy-server=http://127.0.0.1:9',
    '--proxy-bypass-list=127.0.0.1;localhost;[::1]', '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1',
    `--user-data-dir=${profile}`, 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });
  const activePort = join(profile, 'DevToolsActivePort');
  let port;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try { port = Number((await readFile(activePort, 'utf8')).split(/\r?\n/)[0]); break; }
    catch { if (browser.exitCode !== null) throw new Error(`Chromium exited with code ${browser.exitCode}.`); await sleep(100); }
  }
  if (!port) throw new Error('Chromium did not expose its local DevTools endpoint.');
  const targetResponse = await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(url)}`, { method: 'PUT' });
  if (!targetResponse.ok) throw new Error(`DevTools target failed: ${targetResponse.status}`);
  const target = await targetResponse.json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolveOpen, reject) => { ws.addEventListener('open', resolveOpen, { once: true }); ws.addEventListener('error', reject, { once: true }); });
  const cdp = new CDP(ws);
  await cdp.send('Page.enable'); await cdp.send('Runtime.enable');
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  const navigationStarted = Date.now();
  await cdp.send('Page.navigate', { url });
  await waitFor(cdp, `document.readyState==='complete'&&!!document.querySelector('[data-testid="stApp"]')&&[...document.querySelectorAll('input[aria-label="Recipe title"]')].some(e=>e.getClientRects().length)`, 'initial Review editor', 45000);
  await waitFor(cdp, `(()=>{const i=[...document.images].find(x=>x.alt.startsWith('Recipe source page'));return !!i&&i.complete&&i.naturalWidth>100&&![...document.querySelectorAll('[data-testid*="skeleton" i],[class*="skeleton" i')].length})()`, 'settled source scan and editor', 60000);
  await assertBrowserFixtureAttestation(cdp, fixture);
  const timings = { firstReviewSettledMs: Date.now() - navigationStarted };
  const evidence = { screenshots: [await screenshot(cdp, 'browser-review-after-precise-1440x900.png')], timings };
  if (arg('inspect') === 'true') {
    console.log(JSON.stringify(await evaluate(cdp, `JSON.stringify({body:document.body.innerText.slice(0,2400),controls:[...document.querySelectorAll('input,textarea,button,[role="tab"]')].map(e=>({tag:e.tagName,role:e.getAttribute('role'),aria:e.getAttribute('aria-label'),text:e.innerText?.trim().slice(0,80),value:e.value?.slice(0,100),disabled:e.disabled})).filter(x=>x.aria||x.text)})`), null, 2));
    ws.close(); browser.kill(); await sleep(150); await rm(profile, { recursive: true, force: true }).catch(() => {});
    await fixture.close();
    process.exit(0);
  }

  await clickVisibleText(cdp, 'Instructions', '[role="tab"]');
  await waitFor(cdp, `!!document.querySelector('textarea[aria-label="Step 1"]')`, 'Instructions section');
  await clickVisibleText(cdp, 'Ingredients', '[role="tab"]');
  await waitFor(cdp, `!!document.querySelector('input[aria-label="Ingredient 1"]')`, 'Ingredients section after round trip');
  await clickVisibleText(cdp, 'Instructions', '[role="tab"]');
  await waitFor(cdp, `!!document.querySelector('textarea[aria-label="Step 1"]')`, 'Instructions section after round trip');
  evidence.sectionRoundTrip = true;

  await clickVisibleText(cdp, 'Bulk edit instructions');
  await waitFor(cdp, `!!document.querySelector('textarea[aria-label="Bulk instructions"]')`, 'dynamic Bulk Edit Instructions editor');
  await fillLabel(cdp, 'Bulk instructions', instructionValue);
  await waitFor(cdp, `document.querySelector('textarea[aria-label="Bulk instructions"]')?.value.includes('Bake until set.')`, 'bulk edit input');
  await clickVisibleText(cdp, 'Cancel bulk edit');
  await waitFor(cdp, `!document.querySelector('textarea[aria-label="Bulk instructions"]')`, 'bulk editor cancel');
  evidence.bulkEditOpenCancel = true;
  evidence.screenshots.push(await screenshot(cdp, 'browser-review-after-bulk-cancel-1440x900.png'));
  await clickVisibleText(cdp, 'Bulk edit instructions');
  await waitFor(cdp, `!!document.querySelector('textarea[aria-label="Bulk instructions"]')`, 'bulk editor reopen');
  const restoredInstructions = await evaluate(cdp, `document.querySelector('textarea[aria-label="Bulk instructions"]')?.value||''`);
  if (!restoredInstructions.includes('Cook rice.') || !restoredInstructions.includes('Fold in egg.')) throw new Error(`Bulk Cancel did not restore original instructions: ${restoredInstructions}`);
  await clickVisibleText(cdp, 'Cancel bulk edit');
  await waitFor(cdp, `!document.querySelector('textarea[aria-label="Bulk instructions"]')`, 'bulk editor close after reopen');
  evidence.bulkEditCancelReopen = true;

  await clickVisibleText(cdp, 'Queue');
  await waitFor(cdp, `!!document.querySelector('input[aria-label="Jump to recipe"]')`, 'recipe queue controls');
  await clickComboOption(cdp, 'Jump to recipe', candidate);
  await waitForStable(cdp, `[...document.querySelectorAll('input[aria-label="Recipe title"]')].some(e=>e.getClientRects().length&&e.value===${JSON.stringify(candidate)})`, 'selected proposal candidate');
  await ensureExpanderOpen(cdp, 'Issues and source notes');
  await clickVisibleText(cdp, 'Ingredients', '[role="tab"]');
  await waitFor(cdp, `document.body.innerText.includes('1 cup dates')&&document.body.innerText.includes('1/2 cup dates')`, 'ingredient proposals');
  await clickProposalAction(cdp, '1 cup dates', 'Reject; keep issue open');
  await waitFor(cdp, `document.body.innerText.includes('Rejected')&&document.body.innerText.includes('Rejecting this proposal leaves the source issue unresolved')`, 'rejected critical proposal and unresolved source issue');
  evidence.screenshots.push(await screenshot(cdp, 'browser-review-after-critical-proposal-reject-1440x900.png'));
  await clickProposalAction(cdp, '1/2 cup dates', 'Edit and use');
  await waitFor(cdp, `!!document.querySelector('textarea[aria-label="Edit and use"]')`, 'proposal editor');
  await fillLabel(cdp, 'Edit and use', '1 1/2 cups dates');
  await clickVisibleText(cdp, 'Use edited version');
  await waitFor(cdp, `!document.body.innerText.includes('1/2 cup dates')`, 'edited ingredient proposal');
  await clickProposalAction(cdp, '12 bars', 'Use suggestion');
  await waitFor(cdp, `!document.body.innerText.includes('12 bars')`, 'accepted optional proposal');
  evidence.proposalActions = { accepted: true, edited: true, rejected: true, criticalFindingRemainedAfterReject: true };

  await clickVisibleText(cdp, 'Queue');
  await clickComboOption(cdp, 'Jump to recipe', 'Fried Rice');
  await waitFor(cdp, `[...document.querySelectorAll('input[aria-label="Recipe title"]')].some(e=>e.getClientRects().length&&e.value==='Fried Rice')`, 'complete recipe for approval');
  evidence.screenshots.push(await screenshot(cdp, 'browser-review-ready-for-approval-1440x900.png'));
  await clickVisibleText(cdp, 'Instructions', '[role="tab"]');
  await waitFor(cdp, `!!document.querySelector('textarea[aria-label="Step 1"]')`, 'Instructions before bulk apply');
  await clickVisibleText(cdp, 'Bulk edit instructions');
  await waitFor(cdp, `!!document.querySelector('textarea[aria-label="Bulk instructions"]')`, 'bulk editor for apply');
  await fillLabel(cdp, 'Bulk instructions', instructionValue);
  await clickVisibleText(cdp, 'Apply instructions');
  await waitFor(cdp, `document.body.innerText.includes('Approve & next')`, 'post-bulk review');
  await waitFor(cdp, `(()=>{const b=[...document.querySelectorAll('button')].find(e=>e.innerText.trim()==='Approve & next');return !!b&&!b.disabled})()`, 'enabled recipe approval');
  evidence.approvalEnabledAfterReview = true;
  evidence.screenshots.push(await screenshot(cdp, 'browser-review-ready-to-approve-1440x900.png'));
  await clickVisibleText(cdp, 'Approve & next');
  await waitFor(cdp, `!document.body.innerText.includes('Approve & next')||document.querySelector('input[aria-label="Recipe title"]')?.value!==${JSON.stringify(candidate)}`, 'approval navigation');
  evidence.approved = true;

  await clickVisibleText(cdp, 'Import & history', 'a,button,[role="button"]');
  await waitFor(cdp, `document.body.innerText.includes('Simulation: these actions use a local fake Mealie service.')`, 'fixture import screen');
  await waitFor(cdp, `!![...document.querySelectorAll('button')].find(e=>e.innerText.includes('Select all eligible recipes'))`, 'eligible approved recipe');
  await clickVisibleText(cdp, 'Select all eligible recipes');
  await clickVisibleText(cdp, 'Preview import');
  await waitFor(cdp, `!![...document.querySelectorAll('button')].find(e=>e.innerText.trim()==='Import 1 recipe')`, 'fake import preview');
  await clickVisibleText(cdp, 'Import 1 recipe');
  await waitFor(cdp, `document.body.innerText.includes('Imported')`, 'fake import result');
  evidence.fakeImport = true;
  evidence.screenshots.push(await screenshot(cdp, 'browser-review-fake-import-result-1440x900.png'));
  evidence.finalUrl = await evaluate(cdp, 'location.href');
  evidence.elapsedMs = Date.now() - navigationStarted;
  console.log(JSON.stringify(evidence, null, 2));
  ws.close();
} finally {
  browser?.kill(); await sleep(150); await rm(profile, { recursive: true, force: true }).catch(() => {});
  await fixture.close();
}

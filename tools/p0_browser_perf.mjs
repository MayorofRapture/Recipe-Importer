#!/usr/bin/env node
// Persistent local-only Chromium session for repeatable P0 interaction timings.
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm, mkdir, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { assertBrowserFixtureAttestation, rejectArbitraryUrl,
  prepareEvidenceFile, startIsolatedBrowserFixture } from './browser_fixture_runner.mjs';

function arg(name, fallback) {
  const prefix = `--${name}=`;
  return process.argv.slice(2).find((value) => value.startsWith(prefix))?.slice(prefix.length) ?? fallback;
}
rejectArbitraryUrl();
const action = arg('action', 'tabs');
const direction = arg('direction', 'next');
const targets = arg('targets', '').split(',').filter(Boolean);
const iterations = Number(arg('iterations', '5'));
const startupOnly = arg('startup-only', 'false') === 'true';
const evidencePath = arg('evidence', '');
const screenshotPath = arg('screenshot', '');
const evidenceOutput = evidencePath ? await prepareEvidenceFile(evidencePath) : '';
const screenshotOutput = screenshotPath ? await prepareEvidenceFile(screenshotPath) : '';
if (!['tabs', 'bulk', 'navigate', 'jump', 'save'].includes(action)
    || (action === 'navigate' && !['next', 'previous'].includes(direction))
    || (action === 'jump' && targets.length !== iterations)
    || !Number.isInteger(iterations) || iterations < 1 || iterations > 10) {
  throw new Error('Use --action=tabs|bulk|navigate|jump|save [--iterations=5]. The harness owns its isolated app.');
}
const fixture = await startIsolatedBrowserFixture({ scenario: 'performance',
  requiredCandidates: ['Fried Rice', ...targets] });
const url = fixture.url;
const recipeFieldPattern = /^(Ingredient\s+\d+|Step\s+\d+)$/i;
if (!recipeFieldPattern.test('Ingredient 1') || !recipeFieldPattern.test('Step 2')
    || recipeFieldPattern.test('Bulk instructions')) throw new Error('Recipe selector regex self-check failed.');
const executable = resolve(arg('executable', join(process.env.LOCALAPPDATA || '', 'ms-playwright',
  'chromium_headless_shell-1200', 'chrome-headless-shell-win64', 'chrome-headless-shell.exe')));
const profile = await mkdtemp(join(fixture.root, 'browser-profile-'));
let browser;
browser = spawn(executable, [
  '--headless=new', '--disable-gpu', '--disable-dev-shm-usage', '--disable-background-networking',
  '--disable-component-update', '--disable-sync', '--disable-default-apps', '--disable-extensions',
  '--disable-features=MediaRouter,OptimizationHints', '--no-first-run', '--metrics-recording-only',
  '--disable-quic', '--disable-async-dns', '--proxy-server=http://127.0.0.1:9',
  '--proxy-bypass-list=127.0.0.1;localhost;[::1]', '--remote-debugging-port=0',
  '--remote-debugging-address=127.0.0.1', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1',
  `--user-data-dir=${profile}`, 'about:blank',
], { stdio: ['ignore', 'ignore', 'pipe'], windowsHide: true });
let browserError = '';
browser.stderr.on('data', (chunk) => { browserError += chunk.toString(); });

class CDP {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map();
    ws.addEventListener('message', async (event) => {
      const raw = typeof event.data === 'string' ? event.data
        : event.data instanceof Blob ? await event.data.text() : Buffer.from(event.data).toString('utf8');
      const message = JSON.parse(raw);
      if (!message.id || !this.pending.has(message.id)) return;
      const pending = this.pending.get(message.id); this.pending.delete(message.id);
      message.error ? pending.reject(new Error(message.error.message)) : pending.resolve(message.result);
    });
  }
  send(method, params = {}) {
    const id = ++this.id; this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 15000);
      this.pending.set(id, { resolve: (value) => { clearTimeout(timer); resolve(value); },
        reject: (error) => { clearTimeout(timer); reject(error); } });
    });
  }
}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function evaluate(cdp, expression) {
  const result = await cdp.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || 'Browser evaluation failed.');
  return result.result?.value;
}
async function waitFor(cdp, expression, description, timeout = 180000) {
  const until = Date.now() + timeout;
  while (Date.now() < until) {
    if (await evaluate(cdp, expression).catch(() => false)) return;
    await sleep(10);
  }
  const state = startupOnly
    ? JSON.stringify({ readyState: await evaluate(cdp, 'document.readyState').catch(() => 'unavailable'),
      gate: await evaluate(cdp, gateExpression).catch(() => null) })
    : await evaluate(cdp, 'document.body.innerText.slice(0,1000)').catch(() => 'page unavailable');
  throw new Error(`Timed out waiting for ${description}: ${state}`);
}
async function buttonPoint(cdp, label) {
  const encoded = JSON.stringify(label);
  const point = await evaluate(cdp, `(() => {
    const wanted=${encoded};
    const button=[...document.querySelectorAll('button')].find(e=>{
      const r=e.getBoundingClientRect(); return (e.innerText||'').replace(/\\s+/g,' ').trim().toLowerCase()===wanted.toLowerCase()
        && r.width>0&&r.height>0&&!e.disabled&&e.getAttribute('aria-disabled')!=='true';
    });
    if(!button)return null; button.scrollIntoView({block:'center',inline:'nearest'});
    const r=button.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2,hit=document.elementFromPoint(x,y);
    return {x,y,label:button.innerText.trim(),hit:hit?.innerText?.trim()||hit?.tagName||'',hitIsButton:hit===button||button.contains(hit)};
  })()`);
  if (!point?.hitIsButton) throw new Error(`Exact enabled button hit-test failed for ${label}: ${JSON.stringify(point)}; state=${JSON.stringify(await evaluate(cdp, `({selected:[...document.querySelectorAll('[role="tab"]')].map(e=>({text:e.innerText.trim(),selected:e.getAttribute('aria-selected')})),buttons:[...document.querySelectorAll('button')].map(e=>({text:e.innerText.trim(),disabled:e.disabled,visible:!!e.getClientRects().length})).filter(e=>e.visible)})`))}`);
  return point;
}
async function tabPoint(cdp, label) {
  const point = await evaluate(cdp, `(() => {
    const wanted=${JSON.stringify(label)};
    const tab=[...document.querySelectorAll('[role="tab"]')].find(e=>e.innerText.trim()===wanted);
    if(!tab)return null;tab.scrollIntoView({block:'center',inline:'nearest'});
    const r=tab.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2,hit=document.elementFromPoint(x,y);
    return {x,y,label:tab.innerText.trim(),hit:hit?.innerText?.trim()||hit?.tagName||'',hitInside:hit===tab||tab.contains(hit)};
  })()`);
  if (!point?.hitInside) throw new Error(`Exact tab hit-test failed for ${label}: ${JSON.stringify(point)}`);
  return point;
}
async function click(cdp, point) {
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1 });
}
const gateExpression = `(() => {
    const skeletons=[...document.querySelectorAll('[data-testid*="skeleton" i],[class*="skeleton" i]')];
    const image=[...document.images].find(e=>e.alt.startsWith('Recipe source page'));
    const fields=[...document.querySelectorAll('input,textarea')].filter(e=>{
      const label=(e.getAttribute('aria-label')||[...(e.labels||[])].map(x=>x.innerText).join(' ')).trim();
      return ${JSON.stringify(recipeFieldPattern.source)} && /^(Ingredient\\s+\\d+|Step\\s+\\d+)$/i.test(label)&&e.value.trim()&&e.getClientRects().length&&!e.disabled;
    });
    const title=[...document.querySelectorAll('input')].find(e=>(e.getAttribute('aria-label')||[...(e.labels||[])].map(x=>x.innerText).join(' ')).trim()==='Recipe title');
    return {usable:!skeletons.length&&!!image?.complete&&image.naturalWidth>100&&!!title?.value?.trim()&&fields.length>0,
      skeletons:skeletons.length,imageWidth:image?.naturalWidth||0,fieldCount:fields.length,titleReady:!!title?.value?.trim()};
  })()`;
async function gate(cdp) { return evaluate(cdp, gateExpression); }
async function readCounters(cdp) {
  return evaluate(cdp, `(()=>{const e=[...document.querySelectorAll('[data-testid="stJson"]')].find(x=>x.innerText.includes('full_page_executions'));const text=e?.innerText||'';const counts={};for(const line of text.split('\\n')){const match=line.match(/^\\s*"([^"]+)":\\s*(\\d+)/);if(match)counts[match[1]]=Number(match[2])}return {counts,raw:text}})()`);
}
async function ensureCountersOpen(cdp) {
  const state = await evaluate(cdp, `(()=>{const e=[...document.querySelectorAll('[data-testid="stExpander"]')].find(x=>x.innerText.includes('Performance counters')&&x.getClientRects().length);if(!e)return null;const h=e.querySelector('summary,[data-testid="stExpanderToggleButton"]')||e;const r=h.getBoundingClientRect();return{open:e.innerText.includes('full_page_executions'),box:{x:r.x+r.width/2,y:r.y+r.height/2}}})()`);
  if (!state) throw new Error('UI diagnostics were enabled but the Performance counters expander was not rendered.');
  if (!state.open) { await click(cdp, state.box); await waitFor(cdp, `!![...document.querySelectorAll('[data-testid="stJson"]')].find(x=>x.innerText.includes('full_page_executions'))`, 'visible UI diagnostic counters', 10000); }
}
let ws;
try {
  const activePort = join(profile, 'DevToolsActivePort');
  let debugPort;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try { debugPort = Number((await readFile(activePort, 'utf8')).split(/\r?\n/)[0]); break; }
    catch { if (browser.exitCode !== null) throw new Error(`Chromium exited ${browser.exitCode}: ${browserError.slice(0, 1200)}`); await sleep(100); }
  }
  if (!debugPort) throw new Error('Chromium did not expose DevTools.');
  const targetResponse = await fetch(`http://127.0.0.1:${debugPort}/json/new?about:blank`, { method: 'PUT' });
  if (!targetResponse.ok) throw new Error(`DevTools target failed: ${targetResponse.status}`);
  const target = await targetResponse.json();
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.addEventListener('open', resolve, { once: true }); ws.addEventListener('error', reject, { once: true }); });
  const cdp = new CDP(ws);
  await cdp.send('Page.enable'); await cdp.send('Runtime.enable');
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  const startupAt = Date.now();
  await cdp.send('Page.navigate', { url });
  await waitFor(cdp, `(document.readyState==='complete'&&!!document.querySelector('[data-testid="stApp"]'))`, 'Streamlit shell');
  await waitFor(cdp, `${gateExpression}.usable`, 'populated selected recipe and source scan');
  await assertBrowserFixtureAttestation(cdp, fixture);
  const coldStartupMs = Date.now() - startupAt;
  const measuredGate = await gate(cdp);
  const sourceGate = startupOnly
    ? { usable: measuredGate.usable, skeletons: measuredGate.skeletons, imageWidth: measuredGate.imageWidth }
    : measuredGate;
  if (!startupOnly) await ensureCountersOpen(cdp);
  const samples = [];
  let priorCounters = startupOnly ? null : await readCounters(cdp);
  for (let run = 0; run < iterations; run += 1) {
    if (startupOnly) break;
    if (action === 'tabs') {
      const before = await evaluate(cdp, `document.querySelector('[role="tab"][aria-selected="true"]')?.innerText.trim()||''`);
      const targetTab = before === 'Instructions' ? 'Ingredients' : 'Instructions';
      const point = await tabPoint(cdp, targetTab);
      const started = Date.now(); await click(cdp, point);
      const fieldLabel = targetTab === 'Instructions' ? 'Step 1' : 'Ingredient 1';
      await waitFor(cdp, `document.querySelector('[role="tab"][aria-selected="true"]')?.innerText.trim()===${JSON.stringify(targetTab)}&&(${gateExpression}).usable&&[...document.querySelectorAll('input,textarea')].some(e=>(e.getAttribute('aria-label')||[...(e.labels||[])].map(x=>x.innerText).join(' ')).trim()===${JSON.stringify(fieldLabel)}&&e.value.trim()&&e.getClientRects().length>0)`, `${targetTab} populated editor`, 15000);
      const counters = await readCounters(cdp);
      samples.push({ action: `${before}->${targetTab}`, latencyMs: Date.now() - started, point, countersBefore: priorCounters, countersAfter: counters });
      priorCounters = counters;
    } else if (action === 'bulk') {
      const active = await evaluate(cdp, `document.querySelector('[role="tab"][aria-selected="true"]')?.innerText.trim()||''`);
      if (active !== 'Instructions') { const p = await tabPoint(cdp, 'Instructions'); await click(cdp, p); await waitFor(cdp, `document.querySelector('[role="tab"][aria-selected="true"]')?.innerText.trim()==='Instructions'&&!!document.querySelector('textarea[aria-label="Step 1"]')?.value?.trim()`, 'populated Instructions editor'); }
      const point = await buttonPoint(cdp, 'Bulk edit instructions');
      const started = Date.now(); await click(cdp, point);
      await waitFor(cdp, `(()=>{const e=[...document.querySelectorAll('textarea')].find(x=>(x.getAttribute('aria-label')||[...(x.labels||[])].map(y=>y.innerText).join(' ')).trim()==='Bulk instructions');const r=e?.getBoundingClientRect();return !!e?.value?.trim()&&r?.width>0&&r?.height>0&&document.body.innerText.includes('Preview:')&&![...document.querySelectorAll('[data-testid*="skeleton" i],[class*="skeleton" i]')].length})()`, 'populated Bulk instructions editor and preview', 15000);
      const counters = await readCounters(cdp);
      samples.push({ action: 'open bulk instructions', latencyMs: Date.now() - started, point, countersBefore: priorCounters, countersAfter: counters });
      priorCounters = counters;
      const cancel = await buttonPoint(cdp, 'Cancel bulk edit'); await click(cdp, cancel);
      await waitFor(cdp, `![...document.querySelectorAll('textarea')].some(e=>(e.getAttribute('aria-label')||[...(e.labels||[])].map(x=>x.innerText).join(' ')).trim()==='Bulk instructions')`, 'bulk editor cancel', 15000);
    } else if (action === 'navigate') {
      const before = await evaluate(cdp, `document.querySelector('input[aria-label="Recipe title"]')?.value||''`);
      let buttonLabel = 'Skip for now';
      if (direction === 'previous') {
        const hasBack = await evaluate(cdp, `[...document.querySelectorAll('button')].some(e=>(e.innerText||'').trim()==='Back'&&e.getClientRects().length>0)`);
        buttonLabel = hasBack ? 'Back' : 'Previous recipe';
        if (!hasBack) {
          const options = await buttonPoint(cdp, 'Options expand_more'); await click(cdp, options);
          await waitFor(cdp, `[...document.querySelectorAll('button')].some(e=>(e.innerText||'').trim()==='Previous recipe'&&e.getClientRects().length>0)`, 'Previous recipe option', 10000);
        }
      }
      const point = await buttonPoint(cdp, buttonLabel);
      const started = Date.now(); await click(cdp, point);
      await waitFor(cdp, `document.querySelector('input[aria-label="Recipe title"]')?.value!==${JSON.stringify(before)}&&(${gateExpression}).usable`, `${direction} populated source and recipe`, 30000);
      const after = await evaluate(cdp, `document.querySelector('input[aria-label="Recipe title"]')?.value||''`);
      const counters = await readCounters(cdp);
      samples.push({ action: `navigate ${direction}`, from: before, to: after, latencyMs: Date.now() - started, point, countersBefore: priorCounters, countersAfter: counters });
      priorCounters = counters;
    } else if (action === 'jump') {
      const target = targets[run];
      const before = await evaluate(cdp, `document.querySelector('input[aria-label="Recipe title"]')?.value||''`);
      const beforeCounters = await readCounters(cdp);
      const queueOpen = await evaluate(cdp, `([...document.querySelectorAll('button')].find(e=>(e.innerText||'').trim().startsWith('Queue'))?.innerText||'').includes('expand_less')`);
      const started = Date.now();
      if (!queueOpen) { const queue = await buttonPoint(cdp, 'Queue expand_more'); await click(cdp, queue); }
      await waitFor(cdp, `!![...document.querySelectorAll('[data-testid="stSelectbox"]')].find(e=>e.innerText.includes('Source file'))`, 'Queue source filter', 10000);
      const control = await evaluate(cdp, `(()=>{const box=[...document.querySelectorAll('[data-testid="stSelectbox"]')].find(e=>e.innerText.includes('Source file'));const control=box?.querySelector('[role="combobox"],input,button,[data-baseweb="select"],div[tabindex="0"]');const r=control?.getBoundingClientRect();if(!r)return {debug:[...document.querySelectorAll('[data-testid="stSelectbox"]')].map(e=>({text:e.innerText.slice(0,80),children:[...e.querySelectorAll('[role="combobox"],input,button,[data-baseweb="select"],div[tabindex="0"]')].map(x=>({tag:x.tagName,role:x.getAttribute('role'),aria:x.getAttribute('aria-label'),base:x.getAttribute('data-baseweb')}))}))};const x=r.x+r.width/2,y=r.y+r.height/2,hit=document.elementFromPoint(x,y);return{x,y,hitInside:!!(hit&&(hit===control||control.contains(hit)))}})()`);
      if (!control?.hitInside) throw new Error(`Source file select hit-test failed: ${JSON.stringify(control)}`);
      await click(cdp, control);
      await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Control', code: 'ControlLeft', modifiers: 2 });
      await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'a', code: 'KeyA', modifiers: 2 });
      await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'a', code: 'KeyA', modifiers: 2 });
      await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Control', code: 'ControlLeft' });
      await cdp.send('Input.insertText', { text: target });
      await waitFor(cdp, `!![...document.querySelectorAll('[role="option"]')].find(e=>e.innerText.trim()===${JSON.stringify(target)}&&e.getClientRects().length)`, `source option ${target}`, 10000);
      const option = await evaluate(cdp, `(()=>{const e=[...document.querySelectorAll('[role="option"]')].find(x=>x.innerText.trim()===${JSON.stringify(target)}&&x.getClientRects().length);if(!e)return null;e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2,hit=document.elementFromPoint(x,y);return{x,y,hitInside:!!(hit&&(hit===e||e.contains(hit)))}})()`);
      if (!option?.hitInside) throw new Error(`Source option physical hit-test failed for ${target}: ${JSON.stringify(option)}`);
      await click(cdp, option);
      const expectedTitle = target.replace(/^scan-(\d{3})\.(?:jpg|pdf)$/i, 'Source $1 Recipe 00');
      await waitFor(cdp, `document.querySelector('input[aria-label="Recipe title"]')?.value===${JSON.stringify(expectedTitle)}&&(${gateExpression}).usable`, `jump to ${expectedTitle}`, 30000);
      const after = await readCounters(cdp);
      const title = await evaluate(cdp, `document.querySelector('input[aria-label="Recipe title"]')?.value||''`);
      samples.push({ action: 'jump by source filter', from: before, to: title, targetSource: target, latencyMs: Date.now() - started, point: option, countersBefore: beforeCounters, countersAfter: after });
      priorCounters = after;
    } else if (action === 'save') {
      const activeTab = await evaluate(cdp, `document.querySelector('[role="tab"][aria-selected="true"]')?.innerText.trim()||''`);
      if (activeTab !== 'Ingredients') {
        const ingredientsTab = await tabPoint(cdp, 'Ingredients'); await click(cdp, ingredientsTab);
        await waitFor(cdp, `document.querySelector('[role="tab"][aria-selected="true"]')?.innerText.trim()==='Ingredients'&&[...document.querySelectorAll('input,textarea')].some(e=>(e.getAttribute('aria-label')||[...(e.labels||[])].map(x=>x.innerText).join(' ')).trim()==='Ingredient 1'&&e.value.trim())`, 'populated Ingredients editor', 15000);
      }
      const ingredient = `P0 synthetic ingredient edit ${Date.now()}-${run}`;
      const ingredientPoint = await evaluate(cdp, `(()=>{const e=[...document.querySelectorAll('input,textarea')].find(x=>(x.getAttribute('aria-label')||[...(x.labels||[])].map(y=>y.innerText).join(' ')).trim()==='Ingredient 1'),r=e?.getBoundingClientRect();return r?{x:r.x+r.width/2,y:r.y+r.height/2}:null})()`);
      if (!ingredientPoint) throw new Error('Ingredient 1 field is missing from the selected Ingredients tab.');
      await click(cdp, ingredientPoint);
      await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Control', code: 'ControlLeft', modifiers: 2 });
      await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'a', code: 'KeyA', modifiers: 2 });
      await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'a', code: 'KeyA', modifiers: 2 });
      await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Control', code: 'ControlLeft' });
      await cdp.send('Input.insertText', { text: ingredient });
      await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Tab', code: 'Tab' });
      await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Tab', code: 'Tab' });
      await waitFor(cdp, `document.body.innerText.includes('Unsaved changes')`, 'edited ingredient marked unsaved', 5000);
      const beforeSaveCounters = await readCounters(cdp);
      const point = await buttonPoint(cdp, 'Save draft');
      const started = Date.now(); await click(cdp, point);
      await waitFor(cdp, `(()=>{const e=[...document.querySelectorAll('[data-testid="stJson"]')].find(x=>x.innerText.includes('full_page_executions'));const lines=(e?.innerText||'').split('\\n');const val=(key)=>{const line=lines.find(x=>x.includes('"'+key+'"'));return line?Number(line.split(':')[1]):0};return (val('full_page_executions')>${beforeSaveCounters.counts.full_page_executions||0}||val('fragment_executions')>${beforeSaveCounters.counts.fragment_executions||0})&&val('durable_writes')>${beforeSaveCounters.counts.durable_writes||0}&&document.body.innerText.includes('Draft saved (revision')})()`, 'draft revision save and rerender', 15000);
      const counters = await readCounters(cdp);
      samples.push({ action: 'save draft', latencyMs: Date.now() - started, point, countersBefore: priorCounters, countersAfter: counters });
      priorCounters = counters;
    } else {
      throw new Error(`Unsupported action: ${action}`);
    }
  }
  const elapsed = samples.map(x => x.latencyMs).sort((a, b) => a - b);
  const p95 = elapsed[Math.min(elapsed.length - 1, Math.ceil(elapsed.length * 0.95) - 1)];
  const result = { url, action, coldStartupMs, sourceGate, samples,
    summary: { count: elapsed.length, medianMs: elapsed[Math.floor(elapsed.length / 2)], p95Ms: p95, worstMs: elapsed.at(-1) } };
  if (screenshotPath && !startupOnly) {
    const queueOpen = await evaluate(cdp, `([...document.querySelectorAll('button')].find(e=>(e.innerText||'').trim().startsWith('Queue'))?.innerText||'').includes('expand_less')`);
    if (queueOpen) { const point = await buttonPoint(cdp, 'Queue expand_less'); await click(cdp, point); }
    const countersHeader = await evaluate(cdp, `(()=>{const e=[...document.querySelectorAll('[data-testid="stExpander"]')].find(x=>x.innerText.includes('Performance counters'));const h=e?.querySelector('summary,[data-testid="stExpanderToggleButton"]');const r=h?.getBoundingClientRect();if(!r)return null;const x=r.x+r.width/2,y=r.y+r.height/2,hit=document.elementFromPoint(x,y);return{x,y,open:!!e.querySelector('[data-testid="stJson"]'),hitInside:!!(hit&&(hit===h||h.contains(hit)))}})()`);
    if (countersHeader?.open&&countersHeader.hitInside) await click(cdp, countersHeader);
    const screenshot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
    const path = screenshotOutput;
    await writeFile(path, Buffer.from(screenshot.data, 'base64')); result.screenshot = path;
  }
  if (evidenceOutput) { const path = evidenceOutput; await writeFile(path, JSON.stringify(result, null, 2), 'utf8'); result.evidence = path; }
  console.log(JSON.stringify(result, null, 2));
} finally {
  ws?.close(); browser.kill(); await sleep(150); await rm(profile, { recursive: true, force: true }).catch(() => {});
  await fixture.close();
}

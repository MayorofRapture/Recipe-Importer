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
let activeContext = null;
async function evaluate(cdp, expression) {
  const response = await cdp.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (response.exceptionDetails) throw new Error(response.exceptionDetails.text || 'Browser evaluation failed.');
  return response.result?.value;
}
async function browserDiagnostics(cdp) {
  const snapshot = await evaluate(cdp, `JSON.stringify((() => {
    const visible = (element) => element.getClientRects().length > 0;
    const title = [...document.querySelectorAll('input[aria-label="Recipe title"]')]
      .find(visible)?.value || null;
    const controls = [...document.querySelectorAll('input[aria-label],textarea[aria-label]')]
      .filter(visible).map((element) => ({
        label: element.getAttribute('aria-label'),
        value: /queue|recipe|source|status|issue type|severity/i.test(element.getAttribute('aria-label'))
          ? element.value : undefined,
        disabled: element.disabled,
      }));
    return {
      selectedTitle: title,
      focus: (() => { const element = document.activeElement; return {
        tag: element?.tagName || null,
        label: element?.getAttribute('aria-label') || null,
        value: element?.value || null,
        text: (element?.innerText || '').trim().slice(0, 80),
        visible: !!element?.getClientRects().length,
      }; })(),
      actionContext: ${JSON.stringify(activeContext)},
      activeTabs: [...document.querySelectorAll('[role="tab"][aria-selected="true"]')]
        .filter(visible).map((element) => element.innerText.trim()),
      queueAndFilters: controls,
      visibleActions: [...document.querySelectorAll('button')].filter(visible)
        .map((element) => ({ label: element.getAttribute('aria-label') || element.innerText.trim(),
          disabled: element.disabled })).filter((item) => item.label).slice(-18),
      selectedRecipeTags: [...document.querySelectorAll('[data-baseweb="tag"]')].filter(visible)
        .map((element) => element.innerText.trim()),
    };
  })())`);
  return JSON.parse(snapshot || '{}');
}
async function waitFor(cdp, expression, description, timeout = 30000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await evaluate(cdp, expression).catch(() => false)) return;
    await sleep(250);
  }
  throw new Error(`Timed out waiting for ${description}; browser state: ${JSON.stringify(await browserDiagnostics(cdp).catch(() => ({ page: 'unavailable' })))}.`);
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
  throw new Error(`Timed out waiting for stable ${description}; browser state: ${JSON.stringify(await browserDiagnostics(cdp).catch(() => ({ page: 'unavailable' })))}.`);
}
async function assertActiveCandidate(cdp, fixture, intended, { tab, revision } = {}) {
  const candidateIds = fixture.candidateIds?.[intended] || [];
  if (candidateIds.length !== 1) {
    throw new Error(`Fixture must identify one candidate ID for ${JSON.stringify(intended)}; found ${candidateIds.length}.`);
  }
  activeContext = { intendedTitle: intended, expectedCandidateId: candidateIds[0], expectedRevision: revision ?? 0, expectedTab: tab || null };
  await waitForStable(cdp, `(() => {
    const title = [...document.querySelectorAll('input[aria-label="Recipe title"]')]
      .find((element) => element.getClientRects().length)?.value;
    const activeTab = [...document.querySelectorAll('[role="tab"][aria-selected="true"]')]
      .filter((element) => element.getClientRects().length).map((element) => element.innerText.trim());
    return title === ${JSON.stringify(intended)} && ${tab ? `activeTab.includes(${JSON.stringify(tab)})` : 'true'};
  })()`, `candidate ${intended} at saved revision ${revision ?? 0}`, 1600);
  const state = await browserDiagnostics(cdp);
  const visitPath = join(fixture.output, 'review_queue_events.jsonl');
  const visits = (await readFile(visitPath, 'utf8')).split(/\r?\n/).filter(Boolean)
    .map((line) => JSON.parse(line)).filter((event) => event.event === 'visit');
  const actualCandidateId = visits.at(-1)?.candidate_id || null;
  const eventsPath = join(fixture.output, 'review_events.jsonl');
  const reviewEvents = (await readFile(eventsPath, 'utf8')).split(/\r?\n/).filter(Boolean)
    .map((line) => JSON.parse(line));
  const activeRevision = reviewEvents.filter((event) => event.event === 'review_revision'
    && event.candidate_id === candidateIds[0]).at(-1)?.revision || 0;
  if (actualCandidateId !== candidateIds[0] || activeRevision !== (revision ?? 0)) {
    throw new Error(`Candidate guard failed before consequential action; intended=${JSON.stringify(intended)}, expectedId=${candidateIds[0]}, actualId=${actualCandidateId}, expectedRevision=${revision ?? 0}, actualRevision=${activeRevision}, browser=${JSON.stringify(state)}.`);
  }
  return candidateIds[0];
}
async function waitForCandidateSelected(cdp, fixture, intended, timeout = 30000) {
  const expectedId = fixture.candidateIds?.[intended]?.[0];
  if (!expectedId || fixture.candidateIds[intended].length !== 1) {
    throw new Error(`Fixture must identify one candidate ID for ${JSON.stringify(intended)}.`);
  }
  const path = join(fixture.output, 'review_queue_events.jsonl');
  const deadline = Date.now() + timeout;
  let actualId = null;
  let state = {};
  let matchedAt = 0;
  while (Date.now() < deadline) {
    state = await browserDiagnostics(cdp).catch(() => ({ page: 'unavailable' }));
    try {
      const events = (await readFile(path, 'utf8')).split(/\r?\n/).filter(Boolean)
        .map((line) => JSON.parse(line));
      actualId = events.filter((event) => event.event === 'visit').at(-1)?.candidate_id || null;
    } catch { actualId = null; }
    const selectedTitle = state.queueAndFilters?.find((item) => item.label === 'Jump to recipe')?.value;
    if (actualId === expectedId && state.selectedTitle === intended && selectedTitle === intended) {
      if (!matchedAt) matchedAt = Date.now();
      if (Date.now() - matchedAt >= 3000) return assertActiveCandidate(cdp, fixture, intended, { revision: 0 });
    } else matchedAt = 0;
    await sleep(250);
  }
  throw new Error(`Candidate selection did not settle; intended=${JSON.stringify(intended)}, expectedId=${expectedId}, actualId=${actualId}, browser=${JSON.stringify(state)}.`);
}
async function assertReviewEvent(fixture, { candidateId, issueId, disposition }) {
  const path = join(fixture.output, 'review_events.jsonl');
  const lines = (await readFile(path, 'utf8')).split(/\r?\n/).filter(Boolean);
  const event = lines.map((line) => JSON.parse(line)).reverse().find((item) =>
    item.event === 'review_issue_resolution' && item.issue_id === issueId && item.disposition === disposition);
  if (!event || event.candidate_id !== candidateId || !event.target_id) {
    const related = lines.map((line) => JSON.parse(line)).filter((item) => item.issue_id === issueId)
      .map((item) => ({ event: item.event, candidate_id: item.candidate_id,
        disposition: item.disposition, revision: item.revision, reason: item.reason }));
    throw new Error(`Review event identity check failed; intended=${JSON.stringify({ candidateId, issueId, disposition })}, actual=${JSON.stringify(event && { candidate_id: event.candidate_id, issue_id: event.issue_id, disposition: event.disposition, target_id: event.target_id })}, related=${JSON.stringify(related)}.`);
  }
  return event;
}
async function waitForReviewRevision(fixture, candidateId, expectedRevision, timeout = 30000) {
  const path = join(fixture.output, 'review_events.jsonl');
  const deadline = Date.now() + timeout;
  let actualRevision = 0;
  while (Date.now() < deadline) {
    const events = (await readFile(path, 'utf8')).split(/\r?\n/).filter(Boolean)
      .map((line) => JSON.parse(line));
    actualRevision = events.filter((event) => event.event === 'review_revision'
      && event.candidate_id === candidateId).at(-1)?.revision || 0;
    if (actualRevision === expectedRevision) return;
    await sleep(250);
  }
  throw new Error(`Saved review revision did not settle; candidateId=${candidateId}, expected=${expectedRevision}, actual=${actualRevision}.`);
}
async function waitForReviewResolution(fixture, { candidateId, issueId, disposition }, timeout = 30000) {
  const path = join(fixture.output, 'review_events.jsonl');
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const events = (await readFile(path, 'utf8')).split(/\r?\n/).filter(Boolean)
      .map((line) => JSON.parse(line));
    const event = events.findLast((item) => item.event === 'review_issue_resolution'
      && item.candidate_id === candidateId && item.issue_id === issueId
      && item.disposition === disposition);
    if (event?.target_id) return event;
    await sleep(100);
  }
  throw new Error(`Review resolution did not persist; candidateId=${candidateId}, issueId=${issueId}, disposition=${disposition}.`);
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
  if (!point) throw new Error(`Could not find enabled visible control containing ${JSON.stringify(text)}; browser state: ${JSON.stringify(await browserDiagnostics(cdp))}.`);
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1 });
  await evaluate(cdp, 'new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
  console.error(`Clicked ${JSON.stringify({ text: point.text, x: Math.round(point.x), y: Math.round(point.y) })}`);
}
async function pressKey(cdp, key, code, windowsVirtualKeyCode) {
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key, code, windowsVirtualKeyCode });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key, code, windowsVirtualKeyCode });
}
async function tabToSourcePageControl(cdp) {
  const trail = [];
  for (let index = 0; index < 50; index += 1) {
    await pressKey(cdp, 'Tab', 'Tab', 9);
    const focus = await evaluate(cdp, `(() => { const e=document.activeElement; return {
      tag:e?.tagName||'', role:e?.getAttribute('role')||'', label:e?.getAttribute('aria-label')||'',
      visible:!!e?.getClientRects().length, focusedText:(e?.innerText||'').trim().slice(0,60)
    }; })()`);
    trail.push(focus);
    if (focus.label === 'Page' && focus.visible) return trail;
  }
  throw new Error(`Keyboard Tab did not reach the source page control; focus trail=${JSON.stringify(trail)}.`);
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
  const result = await evaluate(cdp, `(async () => {
    const value=${JSON.stringify(proposedValue)}, action=${JSON.stringify(action)};
    const targets=[...document.querySelectorAll('p,div,span')].filter(e=>e.children.length===0&&e.innerText.trim()===value)
      .sort((a,b)=>a.getBoundingClientRect().width*a.getBoundingClientRect().height-b.getBoundingClientRect().width*b.getBoundingClientRect().height);
    for(const target of targets){
      let node=target;
      for(let depth=0;node&&depth<30;depth+=1,node=node.parentElement){
        const button=[...node.querySelectorAll('button')].find(e=>e.innerText.trim()===action&&!e.disabled);
        if(button){button.scrollIntoView({block:'center',inline:'nearest'});await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
          const rect=button.getBoundingClientRect(),hit=document.elementFromPoint(rect.x+rect.width/2,rect.y+rect.height/2);
          return {x:rect.x+rect.width/2,y:rect.y+rect.height/2,button:button.innerText.trim(),hit:hit?.innerText?.trim()||hit?.tagName||'',hitIsButton:hit===button||button.contains(hit)};
        }
      }
    }
    const matches=[...document.querySelectorAll('p,div,span')].filter(e=>(e.innerText||'').includes(value));
    const matchingActions=[...document.querySelectorAll('button')].filter(button=>button.innerText.trim()===action)
      .map((button)=>{const ancestors=[];let node=button;for(let depth=0;node&&depth<6;depth+=1,node=node.parentElement)
        ancestors.push({tag:node.tagName,text:(node.innerText||'').trim().slice(0,180)});return ancestors;});
    return {point:null,diagnostic:{proposalMatches:matches.slice(0,5).map((target)=>{
      const ancestors=[];let node=target;
      for(let depth=0;node&&depth<5;depth+=1,node=node.parentElement){
        ancestors.push({tag:node.tagName,role:node.getAttribute('role'),text:(node.innerText||'').trim().slice(0,100),
          buttons:[...node.querySelectorAll('button')].map((button)=>({label:button.innerText.trim(),disabled:button.disabled}))});
      }
      return ancestors;
    }),matchingActions}};
  })()`);
  const point = result?.point || result;
  if (!point?.x) throw new Error(`Could not find ${action} for proposal ${JSON.stringify(proposedValue)}; control matches=${JSON.stringify(result?.diagnostic || [])}; browser state: ${JSON.stringify(await browserDiagnostics(cdp))}.`);
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1 });
  await evaluate(cdp, 'new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
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
  const friedRiceId = await assertActiveCandidate(cdp, fixture, 'Fried Rice', { revision: 0 });
  const timings = { firstReviewSettledMs: Date.now() - navigationStarted };
  const evidence = { screenshots: [await screenshot(cdp, 'browser-review-after-precise-1440x900.png')], timings };
  const firstSourceText = await evaluate(cdp, `(() => {
    const sections=[...document.querySelectorAll('[data-testid="stText"]')]
      .filter(element=>element.getClientRects().length).map(element=>element.innerText.trim());
    return JSON.stringify({sections, body:document.body.innerText.includes('Source text')});
  })()`);
  const firstSource = JSON.parse(firstSourceText || '{}');
  if (!firstSource.body || !firstSource.sections.some(text=>text.includes('Fried Rice')&&text.includes('Cook rice.'))
      || firstSource.sections.some(text=>text.includes('The Best Caramel Apples'))) {
    throw new Error(`Source text did not show only the selected source page: ${JSON.stringify(firstSource)}.`);
  }
  evidence.sourceTextStyleAudit = await evaluate(cdp, `(() => {
    const color=(value)=>{const m=value.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);return m?m.slice(1,4).map(Number):null};
    const luminance=(rgb)=>{const values=rgb.map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);return .2126*values[0]+.7152*values[1]+.0722*values[2]};
    const ratio=(a,b)=>{const x=luminance(a),y=luminance(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05)};
    const text=[...document.querySelectorAll('[data-testid="stText"]')].find(e=>e.innerText.includes('Cook rice.'));
    const caption=[...document.querySelectorAll('[data-testid="stCaptionContainer"]')].find(e=>e.innerText.includes('Read-only'));
    const read=(element)=>{if(!element)return null;const style=getComputedStyle(element),fg=color(style.color);let bg=element;let background=null;
      while(bg){const parsed=color(getComputedStyle(bg).backgroundColor);if(parsed&&getComputedStyle(bg).backgroundColor!=='rgba(0, 0, 0, 0)'){background=parsed;break}bg=bg.parentElement}
      background=background||[255,255,255];return {color:style.color,background:'rgb('+background.join(', ')+')',fontSize:style.fontSize,contrast:fg?Number(ratio(fg,background).toFixed(2)):null};};
    return {sourceText:read(text),provenance:read(caption)};
  })()`);
  const initialAx = await cdp.send('Accessibility.getFullAXTree');
  const axNodes = initialAx.nodes || [];
  const sourceHeading = axNodes.some(node=>node.role?.value==='heading'&&node.name?.value==='Source text');
  const sourceTextNode = axNodes.some(node=>node.name?.value?.includes('Cook rice.')
    && (node.role?.value==='StaticText'||node.role?.value==='generic'));
  const pageControlNode = axNodes.some(node=>node.role?.value==='combobox'&&node.name?.value==='Page');
  if (!sourceHeading || !sourceTextNode || !pageControlNode) {
    throw new Error(`Source text or its page control is missing from the accessibility tree: ${JSON.stringify({sourceHeading,sourceTextNode,pageControlNode,
      candidates:axNodes.filter(node=>node.name?.value?.includes('Source text')||node.name?.value?.includes('Cook rice.'))
        .map(node=>({role:node.role?.value,name:node.name?.value}))})}.`);
  }
  evidence.sourceTextAccessibilityTree = { heading: true, sourceText: true, pageControl: true };
  const focusTrail = await tabToSourcePageControl(cdp);
  const focusState = await evaluate(cdp, `(() => {const e=document.activeElement,s=getComputedStyle(e);return {
    label:e.getAttribute('aria-label'),outlineStyle:s.outlineStyle,outlineWidth:s.outlineWidth,
    outlineColor:s.outlineColor,boxShadow:s.boxShadow,borderColor:s.borderColor,
    rect:(()=>{const r=e.getBoundingClientRect();return {width:r.width,height:r.height}})()
  }})()`);
  evidence.sourcePageKeyboardFocus = focusState;
  evidence.screenshots.push(await screenshot(cdp, 'browser-review-source-page-keyboard-focus.png'));
  await pressKey(cdp, 'ArrowDown', 'ArrowDown', 40);
  await pressKey(cdp, 'Enter', 'Enter', 13);
  await waitFor(cdp, `(() => {
    const text=[...document.querySelectorAll('[data-testid="stText"]')].filter(e=>e.getClientRects().length)
      .map(e=>e.innerText.trim()).join(' ');
    return text.includes('The Best Caramel Apples')&&!text.includes('Cook rice.');
  })()`, 'keyboard-selected source page text');
  evidence.sourceTextKeyboardNavigation = { tabsToPageControl: focusTrail.length };
  await evaluate(cdp, `(() => {const h=[...document.querySelectorAll('h1,h2,h3,h4')]
    .find(e=>e.innerText.trim()==='Source text');h?.scrollIntoView({block:'center'});return !!h})()`);
  evidence.screenshots.push(await screenshot(cdp, 'browser-review-source-text-readable.png'));
  await pressKey(cdp, 'ArrowUp', 'ArrowUp', 38);
  await pressKey(cdp, 'Enter', 'Enter', 13);
  await waitFor(cdp, `(() => [...document.querySelectorAll('[data-testid="stText"]')]
    .filter(e=>e.getClientRects().length).some(e=>e.innerText.includes('Cook rice.')))()`, 'return to source page one');
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

  await assertActiveCandidate(cdp, fixture, 'Fried Rice', { revision: 0 });
  await clickVisibleText(cdp, 'Queue');
  await waitFor(cdp, `!!document.querySelector('input[aria-label="Jump to recipe"]')`, 'recipe queue controls');
  await clickComboOption(cdp, 'Jump to recipe', candidate);
  const cranberryId = await waitForCandidateSelected(cdp, fixture, candidate);
  await waitFor(cdp, `document.activeElement?.getAttribute('aria-label')==='Recipe title'`, 'focus restored after candidate navigation');
  const candidateFocus = await evaluate(cdp, `(() => {const e=document.activeElement;return {tag:e?.tagName||'',label:e?.getAttribute('aria-label')||'',value:e?.value||''}})()`);
  if (candidateFocus.label !== 'Recipe title') throw new Error(`Candidate navigation did not restore focus to Recipe title: ${JSON.stringify(candidateFocus)}.`);
  evidence.focusAfterCandidateNavigation = candidateFocus;
  await ensureExpanderOpen(cdp, 'Issues and source notes');
  await clickVisibleText(cdp, 'Ingredients', '[role="tab"]');
  await assertActiveCandidate(cdp, fixture, candidate, { tab: 'Ingredients', revision: 0 });
  await waitFor(cdp, `document.body.innerText.includes('1 cup dates')`, 'critical ingredient proposal');
  await clickProposalAction(cdp, '1 cup dates', 'Reject; keep issue open');
  await waitFor(cdp, `document.body.innerText.includes('Rejected')&&document.body.innerText.includes('Rejecting this proposal leaves the source issue unresolved')`, 'rejected critical proposal and unresolved source issue');
  await waitForReviewRevision(fixture, cranberryId, 1);
  await assertActiveCandidate(cdp, fixture, candidate, { tab: 'Ingredients', revision: 1 });
  const rejectedEvent = await assertReviewEvent(fixture, {
    candidateId: cranberryId, issueId: 'cranberry-quantity', disposition: 'rejected',
  });
  evidence.screenshots.push(await screenshot(cdp, 'browser-review-after-critical-proposal-reject-1440x900.png'));
  await assertActiveCandidate(cdp, fixture, candidate, { tab: 'Ingredients', revision: 1 });
  await clickProposalAction(cdp, '1 cup cranberries', 'Edit and use');
  await waitFor(cdp, `!!document.querySelector('textarea[aria-label="Edit and use"]')`, 'proposal editor');
  await fillLabel(cdp, 'Edit and use', '2 cups cranberries');
  await assertActiveCandidate(cdp, fixture, candidate, { tab: 'Ingredients', revision: 1 });
  await clickVisibleText(cdp, 'Use edited version');
  await waitForReviewRevision(fixture, cranberryId, 2);
  await waitForStable(cdp, `document.querySelector('input[aria-label="Ingredient 2"]')?.value==='2 cups cranberries'`, 'edited ingredient proposal visible');
  await assertActiveCandidate(cdp, fixture, candidate, { tab: 'Ingredients', revision: 2 });
  const editedEvent = await assertReviewEvent(fixture, {
    candidateId: cranberryId, issueId: 'cranberry-quantity-alternative', disposition: 'edited',
  });
  if (editedEvent.final_value !== '2 cups cranberries' ||
      editedEvent.original_issue?.field !== 'ingredients' ||
      editedEvent.original_issue?.source_target_id === rejectedEvent.original_issue?.source_target_id) {
    throw new Error(`Edited proposal target check failed; actual=${JSON.stringify({ final_value: editedEvent.final_value, field: editedEvent.original_issue?.field })}.`);
  }
  await waitFor(cdp, `document.body.innerText.includes('1 critical source issue unresolved')`, 'independent critical finding remains unresolved after instruction edit');
  await ensureExpanderOpen(cdp, 'Issues and source notes');
  await assertActiveCandidate(cdp, fixture, candidate, { revision: 2 });
  await clickProposalAction(cdp, '12 bars', 'Accept inferred suggestion');
  await waitForReviewRevision(fixture, cranberryId, 3);
  await waitForReviewResolution(fixture, {
    candidateId: cranberryId, issueId: 'cranberry-yield', disposition: 'accepted',
  });
  const yieldEvent = await assertReviewEvent(fixture, {
    candidateId: cranberryId, issueId: 'cranberry-yield', disposition: 'accepted',
  });
  if (yieldEvent.original_issue?.field !== 'yield_text') {
    throw new Error(`Optional yield proposal target check failed; actual field=${yieldEvent.original_issue?.field}.`);
  }
  await assertActiveCandidate(cdp, fixture, candidate, { revision: 3 });
  const savedYield = (await readFile(join(fixture.output, 'review_events.jsonl'), 'utf8'))
    .split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line))
    .findLast((event) => event.event === 'review_revision' && event.candidate_id === cranberryId);
  if (savedYield?.recipe?.yield_text !== '12 bars') {
    throw new Error(`Accepted optional yield did not reach the saved recipe revision: ${JSON.stringify(savedYield?.recipe?.yield_text)}.`);
  }
  if (rejectedEvent.candidate_id !== editedEvent.candidate_id ||
      editedEvent.candidate_id !== yieldEvent.candidate_id ||
      rejectedEvent.target_id === editedEvent.target_id ||
      editedEvent.target_id === yieldEvent.target_id) {
    throw new Error('Proposal decisions did not retain their expected candidate and distinct source targets.');
  }
  evidence.proposalActions = { accepted: true, edited: true, rejected: true, criticalFindingRemainedAfterReject: true };

  await clickVisibleText(cdp, 'Queue');
  await assertActiveCandidate(cdp, fixture, candidate, { revision: 3 });
  await clickComboOption(cdp, 'Jump to recipe', 'Fried Rice');
  await waitForCandidateSelected(cdp, fixture, 'Fried Rice');
  evidence.screenshots.push(await screenshot(cdp, 'browser-review-ready-for-approval-1440x900.png'));
  await clickVisibleText(cdp, 'Instructions', '[role="tab"]');
  await waitFor(cdp, `!!document.querySelector('textarea[aria-label="Step 1"]')&&!!document.querySelector('textarea[aria-label="Step 2"]')`, 'Instructions before approval edit');
  const approvedInstructions = instructionValue.split(/\n{2,}/).map((item) => item.trim()).filter(Boolean);
  if (approvedInstructions.length !== 2) throw new Error('The browser fixture expects exactly two instruction steps.');
  await fillLabel(cdp, 'Step 1', approvedInstructions[0]);
  await fillLabel(cdp, 'Step 2', approvedInstructions[1]);
  await clickVisibleText(cdp, 'Save draft');
  await waitForReviewRevision(fixture, friedRiceId, 1);
  await assertActiveCandidate(cdp, fixture, 'Fried Rice', { tab: 'Instructions', revision: 1 });
  await waitFor(cdp, `(()=>{const b=[...document.querySelectorAll('button')].find(e=>e.innerText.trim()==='Approve & next');return !!b&&!b.disabled})()`, 'enabled recipe approval');
  await assertActiveCandidate(cdp, fixture, 'Fried Rice', { tab: 'Instructions', revision: 1 });
  evidence.approvalEnabledAfterReview = true;
  evidence.screenshots.push(await screenshot(cdp, 'browser-review-ready-to-approve-1440x900.png'));
  await clickVisibleText(cdp, 'Approve & next');
  await waitFor(cdp, `document.body.innerText.includes('Imported')||document.querySelector('input[aria-label="Recipe title"]')?.value!==${JSON.stringify('Fried Rice')}`, 'approval navigation');
  await waitFor(cdp, `(() => {const title=[...document.querySelectorAll('input[aria-label="Recipe title"]')].find(e=>e.getClientRects().length);return !!title?.value&&document.activeElement===title})()`, 'focus follows the selected recipe after Approve & next candidate navigation');
  evidence.focusAfterApprovalNavigation = await evaluate(cdp, `(() => {const e=document.activeElement;return {tag:e?.tagName||'',label:e?.getAttribute('aria-label')||'',value:e?.value||''}})()`);
  const reviewEvents = (await readFile(join(fixture.output, 'review_events.jsonl'), 'utf8'))
    .split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
  const approval = reviewEvents.findLast((event) => event.event === 'review_approval' && event.candidate_id === friedRiceId);
  if (!approval || approval.revision !== 1) throw new Error(`Approval did not persist for expected candidate/revision: ${JSON.stringify({ expectedId: friedRiceId, approval: approval && { candidate_id: approval.candidate_id, revision: approval.revision } })}.`);
  evidence.approved = true;

  await clickVisibleText(cdp, 'Import & history', 'a,button,[role="button"]');
  await waitFor(cdp, `document.body.innerText.includes('Simulation: these actions use a local fake Mealie service.')`, 'fixture import screen');
  await waitFor(cdp, `!![...document.querySelectorAll('button')].find(e=>e.innerText.includes('Select all eligible recipes'))`, 'eligible approved recipe');
  await clickVisibleText(cdp, 'Select all eligible recipes');
  await waitFor(cdp, `(()=>{const b=[...document.querySelectorAll('button')].find(e=>e.innerText.trim()==='Preview import');return !!b&&!b.disabled})()`, 'selected recipe ready for import preview');
  await clickVisibleText(cdp, 'Preview import');
  await waitFor(cdp, `!![...document.querySelectorAll('button')].find(e=>e.innerText.trim()==='Import 1 recipe')`, 'fake import preview');
  await clickVisibleText(cdp, 'Import 1 recipe');
  await waitFor(cdp, `document.body.innerText.includes('Fried Rice · Imported')`, 'completed fake import result');
  const fakeMealie = JSON.parse(await readFile(join(fixture.output, 'fixture_mealie.json'), 'utf8'));
  const created = Object.values(fakeMealie.recipes).find((recipe) => recipe.name === 'Fried Rice');
  if (!created || created.payload.recipeInstructions?.map((item) => item.text).join(' ') !== instructionValue.replace(/\n+/g, ' ')) {
    throw new Error('Approved saved instructions did not reach the synthetic Mealie recipe payload.');
  }
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

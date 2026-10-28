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
const stateRegression = arg('state-regression') === 'true';
const d2Only = arg('d2-only') === 'true';
const d2Corrections = arg('d2-corrections') === 'true';
const evidenceArg = arg('evidence-dir');
const editValue = arg('edited-value', '10 bars');
const instructionValue = arg('instructions', 'Combine the ingredients.\n\nBake until set.');
const viewportWidth = Number(arg('inspect-width', '1440'));
const executable = resolve(arg('executable', join(process.env.LOCALAPPDATA || '', 'ms-playwright',
  'chromium_headless_shell-1200', 'chrome-headless-shell-win64', 'chrome-headless-shell.exe')));
if (!evidenceArg) {
  throw new Error('Provide --evidence-dir for synthetic browser evidence.');
}
const evidenceDir = await prepareEvidenceDirectory(evidenceArg);
const fixture = await startIsolatedBrowserFixture({
  scenario: stateRegression ? 'review-state' : d2Corrections ? 'review-d2-corrections' : d2Only ? 'review-d2' : 'review',
  requiredCandidates: stateRegression ? ['Fried Rice', 'Synthetic Manual Directions'] : [candidate],
});
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
      paneChoices: [...document.querySelectorAll('[role="radio"]')].filter(visible)
        .map((element) => ({ text: element.innerText.trim(), checked: element.getAttribute('aria-checked'),
          selected: element.getAttribute('aria-pressed'), tabIndex: element.tabIndex })),
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
async function clickPane(cdp, text) {
  const activated = await evaluate(cdp, `(() => {
    const e=[...document.querySelectorAll('.st-key-review-pane-switcher [role="radio"]')]
      .find(option=>option.innerText.trim()===${JSON.stringify(text)});
    if(!e)return false; e.scrollIntoView({block:'center',inline:'nearest'}); e.focus(); e.click(); return true;
  })()`);
  if (!activated) throw new Error(`Could not find the ${text} pane option.`);
  await evaluate(cdp, 'new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
  await waitFor(cdp, `!![...document.querySelectorAll('.st-key-review-pane-switcher [role="radio"]')].find(e=>e.innerText.trim()===${JSON.stringify(text)}&&e.getAttribute('aria-checked')==='true')`, `${text} pane selection`);
  const focusedWithinSwitcher = await evaluate(cdp, `document.querySelector('.st-key-review-pane-switcher')?.contains(document.activeElement)===true`);
  if (!focusedWithinSwitcher) throw new Error(`Focus did not remain with the Review pane switcher after choosing ${text}.`);
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
  const inlineLabel = `Proposal · ${proposedValue}`;
  const inlineProposal = await evaluate(cdp, `!![...document.querySelectorAll('[data-testid="stExpander"]')]
    .find(e=>e.innerText.includes(${JSON.stringify(inlineLabel)}))`);
  if (inlineProposal) await ensureExpanderOpen(cdp, inlineLabel);
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
async function selectNextComboOption(cdp, label, expectedValue) {
  await evaluate(cdp, `(() => { const element=document.querySelector('input[aria-label='+${JSON.stringify(JSON.stringify(label))}+']'); element?.focus(); return !!element; })()`);
  await pressKey(cdp, 'ArrowDown', 'ArrowDown', 40);
  await pressKey(cdp, 'Enter', 'Enter', 13);
  await waitFor(cdp, `document.querySelector('input[aria-label='+${JSON.stringify(JSON.stringify(label))}+']')?.value===${JSON.stringify(expectedValue)}`, `${label} set to ${expectedValue}`);
}
async function selectPreviousComboOption(cdp, label, expectedValue) {
  await evaluate(cdp, `(() => { const element=document.querySelector('input[aria-label='+${JSON.stringify(JSON.stringify(label))}+']'); element?.focus(); return !!element; })()`);
  await pressKey(cdp, 'ArrowUp', 'ArrowUp', 38);
  await pressKey(cdp, 'Enter', 'Enter', 13);
  await waitFor(cdp, `document.querySelector('input[aria-label='+${JSON.stringify(JSON.stringify(label))}+']')?.value===${JSON.stringify(expectedValue)}`, `${label} set to ${expectedValue}`);
  if (label === 'Page') {
    const pageNumber = Number(expectedValue.split(' ')[0]);
    await waitFor(cdp, `!!document.querySelector('img[alt="Recipe source page ${pageNumber}"]')`, `${label} preview rendered on page ${pageNumber}`);
  }
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
  if (value === '') {
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Backspace', code: 'Backspace', windowsVirtualKeyCode: 8 });
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Backspace', code: 'Backspace', windowsVirtualKeyCode: 8 });
  } else {
    await cdp.send('Input.insertText', { text: value });
  }
}
async function screenshot(cdp, name) {
  await mkdir(evidenceDir, { recursive: true });
  const image = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  const path = join(evidenceDir, name);
  await writeFile(path, Buffer.from(image.data, 'base64'));
  return path;
}

async function verifyResponsiveLayout(cdp, evidence) {
  const layouts = [];
  for (const { width, height } of [
    { width: 1920, height: 1080 }, { width: 1440, height: 900 },
    { width: 768, height: 900 }, { width: 320, height: 900 },
  ]) {
    await cdp.send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
    await evaluate(cdp, 'window.scrollTo(0,0)');
    await waitFor(cdp, `innerWidth===${width}&&document.documentElement.scrollWidth<=innerWidth`, `${width}px layout without horizontal overflow`);
    const layout = await evaluate(cdp, `JSON.stringify((() => {
      const pane=(name)=>{const e=document.querySelector('.st-key-review_'+name+'_pane'),r=e?.getBoundingClientRect();return {visible:!!e?.getClientRects().length,width:r?.width||0,left:r?.left||0,right:r?.right||0}};
      const action=(label)=>{const e=[...document.querySelectorAll('button')].find(x=>x.innerText.trim()===label);const r=e?.getBoundingClientRect();return {visible:!!e?.getClientRects().length,top:r?.top??null,bottom:r?.bottom??null,disabled:e?.disabled??null}};
      const switcher=document.querySelector('.st-key-review-pane-switcher');
      return {width:innerWidth,scrollWidth:document.documentElement.scrollWidth,scrollY,source:pane('source'),recipe:pane('recipe'),switcherVisible:!!switcher?.getClientRects().length,save:action('Save draft'),approve:action('Approve & next')};
    })())`);
    const state = JSON.parse(layout);
    if (width > 900) {
      if (!state.source.visible || !state.recipe.visible || state.switcherVisible) throw new Error(`Wide comparison layout failed at ${width}px: ${layout}`);
      if (state.source.width < 480 || state.recipe.width < 600) throw new Error(`Wide pane widths are too small at ${width}px: ${layout}`);
    } else if (!state.source.visible || state.recipe.visible || !state.switcherVisible) {
      throw new Error(`Narrow Source layout failed at ${width}px: ${layout}`);
    }
    if (!state.save.visible || !state.approve.visible || state.save.bottom > height || state.approve.bottom > height) {
      throw new Error(`Save/Approve are outside the initial ${width}x${height} viewport: ${layout}`);
    }
    layouts.push(state);
    evidence.screenshots.push(await screenshot(cdp, `d1-review-source-${width}x${height}.png`));
    if (width <= 900) {
      if (width === 768) {
        await selectNextComboOption(cdp, 'Page', '2 of 2');
        await selectNextComboOption(cdp, 'Source view', 'Inspect');
      }
      await clickPane(cdp, 'Recipe');
      await waitFor(cdp, `!![...document.querySelectorAll('input[aria-label="Recipe title"]')].find(e=>e.getClientRects().length)`, `${width}px Recipe pane`);
      await clickVisibleText(cdp, 'Ingredients', '[role="tab"]');
      await waitFor(cdp, `!![...document.querySelectorAll('input[aria-label="Ingredient 1"]')].find(e=>e.getClientRects().length)`, `${width}px ingredient editor`);
      await fillLabel(cdp, 'Ingredient 1', `responsive draft ${width}`);
      await waitFor(cdp, `([...document.querySelectorAll('input[aria-label="Ingredient 1"]')].find(e=>e.getClientRects().length))?.value===${JSON.stringify(`responsive draft ${width}`)}`, `${width}px ingredient text entered`);
      await pressKey(cdp, 'Tab', 'Tab', 9);
      await waitFor(cdp, `document.body.innerText.includes('Unsaved changes')`, `${width}px editor draft state`);
      await clickPane(cdp, 'Source');
      await waitFor(cdp, `!document.querySelector('.st-key-review_recipe_pane')?.getClientRects().length`, `${width}px Source pane restored`);
      await clickPane(cdp, 'Recipe');
      await waitFor(cdp, `(() => {const input=[...document.querySelectorAll('input[aria-label="Ingredient 1"]')].find(e=>e.getClientRects().length);return input?.value===${JSON.stringify(`responsive draft ${width}`)}})()`, `${width}px unsaved edit restored`);
      const retainedEdit = await evaluate(cdp, `document.querySelector('input[aria-label="Ingredient 1"]')?.value`);
      const instructionsSelected = await evaluate(cdp, `[...document.querySelectorAll('[role="tab"][aria-selected="true"]')].some(e=>e.innerText.trim()==='Instructions')`);
      if (!instructionsSelected) {
        await clickVisibleText(cdp, 'Instructions', '[role="tab"]');
        await waitFor(cdp, `[...document.querySelectorAll('[role="tab"][aria-selected="true"]')].some(e=>e.innerText.trim()==='Instructions')`, `${width}px Instructions tab selection before pane switch`);
      }
      await clickPane(cdp, 'Source');
      await waitFor(cdp, `!![...document.querySelectorAll('input[aria-label="Page"]')].find(e=>e.getClientRects().length)`, `${width}px Source pane after tab round trip`);
      const retainedSource = await evaluate(cdp, `JSON.stringify({page:[...document.querySelectorAll('input[aria-label="Page"]')].find(e=>e.getClientRects().length)?.value,zoom:[...document.querySelectorAll('input[aria-label="Source view"]')].find(e=>e.getClientRects().length)?.value})`);
      await clickPane(cdp, 'Recipe');
      await waitFor(cdp, `!![...document.querySelectorAll('input[aria-label="Recipe title"]')].find(e=>e.getClientRects().length)`, `${width}px Recipe pane after tab round trip`);
      await waitFor(cdp, `!![...document.querySelectorAll('[role="tab"][aria-selected="true"]')].find(e=>e.innerText.trim()==='Instructions')`, `${width}px recipe section restored`);
      const retainedState = { ...JSON.parse(retainedSource), tab: await evaluate(cdp, `[...document.querySelectorAll('[role="tab"][aria-selected="true"]')].map(e=>e.innerText.trim())`) };
      retainedState.edit = retainedEdit;
      if (retainedState.page !== '2 of 2' || retainedState.zoom !== 'Inspect' || retainedState.edit !== `responsive draft ${width}` || !retainedState.tab.includes('Instructions')) {
        throw new Error(`Narrow pane switch lost state at ${width}px: ${JSON.stringify(retainedState)}`);
      }
      evidence.screenshots.push(await screenshot(cdp, `d1-review-recipe-${width}x${height}.png`));
      await clickPane(cdp, 'Source');
      await waitFor(cdp, `!![...document.querySelectorAll('input[aria-label="Page"]')].find(e=>e.getClientRects().length)`, `${width}px Source pane before next viewport`);
      layouts.at(-1).retainedPaneState = retainedState;
    }
  }
  evidence.responsiveLayouts = layouts;
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await selectPreviousComboOption(cdp, 'Page', '1 of 2');
  await selectPreviousComboOption(cdp, 'Source view', 'Fit');
  await evaluate(cdp, 'window.scrollTo(0,0)');
  await evaluate(cdp, `document.documentElement.style.fontSize='200%'`);
  await waitFor(cdp, `document.documentElement.scrollWidth<=innerWidth`, '200 percent root text resize without horizontal overflow');
  const resized = await evaluate(cdp, `JSON.stringify({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,rootFont:getComputedStyle(document.documentElement).fontSize,save:[...document.querySelectorAll('button')].find(e=>e.innerText.trim()==='Save draft')?.getBoundingClientRect().toJSON(),approve:[...document.querySelectorAll('button')].find(e=>e.innerText.trim()==='Approve & next')?.getBoundingClientRect().toJSON()})`);
  evidence.textResize200Percent = JSON.parse(resized);
  evidence.screenshots.push(await screenshot(cdp, 'd1-review-text-200-percent.png'));
  await evaluate(cdp, `document.documentElement.style.fontSize=''`);
}

const profile = await mkdtemp(join(fixture.root, 'browser-profile-'));
let browser;
async function stopBrowser() {
  if (browser && browser.exitCode === null) {
    browser.kill();
    await Promise.race([new Promise((resolveClose) => browser.once('close', resolveClose)), sleep(5000)]);
  }
  await rm(profile, { recursive: true, force: true }).catch(() => {});
}
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
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: viewportWidth, height: 900, deviceScaleFactor: 1, mobile: false });
  const navigationStarted = Date.now();
  await cdp.send('Page.navigate', { url });
  const initialPaneReady = viewportWidth <= 900
    ? `!!document.querySelector('.st-key-review_source_pane')?.getClientRects().length`
    : `[...document.querySelectorAll('input[aria-label="Recipe title"]')].some(e=>e.getClientRects().length)`;
  await waitFor(cdp, `document.readyState==='complete'&&!!document.querySelector('[data-testid="stApp"]')&&(${initialPaneReady})`, 'initial Review pane', 45000);
  await waitFor(cdp, `(()=>{const i=[...document.images].find(x=>x.alt.startsWith('Recipe source page'));return !!i&&i.complete&&i.naturalWidth>100&&![...document.querySelectorAll('[data-testid*="skeleton" i],[class*="skeleton" i')].length})()`, 'settled source scan and editor', 60000);
  await assertBrowserFixtureAttestation(cdp, fixture);
  await evaluate(cdp, `(() => { const caption=[...document.querySelectorAll('[data-testid="stCaptionContainer"]')].find(e=>e.innerText.startsWith('Isolated browser fixture:')); if(caption)caption.style.display='none'; })()`);
  if (viewportWidth <= 900) {
    const initialPanes = await evaluate(cdp, `JSON.stringify(['source','recipe'].map(name=>{const e=document.querySelector('.st-key-review_'+name+'_pane');return {name,visible:!!e?.getClientRects().length,width:e?.getBoundingClientRect().width||0}}))`);
    if (arg('inspect') === 'true') console.log(`Initial narrow panes: ${initialPanes}`);
    await clickPane(cdp, 'Recipe');
    await waitFor(cdp, `!![...document.querySelectorAll('input[aria-label="Recipe title"]')].find(e=>e.getClientRects().length)`, 'narrow Recipe pane');
  }
  if (d2Only) {
    const d2Evidence = { screenshots: [], timings: { firstReviewSettledMs: Date.now() - navigationStarted } };
    await clickVisibleText(cdp, 'Queue');
    await waitFor(cdp, `!!document.querySelector('input[aria-label="Jump to recipe"]')`, 'recipe queue controls');
    await clickComboOption(cdp, 'Jump to recipe', candidate);
    const candidateId = await waitForCandidateSelected(cdp, fixture, candidate);
    await ensureExpanderOpen(cdp, 'Issues and source notes');
    const contextualIssues = [];
    for (const width of [1440, 768, 320]) {
      await cdp.send('Emulation.setDeviceMetricsOverride', { width, height: 1000, deviceScaleFactor: 1, mobile: false });
      if (width <= 900) await clickPane(cdp, 'Recipe');
      await waitFor(cdp, `document.documentElement.scrollWidth<=innerWidth`, `${width}px issue layout without horizontal overflow`);
      await ensureExpanderOpen(cdp, 'Optional details');
      await clickVisibleText(cdp, 'Ingredients', '[role="tab"]');
      await waitFor(cdp, `!![...document.querySelectorAll('input[aria-label="Ingredient 1"]')].find(e=>e.getClientRects().length)`, `${width}px ingredient editor`);
      const serialized = await evaluate(cdp, `JSON.stringify({
        missing:document.body.innerText.includes('Missing from source'),
        ambiguous:document.body.innerText.includes('Ambiguous in source'),
        unreadable:document.body.innerText.includes('Unreadable in source'),
        optional:document.body.innerText.includes('Optional note · Missing from source'),
        proposal:document.body.innerText.includes('Proposal · 1 cup dates · AI-inferred · Unverified'),
        sourceSupported:document.body.innerText.includes('Proposal · 1 cup cranberries · Source-supported · Unverified'),
        overflow:document.documentElement.scrollWidth>innerWidth,
        width:innerWidth
      })`);
      const state = JSON.parse(serialized);
      if (!state.missing || !state.ambiguous || !state.unreadable || !state.optional
          || !state.proposal || !state.sourceSupported || state.overflow) {
        const body = await evaluate(cdp, `document.body.innerText.slice(-2200)`);
        throw new Error(`Contextual ingredient issues/proposal failed at ${width}px: ${serialized}; body=${JSON.stringify(body)}`);
      }
      contextualIssues.push(state);
      d2Evidence.screenshots.push(await screenshot(cdp, `d2-contextual-issues-${width}px.png`));
      if (width <= 900) await clickPane(cdp, 'Source');
    }
    d2Evidence.contextualIssues = contextualIssues;
    await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
    await clickVisibleText(cdp, 'Ingredients', '[role="tab"]');
    await waitFor(cdp, `!![...document.querySelectorAll('input[aria-label="Ingredient 1"]')].find(e=>e.getClientRects().length)`, '1440px ingredient editor after viewport return');
    await waitFor(cdp, `document.body.innerText.includes('Proposal · 1 cup dates · AI-inferred · Unverified')`, 'pending proposal summary after viewport return');
    await ensureExpanderOpen(cdp, 'Proposal · 1 cup dates');
    await waitFor(cdp, `document.body.innerText.includes('AI-inferred completion')&&document.body.innerText.includes('Uncertainty: The quantity is cropped.')`, 'expandable unverified proposal details');
    await sleep(400);
    d2Evidence.screenshots.push(await screenshot(cdp, 'd2-proposal-details-1440x900.png'));
    const reviewEvents = (await readFile(join(fixture.output, 'review_events.jsonl'), 'utf8'))
      .split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
    if (reviewEvents.some((event) => ['review_revision', 'review_approval', 'review_issue_resolution'].includes(event.event))) {
      throw new Error('D2 presentation inspection wrote a recipe revision, approval, or issue decision.');
    }
    d2Evidence.noReviewDecisionsWritten = true;
    d2Evidence.candidateId = candidateId;
    console.log(JSON.stringify(d2Evidence, null, 2));
    ws.close(); await stopBrowser(); await fixture.close();
    process.exit(0);
  }
  if (d2Corrections) {
    await clickVisibleText(cdp, 'Queue');
    await waitFor(cdp, `!!document.querySelector('input[aria-label="Jump to recipe"]')`, 'recipe queue controls');
    await clickComboOption(cdp, 'Jump to recipe', candidate);
    const candidateId = await waitForCandidateSelected(cdp, fixture, candidate);
    await clickVisibleText(cdp, 'Ingredients', '[role="tab"]');
    await waitFor(cdp, `!![...document.querySelectorAll('input[aria-label="Ingredient 1"]')].find(e=>e.getClientRects().length)`, 'ingredient rows');
    await ensureExpanderOpen(cdp, 'Issue details · Ambiguous in source');
    await ensureExpanderOpen(cdp, 'Issue details · Unreadable in source');
    const rowForIssue = async (detail) => JSON.parse(await evaluate(cdp, `JSON.stringify((() => {
      const e=[...document.querySelectorAll('[data-testid="stExpander"]')].find(x=>x.innerText.includes(${JSON.stringify(detail)}));
      const inputs=[...document.querySelectorAll('input[aria-label^="Ingredient "]')];
      const previous=inputs.filter(input=>input.compareDocumentPosition(e)&Node.DOCUMENT_POSITION_FOLLOWING).at(-1);
      return previous?{label:previous.getAttribute('aria-label'),value:previous.value}:null;
    })())`));
    const initialRows = {
      first: await rowForIssue('First row issue.'),
      second: await rowForIssue('Second row issue.'),
    };
    if (initialRows.first?.label !== 'Ingredient 1' || initialRows.first.value !== 'dates'
        || initialRows.second?.label !== 'Ingredient 2' || initialRows.second.value !== 'cranberries') {
      throw new Error(`Initial row finding bindings are misplaced: ${JSON.stringify(initialRows)}.`);
    }
    const screenshots = [await screenshot(cdp, 'd2-f01-before-resolution.png')];
    await clickVisibleText(cdp, 'I checked the source and corrected this field', '[data-testid="stCheckbox"]');
    await waitFor(cdp, `[...document.querySelectorAll('button')].some(e=>e.innerText.includes('Record source correction')&&!e.disabled)`, 'source correction confirmation');
    await clickVisibleText(cdp, 'Record source correction', 'button');
    await waitForReviewResolution(fixture, {
      candidateId, issueId: 'finding:0:ingredients[0]', disposition: 'source_corrected',
    });
    await waitFor(cdp, `!document.body.innerText.includes('First row issue.')&&document.body.innerText.includes('Second row issue.')`, 'second finding remains after first resolution');
    const remainingBeforeReorder = await rowForIssue('Second row issue.');
    if (remainingBeforeReorder?.label !== 'Ingredient 2' || remainingBeforeReorder.value !== 'cranberries') {
      throw new Error(`Remaining finding moved after resolving the first issue: ${JSON.stringify(remainingBeforeReorder)}.`);
    }
    screenshots.push(await screenshot(cdp, 'd2-f01-after-first-resolution.png'));
    await clickVisibleText(cdp, 'Actions', 'button');
    await clickVisibleText(cdp, 'Move down', 'button,[role="menuitem"]');
    await waitFor(cdp, `document.querySelector('input[aria-label="Ingredient 1"]')?.value==='cranberries'&&document.querySelector('input[aria-label="Ingredient 2"]')?.value==='dates'`, 'ingredient row reorder');
    const remainingAfterReorder = await rowForIssue('Second row issue.');
    if (remainingAfterReorder?.label !== 'Ingredient 1' || remainingAfterReorder.value !== 'cranberries') {
      throw new Error(`Finding did not follow its registered row after reordering: ${JSON.stringify(remainingAfterReorder)}.`);
    }
    screenshots.push(await screenshot(cdp, 'd2-f01-after-row-reorder.png'));
    await clickVisibleText(cdp, 'Defer review', 'button');
    await waitFor(cdp, `document.body.innerText.includes('Second row issue.')`, 'deferred finding remains open');
    const resolutionEvents = (await readFile(join(fixture.output, 'review_events.jsonl'), 'utf8'))
      .split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line))
      .filter((event) => event.event === 'review_issue_resolution' && event.candidate_id === candidateId);
    if (resolutionEvents.length !== 1 || resolutionEvents[0].issue_id !== 'finding:0:ingredients[0]') {
      throw new Error(`Resolve/defer actions wrote unexpected issue history: ${JSON.stringify(resolutionEvents)}.`);
    }
    console.log(JSON.stringify({
      candidateId, firstFindingResolved: true, remainingFindingBeforeReorder: remainingBeforeReorder,
      remainingFindingAfterReorder: remainingAfterReorder, deferKeptIssueOpen: true,
      immutableResolutionEvents: resolutionEvents, screenshots,
    }, null, 2));
    ws.close(); await stopBrowser(); await fixture.close();
    process.exit(0);
  }
  const friedRiceId = stateRegression ? null
    : await assertActiveCandidate(cdp, fixture, 'Fried Rice', { revision: 0 });
  const timings = { firstReviewSettledMs: Date.now() - navigationStarted };
  const evidence = { screenshots: [await screenshot(cdp, 'browser-review-after-precise-1440x900.png')], timings };
  if (stateRegression) {
    const counters = async () => JSON.parse(await evaluate(cdp, `(() => {
      const e=[...document.querySelectorAll('[data-testid="stJson"]')].find(x=>x.innerText.includes('full_page_executions'));
      const values={};for(const line of (e?.innerText||'').split('\\n')){const m=line.match(/^\\s*"([^"]+)":\\s*(\\d+)/);if(m)values[m[1]]=Number(m[2]);}
      return JSON.stringify(values);
    })()`));
    const state = async () => JSON.parse(await evaluate(cdp, `JSON.stringify((() => {
      const header=[...document.querySelectorAll('[data-testid="stCaptionContainer"]')]
        .map(e=>e.innerText.trim()).find(t=>/^(Unsaved changes · saved revision|Saved revision) \\d+/.test(t))||'';
      const button=[...document.querySelectorAll('button')].find(e=>e.innerText.trim()==='Approve & next'&&e.getClientRects().length);
      return {header,approveDisabled:button?.disabled??null,
        editorDirty:[...document.querySelectorAll('[data-testid="stCaptionContainer"]')].some(e=>e.innerText.includes('Unsaved changes · use Save draft')),
        title:document.querySelector('input[aria-label="Recipe title"]')?.value||''};
    })())`));
    const states = {};
    await clickVisibleText(cdp, 'Performance counters', '[data-testid="stExpander"]');
    await waitFor(cdp, `!![...document.querySelectorAll('[data-testid="stJson"]')].find(e=>e.innerText.includes('full_page_executions'))`, 'Review execution counters');
    const baseline = await counters();
    const initialState = await state();
    states.initialCompleteRecipeWasSavedAndEligible = initialState.header.startsWith('Saved revision')
      && initialState.approveDisabled === false;
    const waitForFragmentRerun = async (prior, label) => waitFor(cdp, `(()=>{const e=[...document.querySelectorAll('[data-testid="stJson"]')].find(x=>x.innerText.includes('fragment_executions'));return Number((e?.innerText.match(/"fragment_executions":\\s*(\\d+)/)||[])[1]||0)>${prior}})()`, label);

    const initialTitle = await evaluate(cdp, `document.querySelector('input[aria-label="Recipe title"]')?.value||''`);
    if (!initialTitle) throw new Error('The state regression needs an isolated synthetic recipe.');
    const editedTitle = `${initialTitle} draft`;
    await fillLabel(cdp, 'Recipe title', editedTitle);
    await pressKey(cdp, 'Tab', 'Tab', 9);
    await waitFor(cdp, `document.querySelector('input[aria-label="Recipe title"]')?.value===${JSON.stringify(editedTitle)}`, 'committed title edit');
    await waitFor(cdp, `(()=>{const e=[...document.querySelectorAll('[data-testid="stJson"]')].find(x=>x.innerText.includes('fragment_executions'));return Number((e?.innerText.match(/"fragment_executions":\\s*(\\d+)/)||[])[1]||0)>${baseline.fragment_executions||0}})()`, 'fragment-only title rerun');
    states.recipeAfterEdit = { ...await state(), counters: await counters() };
    states.headerRefreshedAfterEdit = states.recipeAfterEdit.header.startsWith('Unsaved changes');
    states.titleEditWasFragmentOnly = states.recipeAfterEdit.counters.full_page_executions === baseline.full_page_executions;
    states.screenshots = [await screenshot(cdp, 'd1-correction-stale-header-reproduction.png')];
    await clickVisibleText(cdp, 'Instructions', '[role="tab"]');
    await waitFor(cdp, `!!document.querySelector('textarea[aria-label="Step 1"]')&&!!document.querySelector('textarea[aria-label="Step 2"]')`, 'complete recipe instructions');
    let afterEdit = await counters();
    await fillLabel(cdp, 'Step 1', '');
    await pressKey(cdp, 'Tab', 'Tab', 9);
    await waitForFragmentRerun(afterEdit.fragment_executions, 'first instruction fragment rerun');
    afterEdit = await counters();
    await fillLabel(cdp, 'Step 2', '');
    await pressKey(cdp, 'Tab', 'Tab', 9);
    await waitForFragmentRerun(afterEdit.fragment_executions, 'second instruction fragment rerun');
    await waitFor(cdp, `document.querySelector('textarea[aria-label="Step 1"]')?.value===''&&document.querySelector('textarea[aria-label="Step 2"]')?.value===''`, 'complete recipe made incomplete');
    states.missingInstructionsCompleted = { ...await state(), counters: await counters() };
    states.approvalDisabledAfterMakingIncomplete = states.missingInstructionsCompleted.approveDisabled === true;
    states.headerUpdatedAfterMakingIncomplete = states.missingInstructionsCompleted.header.startsWith('Unsaved changes');
    states.incompleteEditStayedFragmentOnly = states.missingInstructionsCompleted.counters.full_page_executions === baseline.full_page_executions;
    states.noDraftWriteDuringEditorInteraction = states.missingInstructionsCompleted.counters.durable_writes === baseline.durable_writes;
    states.screenshots = [await screenshot(cdp, 'd1-correction-approval-disabled.png')];
    afterEdit = await counters();
    await fillLabel(cdp, 'Step 1', 'Warm synthetic rice.');
    await pressKey(cdp, 'Tab', 'Tab', 9);
    await waitForFragmentRerun(afterEdit.fragment_executions, 'completed instruction fragment rerun');
    await waitFor(cdp, `document.querySelector('textarea[aria-label="Step 1"]')?.value==='Warm synthetic rice.'`, 'required instruction completed');
    states.missingInstructionsCompleted = { ...await state(), counters: await counters() };
    states.approvalEnabledAfterCompletingMissingInstructions = states.missingInstructionsCompleted.approveDisabled === false;
    states.instructionEditStayedFragmentOnly = states.missingInstructionsCompleted.counters.full_page_executions === baseline.full_page_executions;
    states.noDraftWriteDuringCompletion = states.missingInstructionsCompleted.counters.durable_writes === baseline.durable_writes;
    states.screenshots.push(await screenshot(cdp, 'd1-correction-approval-enabled.png'));
    evidence.headerStateRegression = states;
    console.log(JSON.stringify(evidence, null, 2));
    ws.close(); await stopBrowser(); await fixture.close();
    if (!states.initialCompleteRecipeWasSavedAndEligible || !states.headerRefreshedAfterEdit || !states.headerUpdatedAfterMakingIncomplete
        || !states.approvalDisabledAfterMakingIncomplete || !states.approvalEnabledAfterCompletingMissingInstructions
        || !states.titleEditWasFragmentOnly || !states.incompleteEditStayedFragmentOnly
        || !states.instructionEditStayedFragmentOnly || !states.noDraftWriteDuringEditorInteraction
        || !states.noDraftWriteDuringCompletion) process.exit(1);
    process.exit(0);
  }
  if (arg('inspect') === 'true') {
    if (viewportWidth <= 900) {
      await fillLabel(cdp, 'Ingredient 1', 'probe edit');
      await pressKey(cdp, 'Tab', 'Tab', 9);
      await waitFor(cdp, `document.body.innerText.includes('Unsaved changes')`, 'fragment edit buffer refresh');
      await sleep(500);
      await clickPane(cdp, 'Source');
      await sleep(1000);
    }
    console.log(JSON.stringify(await evaluate(cdp, `JSON.stringify({scrollY,body:document.body.innerText.slice(0,2400),paneChoices:[...document.querySelectorAll('[role="radio"]')].map(e=>({text:e.innerText.trim(),checked:e.getAttribute('aria-checked'),pressed:e.getAttribute('aria-pressed'),tabIndex:e.tabIndex,className:e.className})),wrapperClasses:[...document.querySelectorAll('[class*="review_workspace"]')].map(e=>e.className),overflow:{width:innerWidth,scrollWidth:document.documentElement.scrollWidth},panes:['source','recipe'].map(name=>{const e=document.querySelector('.st-key-review_'+name+'_pane');const r=e?.getBoundingClientRect();return {name,visible:!!e?.getClientRects().length,width:r?.width,left:r?.left,right:r?.right,display:e?getComputedStyle(e).display:null,parentDisplay:e?getComputedStyle(e.parentElement).display:null}}),actions:[...document.querySelectorAll('button')].filter(e=>['Save draft','Approve & next'].includes(e.innerText.trim())).map(e=>({label:e.innerText.trim(),visible:!!e.getClientRects().length,top:e.getBoundingClientRect().top,bottom:e.getBoundingClientRect().bottom})),controls:[...document.querySelectorAll('input,textarea,button,[role="tab"]')].map(e=>({tag:e.tagName,role:e.getAttribute('role'),aria:e.getAttribute('aria-label'),text:e.innerText?.trim().slice(0,80),value:e.value?.slice(0,100),disabled:e.disabled})).filter(x=>x.aria||x.text)})`), null, 2));
    await screenshot(cdp, `browser-review-inspect-${viewportWidth}.png`);
    ws.close(); await stopBrowser();
    await fixture.close();
    process.exit(0);
  }
  if (arg('skip-responsive') !== 'true') {
    await verifyResponsiveLayout(cdp, evidence);
    if (arg('responsive-only') === 'true') {
      console.log(JSON.stringify(evidence, null, 2));
      ws.close(); await stopBrowser();
      await fixture.close();
      process.exit(0);
    }
  }
  const firstSourceText = await evaluate(cdp, `(() => {
    const sections=[...document.querySelectorAll('[data-testid="stText"]')]
      .filter(element=>element.getClientRects().length).map(element=>element.innerText.trim());
    return JSON.stringify({sections, body:document.body.innerText.includes('Source text')});
  })()`);
  const firstSource = JSON.parse(firstSourceText || '{}');
  if (!firstSource.body || !firstSource.sections.some(text=>text.includes('Fried Rice')&&text.includes('Cook rice.'))
      || firstSource.sections.some(text=>text.includes('The Best Caramel Apples'))) {
    throw new Error(`Source text did not show only the selected source page: ${JSON.stringify({firstSource,
      browser:await browserDiagnostics(cdp),visibleSourceControls:await evaluate(cdp, `JSON.stringify([...document.querySelectorAll('input[aria-label="Page"],input[aria-label="Source view"]')].map(e=>({label:e.getAttribute('aria-label'),value:e.value,visible:!!e.getClientRects().length})))`),
      selectedPane:await evaluate(cdp, `JSON.stringify([...document.querySelectorAll('.st-key-review-pane-switcher [role="radio"]')].map(e=>({text:e.innerText.trim(),checked:e.getAttribute('aria-checked')})))`),
      body:await evaluate(cdp, `document.body.innerText.slice(-1200)`)})}.`);
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
  await waitFor(cdp, `document.body.innerText.includes('Proposal · 1 cup dates · AI-inferred · Rejected')&&document.body.innerText.includes('Rejecting this proposal leaves the source issue unresolved')`, 'rejected critical proposal and unresolved source issue');
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
  await waitFor(cdp, `document.body.innerText.includes('Proposal · 1 cup cranberries · AI-inferred · Applied with edits')`, 'edited proposal summary status');
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
  await waitFor(cdp, `document.querySelector('textarea[aria-label="Step 1"]')?.value===${JSON.stringify(approvedInstructions[0])}&&document.querySelector('textarea[aria-label="Step 2"]')?.value===${JSON.stringify(approvedInstructions[1])}`, 'latest instruction edits visible before Save');
  await pressKey(cdp, 'Tab', 'Tab', 9);
  await waitFor(cdp, `document.body.innerText.includes('Unsaved changes')`, 'instruction edits synchronized before Save');
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
  await stopBrowser();
  await fixture.close();
}

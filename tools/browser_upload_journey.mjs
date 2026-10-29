#!/usr/bin/env node
// Browser-only UI journey using cached Chromium, Node's built-in WebSocket,
// CDP mouse/keyboard/file APIs, and the same loopback-only request policy.
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm, mkdir, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { assertBrowserFixtureAttestation, rejectArbitraryUrl,
  prepareEvidenceDirectory, startIsolatedBrowserFixture } from './browser_fixture_runner.mjs';
import { rectIntersectsViewport } from './browser_dom_helpers.mjs';

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
      const value=(e.getAttribute('aria-label')||e.innerText||'').replace(/\\s+/g,' ').trim();
      if (!value.includes(${JSON.stringify(text)}) || !e.getClientRects().length) return false;
      e.scrollIntoView({block:'center',inline:'nearest'});
      const r=e.getBoundingClientRect();
      return r.width>0 && r.height>0 && (${rectIntersectsViewport.toString()})(r,innerWidth,innerHeight);
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
async function tabToControl(cdp, accessibleName, limit = 60) {
  const trail = [];
  for (let index = 0; index < limit; index += 1) {
    await key(cdp, 'Tab', 'Tab', 9);
    const focus = await evaluate(cdp, `(() => {
      const e=document.activeElement;
      return {tag:e?.tagName||'',role:e?.getAttribute('role')||'',label:e?.getAttribute('aria-label')||'',
        text:(e?.innerText||'').replace(/\\s+/g,' ').trim(),visible:!!e?.getClientRects().length};
    })()`);
    trail.push(focus);
    if (focus.visible && (focus.label === accessibleName || focus.text === accessibleName
        || (focus.tag === 'A' && focus.text.includes(accessibleName)))) return trail;
  }
  throw new Error(`Keyboard Tab did not reach ${JSON.stringify(accessibleName)}; focus trail=${JSON.stringify(trail)}.`);
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
async function selectReviewPane(cdp, name) {
  const selected = await evaluate(cdp, `(() => {
    const option=[...document.querySelectorAll('.st-key-review-pane-switcher [role="radio"]')]
      .find(item=>item.innerText.trim()===${JSON.stringify(name)});
    if(!option)return false; option.scrollIntoView({block:'center',inline:'nearest'}); option.focus(); option.click(); return true;
  })()`);
  if (!selected) throw new Error(`Could not find the ${name} Review pane.`);
  await waitFor(cdp, `!![...document.querySelectorAll('.st-key-review-pane-switcher [role="radio"]')]
    .find(item=>item.innerText.trim()===${JSON.stringify(name)}&&item.getAttribute('aria-checked')==='true')`,
    `${name} Review pane`);
}
async function screenshot(cdp, name) {
  const replacedName = name.replace(/\d+x\d+/, `${viewport[0]}x${viewport[1]}`);
  await evaluate(cdp, `(() => {
    window.__ux0SanitizedCaptions=[];
    for(const e of document.querySelectorAll('[data-testid="stCaptionContainer"]')){
      if(/^(Isolated browser fixture:|Local application library:)/.test(e.innerText.trim())){
        window.__ux0SanitizedCaptions.push([e,e.style.visibility]);e.style.visibility='hidden';
      }
    }
    return window.__ux0SanitizedCaptions.length;
  })()`);
  const image = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  await evaluate(cdp, `(() => {for(const [e,value] of window.__ux0SanitizedCaptions)e.style.visibility=value;delete window.__ux0SanitizedCaptions})()`);
  const path = join(evidence, replacedName);
  await mkdir(evidence, { recursive: true });
  await writeFile(path, Buffer.from(image.data, 'base64'));
  return path;
}
async function key(cdp, value, code, vk, modifiers = 0) {
  const event = { key: value, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers };
  if (value === 'Enter') event.text = '\r';
  if (value === ' ') event.text = ' ';
  await cdp.send('Input.dispatchKeyEvent', { ...event, type: 'keyDown' });
  await cdp.send('Input.dispatchKeyEvent', { ...event, type: 'keyUp' });
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
  const uploadHierarchy = await evaluate(cdp, `(() => {
    const text=document.body.innerText;
    const labels=['Synthetic browser batch','Select an existing batch','Current extraction job',
      'Files in this batch','Add PDF, JPG, PNG, or WEBP scans','Extraction','Create a batch','Cookbook PDFs'];
    const positions=Object.fromEntries(labels.map(label=>[label,text.indexOf(label)]));
    return {positions,viewportWidth:innerWidth,
      documentWidth:document.documentElement.scrollWidth};
  })()`);
  if (uploadHierarchy.documentWidth > uploadHierarchy.viewportWidth) {
    throw new Error(`Upload overflows the ${viewport[0]}px viewport: ${JSON.stringify(uploadHierarchy)}.`);
  }
  const orderedSections=['Synthetic browser batch','Select an existing batch','Current extraction job',
    'Files in this batch','Add PDF, JPG, PNG, or WEBP scans','Extraction','Create a batch','Cookbook PDFs'];
  const positions=orderedSections.map(label=>uploadHierarchy.positions[label]);
  if (positions.some(position=>position < 0) || positions.some((position,index)=>index>0 && position < positions[index-1])) {
    throw new Error(`Upload sections do not follow the D3 order: ${JSON.stringify(uploadHierarchy)}.`);
  }
  artifacts.push(await screenshot(cdp, 'upload-partial-success-1440x900.png'));
  const errorVisible = await evaluate(cdp, `(() => { const e=[...document.querySelectorAll('div,p,span')].find(x=>x.children.length===0 && x.innerText.includes('Could not add invalid-scan.pdf')); e?.scrollIntoView({block:'center'}); return !!e; })()`);
  if (errorVisible) artifacts.push(await screenshot(cdp, 'upload-rejected-file-error-1440x900.png'));
  await evaluate(cdp, 'window.scrollTo(0,0)');

  await clickText(cdp, 'Start extraction');
  await waitFor(cdp, `/Extraction is complete/.test(document.body.innerText)`, 'synthetic background extraction');
  artifacts.push(await screenshot(cdp, 'upload-complete-1440x900.png'));

  if (viewport[0] <= 480) {
    await tabToControl(cdp, 'Expand sidebar');
    await key(cdp, ' ', 'Space', 32);
    await waitFor(cdp, `!![...document.querySelectorAll('a')].find(e=>e.innerText.includes('Review')&&e.getBoundingClientRect().right>0)`,
      'narrow navigation links become visible');
    await tabToControl(cdp, 'Review');
    await key(cdp, 'Enter', 'Enter', 13);
  } else {
    await clickText(cdp, 'Review', 'a,button,[role="tab"],[role="button"]');
  }
  await waitFor(cdp, `([...document.querySelectorAll('input')].some(e=>e.value.includes('Synthetic demo from synthetic-recipe')) || document.body.innerText.includes('Synthetic demo from synthetic-recipe')) && /Save draft/.test(document.body.innerText)`, 'review candidate');
  if (viewport[0] <= 900) {
    await selectReviewPane(cdp, 'Recipe');
    await waitFor(cdp, `!![...document.querySelectorAll('input[aria-label="Recipe title"]')].find(e=>e.getClientRects().length)`, 'narrow Recipe pane');
  } else {
    await waitFor(cdp, `!![...document.querySelectorAll('input[aria-label="Recipe title"]')].find(e=>e.getClientRects().length)`, 'desktop Recipe editor');
  }
  if (viewport[0] <= 480) {
    await tabToControl(cdp, 'Collapse sidebar');
    await key(cdp, ' ', 'Space', 32);
    await waitFor(cdp, `!document.querySelector('button[aria-label="Collapse sidebar"]')`, 'narrow navigation closed after selection');
  }
  const reviewAX = await cdp.send('Accessibility.getFullAXTree');
  const reviewNames = (reviewAX.nodes||[]).filter(node=>['textbox','button'].includes(node.role?.value))
    .map(node=>({role:node.role.value,name:node.name?.value||''}));
  for (const name of ['Recipe title','Save draft','Approve & next']) {
    if (!reviewNames.some(node=>node.name===name)) throw new Error(`Review accessibility tree is missing ${JSON.stringify(name)}.`);
  }
  console.error(`Review accessible control names verified at ${viewport.join('x')}: ${JSON.stringify(reviewNames.filter(node=>['Recipe title','Save draft','Approve & next'].includes(node.name)))}`);
  const reviewBaseline = await evaluate(cdp, `(() => {
    const box=e=>{if(!e)return null;const r=e.getBoundingClientRect();return {x:Math.round(r.x),y:Math.round(r.y),right:Math.round(r.right),bottom:Math.round(r.bottom),width:Math.round(r.width),height:Math.round(r.height)}};
    const inViewport=r=>!!r&&r.x>=0&&r.y>=0&&r.right<=innerWidth&&r.bottom<=innerHeight;
    const title=document.querySelector('input[aria-label="Recipe title"]');
    const save=[...document.querySelectorAll('button')].find(e=>e.innerText.trim()==='Save draft');
    const approve=[...document.querySelectorAll('button')].find(e=>e.innerText.trim()==='Approve & next');
    const sourceHeading=[...document.querySelectorAll('h1,h2,h3,h4,div')].find(e=>e.innerText.trim()==='Source scan · synthetic-recipe.png');
    const navButton=document.querySelector('button[aria-label="Expand sidebar"],button[aria-label="Collapse sidebar"]');
    return {viewport:{width:innerWidth,height:innerHeight,documentWidth:document.documentElement.scrollWidth,scrollX,scrollY},
      navigation:{buttonName:navButton?.getAttribute('aria-label')||'',button:box(navButton),
        reviewLink:[...document.querySelectorAll('a')].find(e=>e.innerText.includes('Review'))?box([...document.querySelectorAll('a')].find(e=>e.innerText.includes('Review'))):null},
      recipeTitle:box(title),saveDraft:{box:box(save),visibleWithoutScrolling:inViewport(box(save))},
      approve:{box:box(approve),visibleWithoutScrolling:inViewport(box(approve))},sourceHeading:box(sourceHeading)};
  })()`);
  artifacts.push(await screenshot(cdp, 'review-candidate-1440x900.png'));
  await fillByLabel(cdp, 'Recipe title', 'Synthetic demo from synthetic-recipe reviewed');
  const saveFocusTrail = await tabToControl(cdp, 'Save draft');
  const focusedSave = await evaluate(cdp, `(() => {const e=document.activeElement,s=getComputedStyle(e);return {
    tag:e?.tagName,text:(e?.innerText||'').trim(),visible:!!e?.getClientRects().length,
    outlineStyle:s.outlineStyle,outlineWidth:s.outlineWidth,outlineColor:s.outlineColor,boxShadow:s.boxShadow
  }})()`);
  await clickText(cdp, 'Save draft');
  await waitFor(cdp, `/Draft saved \\(revision 1\\)/.test(document.body.innerText)`, 'durable draft revision');
  await waitFor(cdp, `document.activeElement?.getAttribute('aria-label')==='Recipe title'`, 'focus restored to recipe title after Save draft');
  const focusAfterSave = await evaluate(cdp, `(() => {const e=document.activeElement;return {tag:e?.tagName||'',label:e?.getAttribute('aria-label')||'',text:(e?.innerText||'').trim(),visible:!!e?.getClientRects().length}})()`);
  if (focusAfterSave.label !== 'Recipe title') {
    throw new Error(`Save draft did not restore focus to Recipe title; focused element=${JSON.stringify(focusAfterSave)}.`);
  }
  await fillByLabel(cdp, 'Recipe title', 'Synthetic demo from synthetic-recipe final edit');
  await key(cdp, 'Enter', 'Enter', 13);
  await waitFor(cdp, `!/Draft saved \\(revision 1\\)/.test(document.body.innerText)`, 'unsaved edit clears saved confirmation');
  await clickText(cdp, 'Save draft');
  await waitFor(cdp, `/Draft saved \\(revision 2\\)/.test(document.body.innerText)`, 'second durable draft revision');
  await waitFor(cdp, `document.activeElement?.getAttribute('aria-label')==='Recipe title'`, 'focus restored after second Save draft');
  await clickText(cdp, 'Save draft');
  await waitFor(cdp, `/Draft already saved \\(revision 2\\)/.test(document.body.innerText)`, 'no-op save confirmation');
  await waitFor(cdp, `(() => {const e=document.activeElement;return e?.getAttribute('aria-label')==='Recipe title'||
    (e?.tagName==='BUTTON'&&e.innerText.trim()==='Save draft')})()`, 'focus remains on Save draft or returns to Recipe title after no-op save');
  const focusAfterNoOpSave = await evaluate(cdp, `(() => {const e=document.activeElement;return {tag:e?.tagName||'',label:e?.getAttribute('aria-label')||'',text:(e?.innerText||'').trim()}})()`);
  const priorDocumentStart = await evaluate(cdp, 'performance.timeOrigin');
  await cdp.send('Page.reload');
  await waitFor(cdp, `performance.timeOrigin>${priorDocumentStart}`, 'browser reload completion');
  await waitFor(cdp, `document.querySelector('input[aria-label="Recipe title"]')?.value==='Synthetic demo from synthetic-recipe final edit' && /synthetic-recipe.png/.test(document.body.innerText)`, 'restored saved draft after reload');
  artifacts.push(await screenshot(cdp, 'review-draft-restored-1440x900.png'));
  await clickText(cdp, 'Approve & next');
  await waitFor(cdp, `/You.ve reached the end|queue is clear|No recipes match these filters|Open Import & history/.test(document.body.innerText)`, 'explicit approval');

  if (viewport[0] <= 480) {
    await clickText(cdp, 'Expand sidebar', 'button');
    await waitFor(cdp, `!![...document.querySelectorAll('a')].find(e=>e.innerText.includes('Import & history')&&e.getBoundingClientRect().right>0)`,
      'narrow Import and history link becomes visible');
    await clickText(cdp, 'Import & history', 'a,button,[role="tab"],[role="button"]');
  } else {
    await clickText(cdp, 'Import & history', 'a,button,[role="tab"],[role="button"]');
  }
  if (viewport[0] <= 480) {
    await waitFor(cdp, `location.pathname.includes('import-history')`, 'navigation to Import and history');
    await clickText(cdp, 'Collapse sidebar', 'button');
  }
  await waitFor(cdp, `/Ready to import/.test(document.body.innerText) && /Select all eligible recipes/.test(document.body.innerText)`, 'approved recipe import controls');
  await clickText(cdp, 'Select all eligible recipes');
  await waitFor(cdp, `/Synthetic demo from synthetic-recipe/.test(document.body.innerText)`, 'approved recipe selected for import');
  await fillByLabel(cdp, 'Mealie tag (optional)', 'synthetic-demo-tag');
  await clickText(cdp, 'Preview import');
  await waitFor(cdp, `/1 recipes will be sent to Mealie/.test(document.body.innerText)`, 'import preview');
  artifacts.push(await screenshot(cdp, 'import-preview-1440x900.png'));
  await clickText(cdp, 'Import 1 recipe');
  await waitFor(cdp, `/Recipe created in Mealie/.test(document.body.innerText) && /Tag applied/.test(document.body.innerText) && /History and recovery/.test(document.body.innerText)`, 'separate fake Mealie create and tag outcomes');
  await clickText(cdp, 'History and recovery', 'summary,button,a,[role="button"]');
  await waitFor(cdp, `/Find in history/.test(document.body.innerText) && /Imported successfully/.test(document.body.innerText)`, 'searchable protected import history');
  await evaluate(cdp, `(() => { const text='Synthetic demo from synthetic-recipe'; const items=[...document.querySelectorAll('div,p,span')].filter(e=>e.children.length===0 && e.innerText?.includes(text)); items.sort((a,b)=>a.innerText.length-b.innerText.length)[0]?.scrollIntoView({block:'center'}); })()`);
  artifacts.push(await screenshot(cdp, 'import-history-1440x900.png'));

  const accessible = await evaluate(cdp, `(() => ({
        title:document.title, url:location.href, reviewBaseline:${JSON.stringify(reviewBaseline)},
        keyboardSaveFocus:{tabSteps:${saveFocusTrail.length},control:${JSON.stringify(focusedSave)}},
        focusAfterSave:${JSON.stringify(focusAfterSave)}, focusAfterNoOpSave:${JSON.stringify(focusAfterNoOpSave)},
        viewport:{width:innerWidth,height:innerHeight,documentWidth:document.documentElement.scrollWidth,
      horizontalOverflow:document.documentElement.scrollWidth>innerWidth},
    headings:[...document.querySelectorAll('h1,h2,h3,h4')].map(e=>({level:e.tagName,text:e.innerText.trim()})),
    controls:[...document.querySelectorAll('button,input,textarea,[role="combobox"],a')].map(e=>{
      const r=e.getBoundingClientRect(),s=getComputedStyle(e);return{tag:e.tagName,role:e.getAttribute('role'),name:e.getAttribute('aria-label')||e.labels?.[0]?.innerText?.trim()||e.innerText?.trim()||'',
        disabled:e.disabled||e.getAttribute('aria-disabled')==='true',x:Math.round(r.x),y:Math.round(r.y),width:Math.round(r.width),height:Math.round(r.height),
        focusOutline:s.outlineStyle+' '+s.outlineWidth,focusShadow:s.boxShadow};}),
    imagesWithoutAlt:[...document.querySelectorAll('img:not([alt])')].length,text:document.body.innerText.slice(0,3000)
  }))()`);
  if (accessible.viewport.horizontalOverflow) {
    throw new Error(`Import & history overflows the ${viewport[0]}px viewport: ${JSON.stringify(accessible.viewport)}.`);
  }
  const browserReport = {
    viewport: accessible.viewport,
    uploadHierarchy,
    review: reviewBaseline,
    reviewKeyboardFocus: {
      tabSteps: saveFocusTrail.length,
      visible: focusedSave.visible,
      focusShadow: focusedSave.boxShadow,
      restoredToTitle: focusAfterSave.label === 'Recipe title',
    },
    importHeadings: accessible.headings.map(item => item.text),
    importControls: accessible.controls.filter(item => [
      'Select all eligible recipes', 'Recipes to import', 'Mealie tag (optional)',
      'Preview import', 'Find in history', 'History page',
    ].includes(item.name)).map(({ name, disabled, x, y, width, height }) => ({
      name, disabled, x, y, width, height,
    })),
    screenshots: artifacts.map(path => path.split(/[\\/]/).at(-1)),
  };
  await writeFile(join(evidence, 'browser-report.json'), JSON.stringify(browserReport, null, 2), 'utf8');
  console.log(JSON.stringify({ completed: true, artifacts, final: accessible }, null, 2));
  ws.close();
} finally {
  if (browser && browser.exitCode === null) {
    if (process.platform === 'win32') {
      const { spawnSync } = await import('node:child_process');
      spawnSync('taskkill.exe', ['/PID', String(browser.pid), '/T', '/F'], { windowsHide: true });
    } else browser.kill('SIGTERM');
  }
  await sleep(150); await rm(profile, { recursive: true, force: true }).catch(() => {});
  await fixture.close();
}

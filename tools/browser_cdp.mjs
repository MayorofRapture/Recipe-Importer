#!/usr/bin/env node
// Minimal local Chromium CDP runner for environments with a cached browser but
// no installed Playwright package. It has no npm dependencies and blocks all
// browser requests except to 127.0.0.1.
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

function arg(name, fallback = undefined) {
  const prefix = `--${name}=`;
  const match = process.argv.slice(2).find((value) => value.startsWith(prefix));
  return match ? match.slice(prefix.length) : fallback;
}

const url = arg('url');
if (!url || !/^http:\/\/127\.0\.0\.1(?::\d+)?\//.test(url)) {
  console.error('Usage: node tools/browser_cdp.mjs --url=http://127.0.0.1:8501/ [--viewport=1365x900] [--screenshot=C:\\temp\\screen.png]');
  process.exit(2);
}

const viewport = (arg('viewport', '1365x900').match(/^(\d+)x(\d+)$/) || []).slice(1).map(Number);
if (viewport.length !== 2) throw new Error('Viewport must use WIDTHxHEIGHT.');
const readyRegex = arg('ready-regex', 'Save draft|Save & next|Approve & next');
const [width, height] = viewport;
const tabCount = Number(arg('tab-count', '0'));
if (!Number.isInteger(tabCount) || tabCount < 0 || tabCount > 20) throw new Error('--tab-count must be between 0 and 20.');
const zoomPercent = Number(arg('zoom-percent', '100'));
if (![100, 200].includes(zoomPercent)) throw new Error('--zoom-percent must be 100 or 200.');
const defaultExe = join(process.env.LOCALAPPDATA || '', 'ms-playwright', 'chromium_headless_shell-1200', 'chrome-headless-shell-win64', 'chrome-headless-shell.exe');
const executable = resolve(arg('executable', defaultExe));
const profile = await mkdtemp(join(tmpdir(), 'recipe-importer-browser-profile-'));
const outputPath = arg('screenshot');
const settledScreenshotPath = arg('settled-screenshot', outputPath);
const scrollText = arg('scroll-text', '');
const bulkEdit = arg('bulk-edit', '');
const recipeFieldPattern = /^(Ingredient\s+\d+|Step\s+\d+)$/i;
if (!recipeFieldPattern.test('Ingredient 1') || !recipeFieldPattern.test('Step 2')) {
  throw new Error('Recipe editor selector self-check failed.');
}
const reviewTimeoutMs = Number(arg('review-timeout-ms', '180000'));
if (!Number.isInteger(reviewTimeoutMs) || reviewTimeoutMs < 30000 || reviewTimeoutMs > 300000) {
  throw new Error('--review-timeout-ms must be between 30000 and 300000.');
}
let browser;

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 0;
    this.pending = new Map();
    this.listeners = new Map();
    ws.addEventListener('message', async (event) => {
      const raw = typeof event.data === 'string' ? event.data
        : event.data instanceof Blob ? await event.data.text() : Buffer.from(event.data).toString('utf8');
      const message = JSON.parse(raw);
      if (message.id <= 12 || message.method === 'Runtime.exceptionThrown') {
        console.error(`CDP message: ${message.method || `response ${message.id}${message.error ? ` error=${message.error.message}` : ''}`}`);
      }
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) reject(new Error(message.error.message));
        else resolve(message.result);
      } else if (message.method) {
        for (const handler of this.listeners.get(message.method) || []) handler(message.params);
      }
    });
  }
  on(method, handler) {
    const handlers = this.listeners.get(method) || [];
    handlers.push(handler);
    this.listeners.set(method, handlers);
  }
  send(method, params = {}) {
    const id = ++this.nextId;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Timed out waiting for CDP ${method}.`));
      }, 10000);
      this.pending.set(id, {
        resolve: (value) => { clearTimeout(timeout); resolve(value); },
        reject: (error) => { clearTimeout(timeout); reject(error); },
      });
    });
  }
}

async function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }
async function evaluate(cdp, expression) {
  const result = await cdp.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || 'Browser evaluation failed.');
  return result.result?.value;
}

try {
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
  console.error(`Chromium starting: ${executable}`);

  const activePort = join(profile, 'DevToolsActivePort');
  let port;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try { port = Number((await readFile(activePort, 'utf8')).split(/\r?\n/)[0]); break; }
    catch { if (browser.exitCode !== null) throw new Error(`Chromium exited with code ${browser.exitCode}.`); await sleep(100); }
  }
  if (!port) throw new Error('Chromium did not expose its local DevTools endpoint.');
  console.error(`Chromium DevTools ready on 127.0.0.1:${port}`);

  const newTarget = await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(url)}`, { method: 'PUT' });
  if (!newTarget.ok) throw new Error(`DevTools could not open a target (${newTarget.status}).`);
  const target = await newTarget.json();
  console.error('Browser target created.');
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', reject, { once: true });
  });
  const cdp = new CDP(ws);
  console.error('CDP websocket connected.');
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width, height, deviceScaleFactor: 1, mobile: width <= 480,
  });
  const navigationStarted = Date.now();
  await cdp.send('Page.navigate', { url });
  console.error(`Navigating to ${url}`);

  let loaded = false;
  for (let attempt = 0; attempt < Math.ceil(reviewTimeoutMs / 250); attempt += 1) {
    await sleep(250);
    loaded = await evaluate(cdp, `(() => document.readyState === 'complete' && !!document.querySelector('[data-testid="stApp"]')
      && new RegExp(${JSON.stringify(readyRegex)}).test(document.body.innerText))()`)
      .catch(() => false);
    if (loaded) break;
  }
  if (!loaded) {
    const state = await evaluate(cdp, `JSON.stringify({state:document.readyState,title:document.title,body:document.body?.innerText?.slice(0,1200),html:document.documentElement?.outerHTML?.slice(0,1500)})`).catch((error) => `evaluate error: ${error.message}`);
    throw new Error(`Streamlit app did not finish its review controls within ${reviewTimeoutMs}ms. Page: ${state}`);
  }
  const settleStarted = Date.now();
  let settleEvidence = null;
  let consecutiveSettledChecks = 0;
  for (let attempt = 0; attempt < 120; attempt += 1) {
    settleEvidence = await evaluate(cdp, `(() => {
      const skeletons=[...document.querySelectorAll('[data-testid*="skeleton" i], [class*="skeleton" i]')];
      const sourceImage=[...document.images].find((image)=>image.alt.startsWith('Recipe source page'));
      const labelOf=(field)=>(field.getAttribute('aria-label')||
        [...(field.labels||[])].map(label=>label.innerText).join(' ')||'').trim();
      const title=[...document.querySelectorAll('input')].find((input)=>labelOf(input)==='Recipe title');
      const fieldPattern=new RegExp(${JSON.stringify(recipeFieldPattern.source)}, 'i');
      const recipeFields=[...document.querySelectorAll('input,textarea')].filter((field)=>{
        const label=labelOf(field);
        return fieldPattern.test(label)&&!!field.value?.trim()
          && field.getClientRects().length>0 && !field.disabled;
      });
      const editorTabs=[...document.querySelectorAll('[role="tab"]')];
      const activeTab=editorTabs.find(tab=>tab.getAttribute('aria-selected')==='true')?.innerText?.trim() || '';
      return {settled:skeletons.length===0 && !!sourceImage && sourceImage.complete && sourceImage.naturalWidth>100
          && !!title?.value?.trim() && editorTabs.length===2 && recipeFields.length>0,
        skeletonCount:skeletons.length, sourceImagePresent:!!sourceImage,
        sourceImageComplete:!!sourceImage?.complete, sourceImageWidth:sourceImage?.naturalWidth || 0,
        titleReady:!!title?.value?.trim(), editorTabCount:editorTabs.length, activeTab, populatedRecipeFieldCount:recipeFields.length,
        recipeFieldLabels:[...document.querySelectorAll('input,textarea')].map(field=>({tag:field.tagName,
          label:labelOf(field),value:field.value||'',disabled:field.disabled,visible:field.getClientRects().length>0,
          tabPanel:field.closest('[role="tabpanel"]')?.getAttribute('aria-labelledby')||''})),
        selectedTabs:[...document.querySelectorAll('[role="tab"]')].map(tab=>({text:tab.innerText.trim(),
          selected:tab.getAttribute('aria-selected')}))};
    })()`);
    consecutiveSettledChecks = settleEvidence.settled ? consecutiveSettledChecks + 1 : 0;
    if (consecutiveSettledChecks >= 3) break;
    await sleep(500);
  }
  if (consecutiveSettledChecks < 3) {
    const diagnostic = await evaluate(cdp, `JSON.stringify({body:document.body.innerText.slice(0,1800),controls:[...document.querySelectorAll('[aria-label]')].map(e=>{const p=e.closest('[role=\"tabpanel\"]');return {tag:e.tagName,type:e.type||'',aria:e.getAttribute('aria-label'),labels:[...(e.labels||[])].map(x=>x.innerText.trim()),value:e.value||'',disabled:e.disabled,visible:e.getClientRects().length>0,tabPanel:p?{id:p.id,hidden:p.getAttribute('aria-hidden'),visible:p.getClientRects().length>0}:null}}).filter(e=>/Ingredient|Step|Recipe title/i.test(e.aria||'')),selectedTabs:[...document.querySelectorAll('[role=\"tab\"][aria-selected=\"true\"]')].map(e=>e.innerText.trim()),source:[...document.images].filter(e=>e.alt.startsWith('Recipe source page')).map(e=>({alt:e.alt,complete:e.complete,width:e.naturalWidth}))})`).catch(() => 'page unavailable');
    throw new Error(`Review workspace did not reach a stable, populated state within 60 seconds: ${JSON.stringify(settleEvidence)}; DOM: ${diagnostic}`);
  }
  settleEvidence.waitedMs = Date.now() - settleStarted;
  settleEvidence.consecutiveChecks = consecutiveSettledChecks;

  const browserTiming = { navigationToSettledEditorMs: Date.now() - navigationStarted };
  console.error(`Browser timing: ${JSON.stringify(browserTiming)}`);
  if (settledScreenshotPath) {
    const screenshot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
    const fullPath = resolve(settledScreenshotPath);
    await mkdir(join(fullPath, '..'), { recursive: true });
    await writeFile(fullPath, Buffer.from(screenshot.data, 'base64'));
    console.error(`Settled review screenshot: ${fullPath}`);
  }

  if (bulkEdit) {
    const editorInteractionStarted = Date.now();
    const instructionsTab = await evaluate(cdp, `(() => {
      const e=[...document.querySelectorAll('[role=\"tab\"]')].find(x=>x.innerText.trim()==='Instructions');
      if(!e)return null; e.scrollIntoView({block:'center'}); const r=e.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2};
    })()`);
    if (!instructionsTab) throw new Error('Could not find the Instructions tab for Bulk Edit timing.');
    await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: instructionsTab.x, y: instructionsTab.y, button: 'left', clickCount: 1 });
    await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: instructionsTab.x, y: instructionsTab.y, button: 'left', clickCount: 1 });
    let instructionReady = false;
    for (let attempt = 0; attempt < 120; attempt += 1) {
      instructionReady = await evaluate(cdp, `(() => {
        const selected=[...document.querySelectorAll('[role=\"tab\"]')]
          .find(tab=>tab.getAttribute('aria-selected')==='true')?.innerText?.trim();
        const step=document.querySelector('textarea[aria-label=\"Step 1\"]');
        return selected==='Instructions' && !!step && !!step.value?.trim()
          && step.getClientRects().length>0;
      })()`).catch(() => false);
      if (instructionReady) break;
      await sleep(250);
    }
    if (!instructionReady) throw new Error('Instructions editor did not render for Bulk Edit timing.');
    browserTiming.instructionsTabSettledMs = Date.now() - editorInteractionStarted;
    const selector = await evaluate(cdp, `(() => {
      const wanted=${JSON.stringify(bulkEdit)}.toLowerCase();
      const e=[...document.querySelectorAll('button')]
        .find(button=>(button.innerText||button.getAttribute('aria-label')||'').trim().toLowerCase().includes(wanted));
      if(!e)return null; e.scrollIntoView({block:'center'});
      const r=e.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2};
    })()`);
    if (!selector) throw new Error(`Could not find the ${bulkEdit} button for bulk-edit timing.`);
    const bulkClickStarted = Date.now();
    await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: selector.x, y: selector.y, button: 'left', clickCount: 1 });
    await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: selector.x, y: selector.y, button: 'left', clickCount: 1 });
    let bulkSettled = false;
    for (let attempt = 0; attempt < 120; attempt += 1) {
      bulkSettled = await evaluate(cdp, `(() => {
        const area=document.querySelector('textarea[aria-label="Bulk instructions"]');
        const rect=area?.getBoundingClientRect();
        const preview=document.body.innerText.includes('Preview:') && document.body.innerText.includes('instructions');
        return !!area && !!area.value?.trim() && rect?.width>0 && rect?.height>0
          && preview && ![...document.querySelectorAll('[data-testid*="skeleton" i], [class*="skeleton" i')].length;
      })()`).catch(() => false);
      if (bulkSettled) break;
      await sleep(250);
    }
    if (!bulkSettled) throw new Error('Bulk Edit Instructions did not render a populated editor and preview within 30 seconds.');
    browserTiming.bulkEditInstructionsMs = Date.now() - bulkClickStarted;
    browserTiming.instructionsToBulkReadyTotalMs = Date.now() - editorInteractionStarted;
  }

  if (zoomPercent === 200) {
    await evaluate(cdp, `document.documentElement.style.fontSize='200%'`);
    await sleep(800);
  }

  if (scrollText) {
    const scrolled = await evaluate(cdp, `(() => {
      const text=${JSON.stringify(scrollText)};
      const matches=[...document.querySelectorAll('div,p,span,button,a')].filter(e=>e.innerText?.includes(text));
      const target=matches.sort((a,b)=>a.innerText.length-b.innerText.length)[0];
      target?.scrollIntoView({block:'center',inline:'nearest'});
      return !!target;
    })()`);
    if (!scrolled) throw new Error(`Could not find visible text to scroll to: ${scrollText}`);
    await sleep(400);
  }

  const focusTrace = [];
  for (let index = 0; index < tabCount; index += 1) {
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
    focusTrace.push(await evaluate(cdp, `(() => {
      const e=document.activeElement, r=e?.getBoundingClientRect(), s=e ? getComputedStyle(e) : null;
      return {tag:e?.tagName || null, name:e?.getAttribute('aria-label') || e?.innerText?.trim()?.replace(/\\s+/g,' ').slice(0,100) || '',
        role:e?.getAttribute('role') || null, outline:s ? s.outlineStyle + ' ' + s.outlineWidth + ' ' + s.outlineColor : '',
        visible:!!r && r.width>0 && r.height>0, withinViewport:!!r && r.top>=0 && r.bottom<=innerHeight};
    })()`));
  }

  const snapshot = await evaluate(cdp, `(() => ({
    title: document.title,
    url: location.href,
    viewport: { width: innerWidth, height: innerHeight, documentWidth: document.documentElement.scrollWidth,
      bodyWidth: document.body.scrollWidth, horizontalOverflow: document.documentElement.scrollWidth > innerWidth },
    scroll: { windowX: scrollX, windowY: scrollY, documentTop: document.documentElement.scrollTop,
      bodyTop: document.body.scrollTop, mainTop: document.querySelector('[data-testid="stMain"]')?.scrollTop ?? null,
      blockTop: document.querySelector('[data-testid="stMainBlockContainer"]')?.getBoundingClientRect().top ?? null },
    navigationLinks: [...document.querySelectorAll('a[href]')].map((e) => {
      const r=e.getBoundingClientRect();
      return {name:e.innerText.trim().replace(/\\s+/g,' '), top:Math.round(r.top), bottom:Math.round(r.bottom),
        left:Math.round(r.left), right:Math.round(r.right), visible:!!r.width && !!r.height};
    }),
    rootFontSize: getComputedStyle(document.documentElement).fontSize,
    headings: [...document.querySelectorAll('h1,h2,h3,h4')].map((e) => ({ level: e.tagName, text: e.innerText.trim() })),
    buttonCount: document.querySelectorAll('button').length,
    buttons: [...document.querySelectorAll('button')].slice(0, 28).map((e) => ({
      text: e.innerText.trim().replace(/\\s+/g, ' ').slice(0, 120),
      name: e.getAttribute('aria-label') || e.innerText.trim().replace(/\\s+/g, ' ').slice(0, 120),
      disabled: e.disabled, box: (() => { const r=e.getBoundingClientRect(); return {x:Math.round(r.x),y:Math.round(r.y),width:Math.round(r.width),height:Math.round(r.height)} })()
    })),
    inputs: [...document.querySelectorAll('input,textarea,[role="combobox"]')].map((e) => ({
      tag: e.tagName, role: e.getAttribute('role'), type: e.type || null,
      label: e.getAttribute('aria-label') || e.labels?.[0]?.innerText?.trim() || e.getAttribute('placeholder') || '',
      disabled: e.disabled || e.getAttribute('aria-disabled') === 'true', outerHTML: e.outerHTML.slice(0, 500),
      container: e.parentElement?.parentElement?.outerHTML?.slice(0, 700) || ''
    })),
    imagesWithoutAlt: [...document.querySelectorAll('img:not([alt])')].length,
    loadingPlaceholders: [...document.querySelectorAll('[data-testid*="skeleton" i], [class*="skeleton" i]')]
      .map((e) => ({testId:e.getAttribute('data-testid'), className:String(e.className).slice(0,120),
        box:(() => { const r=e.getBoundingClientRect(); return {x:Math.round(r.x),y:Math.round(r.y),width:Math.round(r.width),height:Math.round(r.height)} })()})),
    renderedMedia: {
      images:[...document.images].map((e)=>({alt:e.alt,complete:e.complete,naturalWidth:e.naturalWidth,
        box:(()=>{const r=e.getBoundingClientRect();return {x:Math.round(r.x),y:Math.round(r.y),width:Math.round(r.width),height:Math.round(r.height)}})()})),
      canvases:[...document.querySelectorAll('canvas')].map((e)=>{const r=e.getBoundingClientRect();return {width:e.width,height:e.height,box:{x:Math.round(r.x),y:Math.round(r.y),width:Math.round(r.width),height:Math.round(r.height)}}}),
      iframes:[...document.querySelectorAll('iframe')].map((e)=>{const r=e.getBoundingClientRect();return {title:e.title,src:e.src,box:{x:Math.round(r.x),y:Math.round(r.y),width:Math.round(r.width),height:Math.round(r.height)}}})
    },
    activeElement: document.activeElement?.outerHTML?.slice(0, 250) || null,
    text: document.body.innerText.slice(0, 3500)
  }))()`);
  snapshot.settlement = settleEvidence;
  snapshot.browserTiming = browserTiming;
  snapshot.focusTrace = focusTrace;

  if (outputPath) {
    const screenshot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
    const fullPath = resolve(outputPath);
    await mkdir(join(fullPath, '..'), { recursive: true });
    await writeFile(fullPath, Buffer.from(screenshot.data, 'base64'));
    snapshot.screenshot = fullPath;
  }
  console.log(JSON.stringify(snapshot, null, 2));
  ws.close();
} finally {
  browser?.kill();
  await sleep(150);
  await rm(profile, { recursive: true, force: true }).catch(() => {});
}

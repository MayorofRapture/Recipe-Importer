#!/usr/bin/env node
// Shared owner for write-capable Streamlit browser acceptance runs.
import { randomBytes, randomUUID } from 'node:crypto';
import { spawn, spawnSync } from 'node:child_process';
import { createWriteStream } from 'node:fs';
import { lstat, mkdtemp, mkdir, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import net from 'node:net';
import { dirname, isAbsolute, join, parse, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const fixtureParent = join(projectRoot, '.test-tmp', 'browser-fixtures');
const python = join(projectRoot, '.venv', 'Scripts', 'python.exe');
const credentials = ['GEMINI_API_KEY', 'MEALIE_URL', 'MEALIE_TOKEN'];

export function rejectArbitraryUrl(args = process.argv.slice(2)) {
  if (args.some((value) => /^--url(?:=|$)/i.test(value))) {
    throw new Error('Write-capable browser flows create and own their isolated Streamlit server; --url is not accepted.');
  }
}

export function assertNoProviderCredentials(env = process.env) {
  const inherited = credentials.filter((name) => String(env[name] || '').trim());
  if (inherited.length) {
    throw new Error(`Browser fixtures refuse inherited provider credentials: ${inherited.join(', ')}.`);
  }
}

function isWithin(path, root) {
  const rel = relative(root, path);
  return rel === '' || (!rel.startsWith(`..${sep}`) && rel !== '..' && !isAbsolute(rel));
}

function overlaps(path, other) {
  return isWithin(path, other) || isWithin(other, path);
}

export async function assertIsolatedFixture({ root, output, sources, runId, nonce,
  productionOutput = join(projectRoot, 'output') }) {
  for (const value of [root, output, sources, productionOutput]) {
    if (!value || !isAbsolute(value)) throw new Error('Fixture isolation requires absolute filesystem paths.');
  }
  const [realRoot, realOutput, realSources, realProduction] = await Promise.all(
    [root, output, sources, productionOutput].map((value) => realpath(value)));
  if (await [root, output, sources, productionOutput].reduce(async (found, value) =>
    (await found) || hasLinkComponent(value), Promise.resolve(false))) {
    throw new Error('Fixture paths cannot traverse a symlink or Windows junction.');
  }
  if (!isWithin(realOutput, realRoot) || !isWithin(realSources, realRoot)
      || overlaps(realOutput, realSources) || overlaps(realRoot, realProduction)
      || overlaps(realOutput, realProduction)) {
    throw new Error('Fixture output and source paths must stay in a disposable tree outside production output.');
  }
  const expected = { kind: 'synthetic-browser-fixture', run_id: runId, nonce };
  for (const marker of [join(realRoot, '.recipe-importer-browser-fixture.json'),
    join(realOutput, '.fixture-library'), join(realSources, '.fixture-sources')]) {
    let actual;
    try { actual = JSON.parse(await readFile(marker, 'utf8')); }
    catch { throw new Error(`Fixture marker is missing or invalid: ${marker}`); }
    if (JSON.stringify(actual) !== JSON.stringify(expected)) {
      throw new Error('Fixture marker does not match the run-specific attestation.');
    }
  }
  return { root: realRoot, output: realOutput, sources: realSources,
    attestation: `${runId}:${nonce}` };
}

async function hasLinkComponent(path) {
  const absolute = resolve(path);
  const { root: volume } = parse(absolute);
  let current = volume;
  for (const part of relative(volume, absolute).split(sep).filter(Boolean)) {
    current = join(current, part);
    try {
      if ((await lstat(current)).isSymbolicLink()) return true;
    } catch (error) {
      if (error.code === 'ENOENT') break;
      throw error;
    }
  }
  return false;
}

export function assertRequiredCandidates(available, required) {
  const missing = required.filter((title) => !available.includes(title));
  if (missing.length) throw new Error(`Synthetic fixture is missing required candidates: ${missing.join(', ')}.`);
}

export async function prepareEvidenceDirectory(path) {
  const evidencePath = resolve(path);
  const temporaryRoot = resolve(projectRoot, '.test-tmp');
  if (!isWithin(evidencePath, temporaryRoot)) {
    throw new Error('Browser evidence must be written under the project .test-tmp directory.');
  }
  if (await hasLinkComponent(temporaryRoot)) {
    throw new Error('The project .test-tmp directory cannot be a symlink or Windows junction.');
  }
  let current = temporaryRoot;
  const relativeParts = relative(temporaryRoot, evidencePath).split(sep).filter(Boolean);
  for (const part of relativeParts) {
    current = join(current, part);
    try {
      if ((await lstat(current)).isSymbolicLink()) {
        throw new Error('Browser evidence paths cannot traverse a symlink or Windows junction.');
      }
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      break;
    }
  }
  await mkdir(evidencePath, { recursive: true });
  const [realEvidence, realTemp] = await Promise.all([realpath(evidencePath), realpath(temporaryRoot)]);
  if (!isWithin(realEvidence, realTemp)) {
    throw new Error('Browser evidence resolved outside the project temporary directory.');
  }
  return realEvidence;
}

export async function prepareEvidenceFile(path) {
  const filePath = resolve(path);
  const parent = await prepareEvidenceDirectory(dirname(filePath));
  if (!isWithin(filePath, parent)) throw new Error('Browser evidence files must remain inside .test-tmp.');
  try {
    if ((await lstat(filePath)).isSymbolicLink()) {
      throw new Error('Browser evidence files cannot replace a symlink or Windows junction target.');
    }
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  return filePath;
}

function freeLoopbackPort() {
  return new Promise((resolvePort, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close((error) => error ? reject(error) : resolvePort(port));
    });
  });
}

function runPython(args, env, { timeout = 30000 } = {}) {
  const result = spawnSync(python, args, { cwd: projectRoot, env, encoding: 'utf8', timeout,
    windowsHide: true, maxBuffer: 2 * 1024 * 1024 });
  if (result.error || result.status !== 0) {
    throw new Error(`Isolated fixture preparation failed: ${(result.stderr || result.error?.message || '').slice(0, 1600)}`);
  }
  return result.stdout.trim();
}

async function terminateOwned(processHandle) {
  if (!processHandle || processHandle.exitCode !== null) return;
  if (process.platform === 'win32') {
    spawnSync('taskkill.exe', ['/PID', String(processHandle.pid), '/T', '/F'],
      { windowsHide: true, encoding: 'utf8' });
  } else {
    processHandle.kill('SIGTERM');
  }
  if (processHandle.exitCode === null) {
    await Promise.race([
      new Promise((done) => processHandle.once('exit', done)),
      new Promise((done) => setTimeout(done, 5000)),
    ]);
  }
  if (processHandle.exitCode === null && process.platform !== 'win32') processHandle.kill('SIGKILL');
}

async function waitForOwnedApp(server, url, logPath, timeoutMs = 90000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    if (server.exitCode !== null) throw new Error(`Owned Streamlit exited early: ${await readFile(logPath, 'utf8')}`);
    try {
      const response = await fetch(`${url}_stcore/health`, { signal: AbortSignal.timeout(1200) });
      if (response.ok && server.exitCode === null) return;
    } catch { /* wait for the owned process to finish binding */ }
    await new Promise((done) => setTimeout(done, 200));
  }
  throw new Error(`Owned Streamlit did not become healthy: ${(await readFile(logPath, 'utf8')).slice(-2000)}`);
}

export async function startIsolatedBrowserFixture({ scenario = 'review', requiredCandidates = [] } = {}) {
  assertNoProviderCredentials();
  await mkdir(fixtureParent, { recursive: true });
  const root = await mkdtemp(join(fixtureParent, `${scenario}-`));
  const temp = join(root, 'tmp');
  await mkdir(temp);
  const runId = randomUUID();
  const nonce = randomBytes(32).toString('hex');
  const env = { ...process.env, TEMP: temp, TMP: temp, TMPDIR: temp,
    PYTHON_DOTENV_DISABLED: '1',
    RECIPE_IMPORTER_FIXTURE_MODE: '1', RECIPE_IMPORTER_BROWSER_FIXTURE: '1',
    RECIPE_IMPORTER_PRODUCTION_OUTPUT_DIR: join(projectRoot, 'output'),
    RECIPE_IMPORTER_FIXTURE_RUN_ID: runId, RECIPE_IMPORTER_FIXTURE_NONCE: nonce };
  if (scenario === 'performance') env.RECIPE_IMPORTER_UI_DIAGNOSTICS = '1';
  for (const name of credentials) delete env[name];

  let server;
  let serverLog;
  let fixture;
  try {
    const setupOutput = runPython([
      join(projectRoot, 'tools', 'browser_fixture_setup.py'), root, scenario, runId, nonce,
    ], env);
    const manifestLine = setupOutput.split(/\r?\n/).reverse().find((line) => line.trimStart().startsWith('{'));
    if (!manifestLine) throw new Error('Fixture setup did not return its validated path manifest.');
    const manifest = JSON.parse(manifestLine);
    env.RECIPE_IMPORTER_FIXTURE_ROOT = manifest.root;
    env.RECIPE_IMPORTER_OUTPUT_DIR = manifest.output;
    env.RECIPE_IMPORTER_SOURCE_DIR = manifest.sources;
    fixture = await assertIsolatedFixture({ root: manifest.root, output: manifest.output,
      sources: manifest.sources, runId, nonce });
    assertRequiredCandidates(manifest.candidates, requiredCandidates);
    fixture.validFile = manifest.valid_file;
    fixture.invalidFile = manifest.invalid_file;
    fixture.sourceFile = manifest.source_file;
    fixture.manualCandidateId = manifest.manual_candidate_id;
    fixture.candidates = manifest.candidates;
    fixture.candidateIds = manifest.candidate_ids;
    fixture.requiredCandidates = requiredCandidates;
    fixture.runId = runId;
    fixture.nonce = nonce;

    runPython(['-c', 'from runtime_safety import validate_browser_fixture_environment; print(validate_browser_fixture_environment())'], env);
    const port = await freeLoopbackPort();
    fixture.url = `http://127.0.0.1:${port}/`;
    const logPath = join(root, 'streamlit.log');
    serverLog = createWriteStream(logPath, { flags: 'a' });
    server = spawn(python, ['-m', 'streamlit', 'run', join(projectRoot, 'streamlit_app.py'),
      '--server.address=127.0.0.1', `--server.port=${port}`, '--server.headless=true',
      '--browser.gatherUsageStats=false', '--server.fileWatcherType=none'],
    { cwd: fixture.root, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    server.stdout.pipe(serverLog, { end: false });
    server.stderr.pipe(serverLog, { end: false });
    fixture.serverPid = server.pid;
    fixture.close = async () => {
      await terminateOwned(server);
      await new Promise((done) => serverLog.end(done));
      const safe = await assertIsolatedFixture({ root: fixture.root, output: fixture.output,
        sources: fixture.sources, runId, nonce });
      if (!isWithin(safe.root, await realpath(fixtureParent))) {
        throw new Error('Refusing to clean a fixture outside the runner-owned temporary root.');
      }
      await rm(root, { recursive: true, force: true });
      fixture.serverClosed = true;
    };
    await waitForOwnedApp(server, fixture.url, logPath);
    return fixture;
  } catch (error) {
    await terminateOwned(server);
    if (serverLog) await new Promise((done) => serverLog.end(done));
    await rm(root, { recursive: true, force: true }).catch(() => {});
    throw error;
  }
}

export async function assertBrowserFixtureAttestation(cdp, fixture, requiredCandidates = fixture.requiredCandidates) {
  const body = await cdp.send('Runtime.evaluate', {
    expression: 'document.body?.innerText || ""', returnByValue: true,
  }).then((result) => result.result?.value || '');
  if (!body.includes(`Isolated browser fixture: ${fixture.attestation}`)) {
    throw new Error('Refusing browser interaction: the owned app did not render its run-specific fixture attestation.');
  }
  assertRequiredCandidates(fixture.candidates, requiredCandidates);
}

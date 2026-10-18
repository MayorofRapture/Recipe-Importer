import assert from 'node:assert/strict';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  assertBrowserFixtureAttestation,
  assertIsolatedFixture,
  assertNoProviderCredentials,
  assertRequiredCandidates,
  prepareEvidenceDirectory,
  rejectArbitraryUrl,
  startIsolatedBrowserFixture,
} from '../tools/browser_fixture_runner.mjs';

const marker = (runId = 'test-run', nonce = 'a'.repeat(64)) => ({
  kind: 'synthetic-browser-fixture', run_id: runId, nonce,
});

async function fixture(t) {
  const temp = await mkdtemp(path.join(os.tmpdir(), 'recipe-importer-fixture-test-'));
  t.after(() => rm(temp, { recursive: true, force: true }));
  const root = path.join(temp, 'fixture');
  const output = path.join(root, 'output');
  const sources = path.join(root, 'sources');
  const production = path.join(temp, 'production', 'output');
  for (const dir of [output, sources, production]) await mkdir(dir, { recursive: true });
  const value = marker();
  await writeFile(path.join(root, '.recipe-importer-browser-fixture.json'), JSON.stringify(value));
  await writeFile(path.join(output, '.fixture-library'), JSON.stringify(value));
  await writeFile(path.join(sources, '.fixture-sources'), JSON.stringify(value));
  return { root, output, sources, production, runId: value.run_id, nonce: value.nonce };
}

test('valid marked fixture paths attest only inside an isolated root', async (t) => {
  const value = await fixture(t);
  const result = await assertIsolatedFixture(value);
  assert.equal(result.attestation, `${value.runId}:${value.nonce}`);
});

test('fixture path validation rejects production output and nested production paths', async (t) => {
  const value = await fixture(t);
  await assert.rejects(assertIsolatedFixture({ ...value, root: value.production,
    output: value.production, sources: path.join(path.dirname(value.production), 'sources') }));
  const nested = path.join(value.production, 'nested');
  await mkdir(nested);
  await writeFile(path.join(nested, '.fixture-library'), JSON.stringify(marker()));
  await assert.rejects(assertIsolatedFixture({ ...value, output: nested }));
});

test('fixture path validation rejects escaped sources and mismatched marker', async (t) => {
  const value = await fixture(t);
  await assert.rejects(assertIsolatedFixture({ ...value, sources: value.production }));
  await writeFile(path.join(value.sources, '.fixture-sources'), JSON.stringify(marker('forged')));
  await assert.rejects(assertIsolatedFixture(value), /attestation/);
});

test('fixture path validation rejects symlink or junction traversal', async (t) => {
  const value = await fixture(t);
  const alias = path.join(path.dirname(value.root), 'alias');
  try {
    await import('node:fs/promises').then(({ symlink }) => symlink(value.root, alias, 'junction'));
  } catch (error) {
    t.skip(`directory links are unavailable: ${error.message}`);
    return;
  }
  await assert.rejects(assertIsolatedFixture({ ...value, root: alias,
    output: path.join(alias, 'output'), sources: path.join(alias, 'sources') }), /symlink|junction/);
});

test('write-capable harnesses reject caller URLs before launch', () => {
  assert.throws(() => rejectArbitraryUrl(['--url=http://127.0.0.1:8501']), /not accepted/);
  assert.throws(() => rejectArbitraryUrl(['--url']), /not accepted/);
  assert.doesNotThrow(() => rejectArbitraryUrl(['--evidence-dir=synthetic']));
});

test('browser evidence stays in project temp storage and rejects production paths', async (t) => {
  await assert.rejects(prepareEvidenceDirectory(path.resolve('output')),
    /project \.test-tmp/);
  const evidence = path.resolve('.test-tmp', 'browser-runner-test', 'evidence');
  await prepareEvidenceDirectory(evidence);
  t.after(() => rm(path.resolve('.test-tmp', 'browser-runner-test'), { recursive: true, force: true }));
});

test('fixture runner rejects credentials and absent required candidates', () => {
  assert.throws(() => assertNoProviderCredentials({ GEMINI_API_KEY: 'set' }), /credentials/);
  assert.throws(() => assertNoProviderCredentials({ MEALIE_TOKEN: 'set' }), /credentials/);
  assert.throws(() => assertRequiredCandidates(['Fried Rice'], ['Missing Recipe']), /missing/);
  assert.doesNotThrow(() => assertRequiredCandidates(['Fried Rice'], ['Fried Rice']));
});

test('a forged synthetic banner cannot satisfy the run-specific attestation', async (t) => {
  const value = await fixture(t);
  await assert.rejects(assertBrowserFixtureAttestation({
    send: async () => ({ result: { value: 'Demonstration library · simulated services only' } }),
  }, { ...value, attestation: `${value.runId}:${value.nonce}`, candidates: ['Fried Rice'],
    requiredCandidates: ['Fried Rice'] }), /attestation/);
});

test('the shared runner owns an attested synthetic Streamlit process and cleans it up', async (t) => {
  let fixture;
  try {
    fixture = await startIsolatedBrowserFixture({ scenario: 'review', requiredCandidates: ['Fried Rice'] });
  } catch (error) {
    if (error.message.includes('EPERM')) {
      t.skip('This host sandbox blocks child processes launched from Node.');
      return;
    }
    throw error;
  }
  t.after(async () => { if (!fixture.serverClosed) await fixture.close(); });
  assert.ok(fixture.serverPid > 0);
  assert.equal(fixture.candidates.includes('Fried Rice'), true);
  const health = await fetch(`${fixture.url}_stcore/health`);
  assert.equal(health.status, 200);
  await fixture.close();
  assert.equal(fixture.serverClosed, true);
});

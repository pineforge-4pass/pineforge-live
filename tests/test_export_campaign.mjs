import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, access } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { exportCampaign, feedDescriptor, parseArgs, selectProbes, verifiedBytes } from '../scripts/export_campaign.mjs';

const sha = bytes => createHash('sha256').update(bytes).digest('hex');
function fixture() {
  const data = new Map();
  const item = (kind, text) => {
    const bytes = Buffer.from(text); const hash = sha(bytes);
    data.set(hash, { sha256: hash, byte_count: bytes.length, bytes, kind, content_type: 'text/plain' });
    return hash;
  };
  const source = item('strategy', 'strategy("test")');
  const metrics = item('metrics', '{"trades":1}');
  const inputs = item('inputs', '{"length":3}');
  const tape = Buffer.from('trade,price\n1,2\n');
  const feed = Buffer.from('time,open,high,low,close,volume\n0,1,2,1,2,3\n');
  const feedSha = sha(feed);
  const laneSha = item('lane-manifest', JSON.stringify({ lanes: [{ id: 'test-15', sourceRoot: 'data-test', timeframe: '15' }] }));
  const probe = { probe_id: 'scrapper:data-test/standard/test', dataset: 'data-test', slug: 'test', source: 'scrapper',
    symbol: 'TEST', timeframe: '15', group_name: 'standard', surface: 'target', strategy_sha256: source,
    metrics_sha256: metrics, meta_sha256: null, tv_trades_sha256: sha(tape), tv_trade_rows: 1,
    inputs: [{ sha256: inputs, bytes: data.get(inputs).byte_count }] };
  const template = { lane_id: 'test-15', group_name: 'standard', document: {
    symbol: 'TEST', environment: {}, feeds: { chart: { sha256: feedSha, bytes: feed.length, inputs: ['chart-alias'] }, finer: { sha256: feedSha, bytes: feed.length, inputs: ['finer-alias'] } },
    fixedInputs: [{ name: 'chart-alias', sha256: feedSha, bytes: feed.length }, { name: 'finer-alias', sha256: feedSha, bytes: feed.length },
      { name: 'lab-bundle', sha256: 'a'.repeat(64), bytes: 1 }, { name: 'corpus-bundle', sha256: 'b'.repeat(64), bytes: 2 }],
    labRepository: { baseCommit: 'a'.repeat(40), bundleSha256: 'a'.repeat(64), byteCount: 1 },
    corpusRepository: { baseCommit: 'b'.repeat(40), bundleInput: 'corpus-bundle' }, laneManifest: { sha256: laneSha, byteCount: data.get(laneSha).byte_count },
  } };
  const calls = [];
  let released = false;
  const client = { release() { released = true; }, async query(sql, values) {
    calls.push(sql);
    if (sql.startsWith('BEGIN') || sql === 'ROLLBACK') return { rows: [] };
    if (sql.startsWith('SELECT transaction_timestamp')) return { rows: [{ captured_at: '2026-09-09', read_only: 'on', isolation: 'repeatable read' }] };
    if (sql.includes('FROM population_versions')) return { rows: [{ sha256: 'c'.repeat(64), document: { probes: [{ ...probe, probeId: probe.probe_id, group: probe.group_name, tvTradesSha256: probe.tv_trades_sha256, tvTradeRows: 1 }] } }] };
    if (sql.includes('FROM scoreboard_baselines')) return { rows: [{ id: 'baseline', engine_commit: 'd'.repeat(40), codegen_commit: 'e'.repeat(40), snapshot_sha256: 'f'.repeat(64) }] };
    if (sql.includes('FROM snapshots')) return { rows: [{ experiment_id: 'experiment', document: {} }] };
    if (sql.includes('FROM lane_input_templates')) return { rows: [template] };
    if (sql.includes('FROM probes p WHERE')) return { rows: [{ ...probe }] };
    if (sql.includes('FROM code_states')) return { rows: [{ repo: 'engine', base_commit: 'd'.repeat(40), dirty: false }, { repo: 'codegen', base_commit: 'e'.repeat(40), dirty: false }] };
    if (sql.startsWith('SELECT sha256,kind,content_type')) return { rows: values[0].map(hash => data.get(hash)).filter(Boolean) };
    if (sql.includes('FROM probe_tapes')) return { rows: [{ tv_trades_sha256: sha(tape), byte_length: tape.length, bytes: tape }] };
    if (sql.includes('FROM evidence_files e LEFT JOIN evidence_segments')) return { rows: [{ sha256: feedSha, kind: 'feed', byte_count: feed.length, segment_bytes: String(feed.length), segments: 1 }] };
    if (sql.includes('FROM evidence_segments WHERE')) return { rows: [{ seq: 0, byte_count: feed.length, bytes: feed }] };
    throw new Error(`Unexpected query: ${sql}`);
  } };
  return { pool: { connect: async () => client }, client, calls, data, source, probe, template, feedSha, released: () => released };
}

test('parses bounded selectors and rejects unknown or duplicate options', () => {
  assert.deepEqual(parseArgs(['--workflow-root', '../workflow', '--out', 'build/x', '--probe', 'a', '--probe', 'b', '--include-feeds']),
    { workflowRoot: '../workflow', out: 'build/x', probes: ['a', 'b'], includeFeeds: true });
  assert.throws(() => parseArgs(['--out', 'a', '--out', 'b']), /Duplicate/);
  assert.throws(() => parseArgs(['--bad']), /Unknown/);
});

test('selection refuses ambiguous slugs and missing selectors without silently dropping probes', () => {
  const { probe } = fixture();
  const second = { ...probe, probe_id: 'scrapper:other/standard/test', dataset: 'other', symbol: 'OTHER' };
  assert.throws(() => selectProbes([probe, second], { probes: ['test'] }), /matched 2/);
  assert.throws(() => selectProbes([probe], { probes: ['missing'] }), /matched 0/);
  assert.deepEqual(selectProbes([probe, second], { probes: ['test'], symbol: 'TEST' }), [probe]);
});

test('SHA and byte lengths fail closed; alias lookup uses declared ordered part names', () => {
  const bytes = Buffer.from('x');
  assert.deepEqual(verifiedBytes(sha(bytes), 1, bytes), bytes);
  assert.throws(() => verifiedBytes(sha(bytes), 2, bytes), /length mismatch/);
  assert.throws(() => verifiedBytes('0'.repeat(64), 1, bytes), /SHA-256 mismatch/);
  const { template, feedSha } = fixture();
  assert.equal(feedDescriptor(template.document, 'finer').parts[0].sha256, feedSha);
  template.document.feeds.finer.inputs = ['missing-alias'];
  assert.throws(() => feedDescriptor(template.document, 'finer'), /0 fixed-input matches/);
});

test('complete export uses one read-only repeatable snapshot and SHA-addressed private files', async () => {
  const f = fixture(); const out = await mkdtemp(path.join(os.tmpdir(), 'pf-campaign-export-'));
  const manifest = await exportCampaign(f.pool, { out, includeFeeds: true });
  assert.equal(f.calls[0], 'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY');
  assert.equal(f.calls.at(-1), 'ROLLBACK');
  assert(f.calls.every(sql => sql.startsWith('SELECT') || sql.startsWith('BEGIN') || sql === 'ROLLBACK'));
  assert(f.released());
  assert.equal(manifest.selectedProbes, 1);
  assert.equal(manifest.probes[0].lane, 'test-15');
  assert.equal(manifest.probes[0].evidence.inputs.sha256, f.probe.inputs[0].sha256);
  assert.equal(manifest.templates[0].labBundle.sha256, 'a'.repeat(64));
  assert(manifest.evidence.some(e => e.sha256 === f.feedSha));
  for (const descriptor of manifest.evidence) assert.equal(sha(await readFile(path.join(out, descriptor.path))), descriptor.sha256);
  assert.equal(await readFile(path.join(out, '.gitignore'), 'utf8'), '*\n');
  assert.equal(JSON.parse(await readFile(path.join(out, 'manifest.json'), 'utf8')).summary.probes, 1);
});

test('corrupt evidence rolls back and never writes a completion manifest', async () => {
  const f = fixture(); const out = await mkdtemp(path.join(os.tmpdir(), 'pf-campaign-corrupt-'));
  f.data.get(f.source).bytes = Buffer.from('bad');
  await assert.rejects(exportCampaign(f.pool, { out }), /length mismatch/);
  assert.equal(f.calls.at(-1), 'ROLLBACK');
  assert(f.released());
  await assert.rejects(access(path.join(out, 'manifest.json')));
});

test('default export leaves original feed bytes as descriptors', async () => {
  const f = fixture(); const out = await mkdtemp(path.join(os.tmpdir(), 'pf-campaign-descriptors-'));
  const manifest = await exportCampaign(f.pool, { out });
  assert.equal(manifest.feedsIncluded, false);
  assert(!manifest.evidence.some(e => e.sha256 === f.feedSha));
  assert(!f.calls.some(sql => sql.includes('FROM evidence_segments WHERE')));
  assert.equal(manifest.templates[0].finer.parts[0].objectName, `sha256/${f.feedSha}`);
});

test('population drift is refused before selected probe bytes are exported', async () => {
  const f = fixture(); const out = await mkdtemp(path.join(os.tmpdir(), 'pf-campaign-drift-'));
  const query = f.client.query;
  f.client.query = async (sql, args) => {
    const result = await query(sql, args);
    if (sql.includes('FROM population_versions')) result.rows[0].document.probes[0].timeframe = '1D';
    return result;
  };
  await assert.rejects(exportCampaign(f.pool, { out }), /differs from published population/);
  assert.equal(f.calls.at(-1), 'ROLLBACK');
  assert(f.released());
  await assert.rejects(access(path.join(out, 'manifest.json')));
});

test('an unverified feed composition cannot produce a completed packet', async () => {
  const f = fixture(); const out = await mkdtemp(path.join(os.tmpdir(), 'pf-campaign-feed-'));
  f.template.document.feeds.finer.sha256 = '0'.repeat(64);
  await assert.rejects(exportCampaign(f.pool, { out, includeFeeds: true }), /Assembled feed identity mismatch/);
  assert.equal(f.calls.at(-1), 'ROLLBACK');
  await assert.rejects(access(path.join(out, 'manifest.json')));
});

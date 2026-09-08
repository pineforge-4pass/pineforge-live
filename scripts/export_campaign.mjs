#!/usr/bin/env node
// Export a SHA-verified, private input packet. This script never runs strategies
// or writes to the campaign registry. Requires the workflow checkout's Node deps.
import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export class ExportError extends Error {}
const fail = (message) => { throw new ExportError(message); };
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');
const shaPattern = /^[0-9a-f]{64}$/;

export function verifiedBytes(sha256, byteCount, bytes) {
  if (!shaPattern.test(sha256 ?? '')) fail('Evidence descriptor has an invalid SHA-256');
  if (!Number.isSafeInteger(Number(byteCount)) || Number(byteCount) < 0) fail(`Invalid byte count for ${sha256}`);
  if (bytes == null) fail(`Missing bytes for ${sha256}`);
  const result = Buffer.from(bytes);
  if (result.length !== Number(byteCount)) fail(`Byte length mismatch for ${sha256}`);
  if (digest(result) !== sha256) fail(`SHA-256 mismatch for ${sha256}`);
  return result;
}

export function parseArgs(argv) {
  const options = { probes: [], includeFeeds: false };
  const fields = { '--workflow-root': 'workflowRoot', '--out': 'out', '--symbol': 'symbol', '--timeframe': 'timeframe' };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--help') { options.help = true; continue; }
    if (arg === '--include-feeds') { options.includeFeeds = true; continue; }
    if (arg === '--probe' || fields[arg]) {
      const value = argv[++i];
      if (!value || value.startsWith('--')) fail(`Missing value for ${arg}`);
      if (arg === '--probe') options.probes.push(value);
      else if (options[fields[arg]] !== undefined) fail(`Duplicate ${arg}`);
      else options[fields[arg]] = value;
    } else fail(`Unknown argument ${arg}`);
  }
  if (!options.help && (!options.workflowRoot || !options.out)) fail('--workflow-root and --out are required');
  return options;
}

export function selectProbes(probes, { probes: names = [], symbol, timeframe } = {}) {
  let chosen = probes.filter(p => ['hard', 'target'].includes(p.surface));
  if (symbol) chosen = chosen.filter(p => p.symbol === symbol);
  if (timeframe) chosen = chosen.filter(p => p.timeframe === timeframe);
  if (names.length) {
    const ids = new Set();
    for (const name of names) {
      const matches = chosen.filter(p => p.probe_id === name || `${p.dataset}/${p.slug}` === name || p.slug === name);
      if (matches.length !== 1) fail(`Probe selector ${name} matched ${matches.length} probes; use the complete probe ID`);
      ids.add(matches[0].probe_id);
    }
    chosen = chosen.filter(p => ids.has(p.probe_id));
  }
  if (!chosen.length) fail('Selection contains no scored probes');
  return chosen.sort((a, b) => a.probe_id.localeCompare(b.probe_id));
}

export function feedDescriptor(document, role) {
  const feed = document.feeds?.[role];
  if (!feed) return null;
  if (!shaPattern.test(feed.sha256) || !Array.isArray(feed.inputs) || !feed.inputs.length) fail(`Invalid ${role} feed descriptor`);
  const parts = feed.inputs.map(name => {
    const matches = document.fixedInputs.filter(x => x.name === name);
    if (matches.length !== 1) fail(`Feed alias ${name} has ${matches.length} fixed-input matches`);
    const part = matches[0];
    if (!shaPattern.test(part.sha256) || !Number.isSafeInteger(part.bytes) || part.bytes < 0) fail(`Invalid feed part ${name}`);
    return { ...part, objectName: `sha256/${part.sha256}` };
  });
  if (parts.reduce((n, p) => n + p.bytes, 0) !== feed.bytes) fail(`Part length total does not match ${role} feed`);
  return { ...feed, parts };
}

function bundleDescriptor(document, kind) {
  const name = `${kind}-bundle`;
  const matches = document.fixedInputs.filter(x => x.name === name);
  if (matches.length !== 1) fail(`Expected exactly one ${name}`);
  const descriptor = matches[0];
  const repository = document[`${kind}Repository`];
  if (!repository || !shaPattern.test(descriptor.sha256)) fail(`Invalid ${kind} repository descriptor`);
  if (repository.bundleSha256 && repository.bundleSha256 !== descriptor.sha256) fail(`${kind} bundle SHA mismatch`);
  if (repository.byteCount && repository.byteCount !== descriptor.bytes) fail(`${kind} bundle byte count mismatch`);
  return { ...descriptor, baseCommit: repository.baseCommit, objectName: `sha256/${descriptor.sha256}` };
}

const chunks = (values, size) => Array.from({ length: Math.ceil(values.length / size) }, (_, i) => values.slice(i * size, (i + 1) * size));

/** The caller supplies a pg Pool. Every registry read uses ONE snapshot/client. */
export async function exportCampaign(pool, options) {
  const out = path.resolve(options.out);
  await mkdir(out, { recursive: true, mode: 0o700 });
  if ((await readdir(out)).length) fail('Output directory must be empty; choose a fresh packet directory');
  // Private evidence remains ignored even when an operator chooses an output
  // directory other than this repository's already-ignored build/ directory.
  await writeFile(path.join(out, '.gitignore'), '*\n', { mode: 0o600, flag: 'wx' });
  await mkdir(path.join(out, 'evidence'), { mode: 0o700 });
  const client = await pool.connect();
  const evidence = new Map();
  let began = false;
  async function storeEvidence(sha, count, bytes, kind, contentType = 'application/octet-stream') {
    const verified = verifiedBytes(sha, count, bytes);
    const existing = evidence.get(sha);
    if (existing) {
      if (existing.bytes !== verified.length) fail(`Conflicting descriptor for ${sha}`);
      if (!existing.kinds.includes(kind)) existing.kinds.push(kind);
      return existing;
    }
    const descriptor = { sha256: sha, bytes: verified.length, kinds: [kind], contentType, path: `evidence/${sha}` };
    await writeFile(path.join(out, descriptor.path), verified, { mode: 0o600, flag: 'wx' });
    evidence.set(sha, descriptor);
    return descriptor;
  }
  async function fetchEvidence(shas, kindOverride = null) {
    for (const batch of chunks([...new Set(shas)].filter(s => !evidence.has(s)), 64)) {
      const rows = (await client.query('SELECT sha256,kind,content_type,byte_count,bytes FROM evidence_files WHERE sha256=ANY($1)', [batch])).rows;
      const bySha = new Map(rows.map(r => [r.sha256, r]));
      for (const sha of batch) {
        const row = bySha.get(sha);
        if (!row) fail(`Missing evidence catalog row ${sha}`);
        await storeEvidence(sha, row.byte_count, row.bytes, kindOverride ?? row.kind, row.content_type);
      }
    }
  }
  try {
    await client.query('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY');
    began = true;
    const captured = (await client.query('SELECT transaction_timestamp()::text AS captured_at, current_setting(\'transaction_read_only\') AS read_only, current_setting(\'transaction_isolation\') AS isolation')).rows[0];
    if (captured.read_only !== 'on' || captured.isolation !== 'repeatable read') fail('Registry transaction did not enforce the read-only snapshot');
    const population = (await client.query('SELECT sha256,r2_file_sha256,total,hard,target,anomaly,created_at_ms,document FROM population_versions ORDER BY created_at_ms DESC,sha256 DESC LIMIT 1')).rows[0];
    if (!population?.document?.probes?.length) fail('Registry has no published population');
    const baseline = (await client.query('SELECT sequence,id,engine_commit,codegen_commit,snapshot_sha256,activation_kind FROM scoreboard_baselines ORDER BY sequence DESC LIMIT 1')).rows[0];
    if (!baseline) fail('Registry has no baseline');
    const snapshot = (await client.query('SELECT sha256,document,population_sha256,experiment_id,declared,measured,engine_errors,unmeasured FROM snapshots WHERE sha256=$1', [baseline.snapshot_sha256])).rows[0];
    if (!snapshot) fail(`Missing baseline snapshot ${baseline.snapshot_sha256}`);
    const rawTemplates = (await client.query('SELECT lane_id,group_name,document FROM lane_input_templates ORDER BY lane_id,group_name')).rows;
    const candidates = (await client.query(`SELECT p.probe_id,p.dataset,p.slug,p.group_name,p.source,p.surface,p.symbol,p.timeframe,
      p.strategy_sha256,p.tv_trades_sha256,p.metrics_sha256,p.meta_sha256,p.tv_trade_rows,
      (SELECT jsonb_agg(jsonb_build_object('sha256',e.sha256,'bytes',e.byte_count))
       FROM evidence_files e WHERE e.kind='inputs'
       AND (e.name='inputs-'||p.slug OR e.meta->'names' ? ('inputs-'||p.slug))
       AND e.meta->'datasets' ? p.dataset) AS inputs
      FROM probes p WHERE p.surface IN ('hard','target') ORDER BY p.probe_id`)).rows;
    const published = new Map(population.document.probes.filter(p => ['hard', 'target'].includes(p.surface)).map(p => [p.probeId, p]));
    if (published.size !== candidates.length) fail('Current scored registry probes differ from the published population; publish/reconcile through the campaign separately');
    for (const p of candidates) {
      const pinned = published.get(p.probe_id);
      if (!pinned || ['symbol', 'timeframe', 'dataset', 'slug', 'surface', 'source'].some(k => p[k] !== pinned[k])
        || p.group_name !== pinned.group || p.tv_trades_sha256 !== pinned.tvTradesSha256 || p.tv_trade_rows !== pinned.tvTradeRows) {
        fail(`Current probe differs from published population: ${p.probe_id}`);
      }
    }
    const selected = selectProbes(candidates, options);
    for (const p of selected) {
      if ((p.inputs?.length ?? 0) > 1) fail(`Ambiguous inputs evidence for ${p.probe_id}`);
      if (!p.strategy_sha256 || !p.metrics_sha256 || !p.tv_trades_sha256) fail(`Missing required evidence identity for ${p.probe_id}`);
    }

    // Use the campaign's pinned lane manifest to resolve the source dataset;
    // symbol alone cannot distinguish the two ETH source groups.
    await fetchEvidence(rawTemplates.map(t => t.document.laneManifest.sha256));
    const laneDocuments = new Map();
    for (const t of rawTemplates) {
      const sha = t.document.laneManifest.sha256;
      if (!laneDocuments.has(sha)) laneDocuments.set(sha, JSON.parse(await readFile(path.join(out, evidence.get(sha).path), 'utf8')));
    }
    const usedTemplates = new Map();
    for (const p of selected) {
      const sourceRoot = p.dataset === 'corpus' ? 'corpus/validation' : p.dataset;
      const matches = rawTemplates.filter(t => t.group_name === p.group_name
        && laneDocuments.get(t.document.laneManifest.sha256).lanes.some(l => l.id === t.lane_id && l.sourceRoot === sourceRoot && l.timeframe === p.timeframe));
      if (matches.length !== 1) fail(`Probe maps to ${matches.length} lane templates: ${p.probe_id}`);
      const t = matches[0];
      if (t.document.symbol !== p.symbol) fail(`Lane symbol mismatch for ${p.probe_id}`);
      p.lane = t.lane_id;
      p.group = t.group_name;
      usedTemplates.set(`${p.lane}/${p.group}`, t);
    }
    const templates = [...usedTemplates.values()].map(t => ({
      lane: t.lane_id, group: t.group_name, symbol: t.document.symbol,
      script_tf: selected.find(p => p.lane === t.lane_id).timeframe,
      environment: t.document.environment,
      chart: feedDescriptor(t.document, 'chart'), finer: feedDescriptor(t.document, 'finer'),
      daily: feedDescriptor(t.document, 'daily'), corpus: feedDescriptor(t.document, 'corpus'),
      labBundle: bundleDescriptor(t.document, 'lab'), corpusBundle: bundleDescriptor(t.document, 'corpus'),
      laneManifest: t.document.laneManifest, imageDigest: t.document.imageDigest, document: t.document,
    }));
    const codeStates = (await client.query(`SELECT DISTINCT c.bundle_sha256,c.repo,c.base_commit,c.tree_sha,c.dirty,c.byte_count,c.description
      FROM code_states c JOIN engine_trade_observations o
      ON c.bundle_sha256=o.engine_code_sha256 OR c.bundle_sha256=o.codegen_code_sha256
      WHERE o.experiment_id=$1 ORDER BY c.repo,c.bundle_sha256`, [snapshot.experiment_id])).rows;
    for (const repo of ['engine', 'codegen']) {
      if (!codeStates.some(c => c.repo === repo && c.base_commit === baseline[`${repo}_commit`] && !c.dirty)) fail(`Baseline has no matching clean ${repo} code bundle observation`);
    }
    await fetchEvidence(selected.flatMap(p => [p.strategy_sha256, p.metrics_sha256, p.meta_sha256, ...(p.inputs ?? []).map(i => i.sha256)].filter(Boolean)));
    // Fetch tapes in bounded batches: each row may hold a large private CSV.
    for (const batch of chunks([...new Set(selected.map(p => p.tv_trades_sha256))], 16)) {
      const rows = (await client.query('SELECT tv_trades_sha256,byte_length,bytes FROM probe_tapes WHERE tv_trades_sha256=ANY($1)', [batch])).rows;
      const bySha = new Map(rows.map(r => [r.tv_trades_sha256, r]));
      for (const sha of batch) {
        const row = bySha.get(sha);
        if (!row) fail(`Missing TV tape ${sha}`);
        await storeEvidence(sha, row.byte_length, row.bytes, 'tv-trades', 'text/csv');
      }
    }
    const feedParts = [...new Map(templates.flatMap(t => [t.chart, t.finer, t.daily, t.corpus].filter(Boolean).flatMap(f => f.parts)).map(p => [p.sha256, p])).values()];
    const feedCatalog = (await client.query(`SELECT e.sha256,e.kind,e.byte_count,coalesce(sum(s.byte_count),0)::bigint AS segment_bytes,count(s.seq)::int AS segments
      FROM evidence_files e LEFT JOIN evidence_segments s ON s.file_sha256=e.sha256
      WHERE e.sha256=ANY($1) GROUP BY e.sha256 ORDER BY e.sha256`, [feedParts.map(p => p.sha256)])).rows;
    for (const part of feedParts) {
      const row = feedCatalog.find(r => r.sha256 === part.sha256);
      if (!row || row.kind !== 'feed' || row.byte_count !== part.bytes) fail(`Missing/mismatched feed catalog ${part.sha256}`);
      if (options.includeFeeds) {
        const segments = (await client.query('SELECT seq,byte_count,bytes FROM evidence_segments WHERE file_sha256=$1 ORDER BY seq', [part.sha256])).rows;
        if (!segments.length || segments.some((s, i) => s.seq !== i || s.bytes.length !== s.byte_count)) fail(`Missing/noncontiguous feed segments for ${part.sha256}`);
        await storeEvidence(part.sha256, part.bytes, Buffer.concat(segments.map(s => s.bytes)), 'feed', 'text/csv');
      }
    }
    if (options.includeFeeds) {
      const feeds = new Map(templates.flatMap(t => [t.chart, t.finer, t.daily, t.corpus].filter(Boolean)).map(f => [f.sha256, f]));
      for (const feed of feeds.values()) {
        const hasher = createHash('sha256');
        let bytes = 0;
        for (const part of feed.parts) {
          for await (const block of createReadStream(path.join(out, evidence.get(part.sha256).path))) {
            bytes += block.length;
            hasher.update(block);
          }
        }
        if (bytes !== feed.bytes || hasher.digest('hex') !== feed.sha256) fail(`Assembled feed identity mismatch for ${feed.sha256}`);
      }
    }
    const probes = selected.map(p => ({ ...p, evidence: Object.fromEntries([
      ['strategy', p.strategy_sha256], ['tvTrades', p.tv_trades_sha256], ['metrics', p.metrics_sha256],
      ['meta', p.meta_sha256], ['inputs', p.inputs?.[0]?.sha256],
    ].map(([kind, sha]) => [kind, sha ? evidence.get(sha) : null])) }));
    const manifest = {
      schemaVersion: 'pineforge-campaign-input-packet/v1',
      capturedAt: captured.captured_at, registryReadOnly: true, transactionIsolation: captured.isolation,
      evidenceBucket: options.evidenceBucket ?? process.env.PINEFORGE_EVIDENCE_BUCKET ?? 'pineforge-workflow-evidence',
      selection: { probe: options.probes ?? [], symbol: options.symbol ?? null, timeframe: options.timeframe ?? null },
      population, baseline, baselineSnapshot: snapshot, codeStates, templates,
      totalScoredPopulation: published.size, selectedProbes: probes.length, probes,
      feedCatalog, feedsIncluded: Boolean(options.includeFeeds),
      evidence: [...evidence.values()].sort((a, b) => a.sha256.localeCompare(b.sha256)),
    };
    manifest.summary = {
      probes: probes.length, templates: templates.length, evidenceFiles: evidence.size,
      evidenceBytes: [...evidence.values()].reduce((n, e) => n + e.bytes, 0),
      feedObjects: feedParts.length, feedBytes: feedParts.reduce((n, f) => n + f.bytes, 0),
      missingHashes: [],
    };
    // Roll back the read-only transaction before publishing the completion
    // marker. Failed exports can leave bytes but can never leave a manifest.
    await client.query('ROLLBACK');
    began = false;
    await writeFile(path.join(out, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
    return manifest;
  } finally {
    try { if (began) await client.query('ROLLBACK'); }
    finally { client.release(); }
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    console.log('Usage: node scripts/export_campaign.mjs --workflow-root ../pineforge-workflow --out build/campaign-packet [--probe ID ...] [--symbol SYMBOL] [--timeframe TF] [--include-feeds]\n\nExports private, SHA-verified inputs from one repeatable-read READ ONLY registry snapshot.\nDefault: all scored probes. Original feed parts remain GCS descriptors unless --include-feeds\nreads them from SQL evidence_segments. The output directory must be empty. No measurements\nor registry/GCS writes are performed. The workflow checkout must have its dependencies installed.');
    return;
  }
  let pool;
  try {
    const modulePath = path.join(path.resolve(options.workflowRoot), 'campaign/src/lib-registry.mjs');
    const { registryPool } = await import(pathToFileURL(modulePath).href);
    pool = await registryPool();
    const manifest = await exportCampaign(pool, options);
    console.log(JSON.stringify({ out: path.resolve(options.out), ...manifest.summary, populationSha256: manifest.population.sha256, baselineId: manifest.baseline.id }));
  } finally { if (pool) await pool.end(); }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => {
    // Connection subprocess errors can contain environment-specific data.
    console.error(`Campaign export failed: ${error instanceof ExportError ? error.message : 'registry, filesystem, or workflow operation failed; check the workflow checkout, proxy connection and output permissions'}`);
    process.exitCode = 1;
  });
}

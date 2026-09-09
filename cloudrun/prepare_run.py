#!/usr/bin/env python3
"""Freeze a local registry export subset and run manifest; no network or execution."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import tarfile

from pineforge_live.verification.cloud_io import canonical_json_bytes,file_identity
from pineforge_live.verification.selection import select_probes


def prepare(export,output,live_commit,run_id,*,per_lane=2,extra_probe_ids=(),all_probes=False,input_gap_policy='reject'):
    if input_gap_policy not in ('reject','observed'):raise ValueError('invalid input gap policy')
    if not re.fullmatch('[0-9a-f]{40}',live_commit):raise ValueError('full live commit required')
    if not re.fullmatch('[a-z0-9][a-z0-9-]{1,62}',run_id):raise ValueError('bounded run id required')
    export=Path(export);output=Path(output);output.mkdir(parents=True,exist_ok=False)
    source=export/'manifest.json';packet=json.loads(source.read_text())
    selected=select_probes(packet,per_lane=per_lane,all_probes=all_probes)
    selected+=select_probes(packet,probe_ids=extra_probe_ids) if extra_probe_ids else []
    selected=list({p['probe_id']:p for p in selected}.values())
    packet=dict(packet,probes=selected,selectedProbes=len(selected),feedsIncluded=False,
                selection={'method':'all' if all_probes else 'seeded-per-lane-group-plus-explicit',
                           'perLaneGroup':per_lane,'seed':20260909,'extraProbeIds':list(extra_probe_ids),
                           'sourceExport':file_identity(source)})
    paths={d['path']:d for p in selected for d in p['evidence'].values() if d}
    for name,d in paths.items():
        path=(export/name).resolve()
        if not path.is_relative_to(export.resolve()):raise ValueError('export evidence path escape')
        if file_identity(path)!={'sha256':d['sha256'],'bytes':d['bytes']}:raise ValueError('export evidence identity mismatch')
    # Keep all original provenance documents and feed descriptors. Only probe
    # evidence payloads are reduced; total population is never relabeled.
    packet['evidence']=[d for d in packet['evidence'] if d.get('path') in paths] if isinstance(packet['evidence'],list) else packet['evidence']
    packet['summary']={'probes':len(selected),'templates':len(packet['templates']),
                       'evidenceFiles':len(paths),'evidenceBytes':sum(d['bytes'] for d in paths.values()),
                       'sourcePopulation':packet['totalScoredPopulation']}
    packet_path=output/'manifest.json';packet_path.write_bytes(canonical_json_bytes(packet))
    archive=output/'probe-packet.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        tar.add(packet_path,arcname='manifest.json',recursive=False)
        for name in sorted(paths):tar.add(export/name,arcname=name,recursive=False)
    sources={d['repo']:{'sha256':d['bundle_sha256'],'bytes':d['byte_count'],
                              'commit':d['base_commit'],'tree':d['tree_sha']} for d in packet['codeStates']}
    lab=packet['templates'][0]['labBundle']
    if any(t['labBundle']!=lab for t in packet['templates']):raise ValueError('multiple lab source identities')
    sources['lab']={'sha256':lab['sha256'],'bytes':lab['bytes'],'commit':lab['baseCommit']}
    manifest={'schemaVersion':'pineforge-live-two-input-run/v1','runId':run_id,'liveCommit':live_commit,
              'sources':sources,'probePacket':{**file_identity(archive),'manifestSha256':file_identity(packet_path)['sha256']},
              'probeIds':[p['probe_id'] for p in selected],'replayBars':16,'dailyReplayBars':2,
              'tickPolicies':['high-first','low-first'],'seed':20260909,'caseTimeoutSeconds':900,
              'inputGapPolicy':input_gap_policy}
    run=output/'run.json';run.write_bytes(canonical_json_bytes(manifest))
    receipt={'runManifest':file_identity(run),'probePacket':file_identity(archive),
             'liveCommit':live_commit,'selectedProbes':len(selected),'registryMutated':False}
    (output/'receipt.json').write_bytes(canonical_json_bytes(receipt))
    return receipt


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('export','output','live-commit','run-id'):p.add_argument('--'+key,required=True)
    p.add_argument('--per-lane',type=int,default=2);p.add_argument('--extra-probe',action='append',default=[])
    p.add_argument('--all-probes',action='store_true');p.add_argument('--input-gap-policy',choices=('reject','observed'),default='reject');args=vars(p.parse_args())
    args['extra_probe_ids']=args.pop('extra_probe')
    print(json.dumps(prepare(**args),indent=2))


if __name__=='__main__':main()

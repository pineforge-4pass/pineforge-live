"""Deterministic selection from a SHA-pinned, read-only campaign export."""
import hashlib


def select_probes(packet, *, per_lane=2, seed=20260909, probe_ids=(), all_probes=False):
    probes=packet['probes']
    by_id={p['probe_id']:p for p in probes}
    if len(by_id)!=len(probes):raise ValueError('duplicate probe identities')
    if probe_ids:
        missing=set(probe_ids)-set(by_id)
        if missing:raise ValueError('requested probe missing from packet: '+','.join(sorted(missing)))
        return [by_id[k] for k in sorted(set(probe_ids))]
    if all_probes:return sorted(probes,key=lambda p:p['probe_id'])
    if type(per_lane) is not int or per_lane<1:raise ValueError('per_lane must be positive')
    groups={}
    for p in probes:groups.setdefault((p['lane'],p['group']),[]).append(p)
    selected=[]
    for key,group in sorted(groups.items()):
        group.sort(key=lambda p:hashlib.sha256(f'{seed}:{p["probe_id"]}'.encode()).hexdigest())
        selected.extend(group[:per_lane])
    return selected

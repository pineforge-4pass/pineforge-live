import pytest
from pineforge_live.verification.selection import select_probes


def packet():
 return {'probes':[{'probe_id':str(i),'lane':'a' if i<4 else 'b','group':'standard'} for i in range(8)]}


def test_selection_is_stable_stratified_and_not_outcome_based():
 p=packet();a=select_probes(p);p['probes'].reverse();b=select_probes(p)
 assert a==b and len(a)==4
 assert sum(x['lane']=='a' for x in a)==2
 assert select_probes(p,all_probes=True)==sorted(p['probes'],key=lambda x:x['probe_id'])


def test_explicit_identity_and_missing_selection():
 p=packet();assert [x['probe_id'] for x in select_probes(p,probe_ids=['3','1'])]==['1','3']
 with pytest.raises(ValueError):select_probes(p,probe_ids=['missing'])
 p['probes'].append(p['probes'][0])
 with pytest.raises(ValueError):select_probes(p)

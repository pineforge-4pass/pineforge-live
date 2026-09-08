"""Run-packet identity checks over generated fixture evidence only."""
import json
import tarfile
import pytest
from cloudrun.prepare_run import prepare
from pineforge_live.verification.cloud_io import canonical_json_bytes,file_identity,safe_extract_tar


def export(tmp_path):
    root=tmp_path/'export';(root/'evidence').mkdir(parents=True)
    p=root/'evidence/source';p.write_bytes(b'fixture source')
    desc={**file_identity(p),'path':'evidence/source'}
    packet={'probes':[{'probe_id':str(i),'lane':'a' if i<4 else 'b','group':'test','evidence':{'strategy':desc}} for i in range(8)],
            'evidence':[desc],'totalScoredPopulation':8,
            'templates':[{'labBundle':{'sha256':'c'*64,'bytes':8,'baseCommit':'d'*40}}],
            'codeStates':[{'repo':repo,'bundle_sha256':'a'*64,'byte_count':9,'base_commit':'b'*40,'tree_sha':'e'*40} for repo in ('engine','codegen')]}
    (root/'manifest.json').write_bytes(canonical_json_bytes(packet))
    return root


def test_freeze_retains_source_population_exact_bytes_and_explicit_selection(tmp_path):
    root=export(tmp_path);out=tmp_path/'out'
    result=prepare(root,out,'f'*40,'test-run',extra_probe_ids=['0'])
    doc=json.loads((out/'run.json').read_text())
    assert doc['liveCommit']=='f'*40 and len(doc['probeIds'])==len(set(doc['probeIds']))
    assert result['runManifest']==file_identity(out/'run.json')
    dest=tmp_path/'readback';safe_extract_tar(out/'probe-packet.tar.gz',dest)
    packet=json.loads((dest/'manifest.json').read_text())
    assert packet['totalScoredPopulation']==8 and packet['summary']['evidenceFiles']==1
    assert file_identity(dest/'evidence/source')==file_identity(root/'evidence/source')
    assert packet['selection']['sourceExport']==file_identity(root/'manifest.json')
    assert doc['probePacket']['manifestSha256']==file_identity(dest/'manifest.json')['sha256']


def test_packet_refuses_modified_source_before_manifest_is_written(tmp_path):
    root=export(tmp_path);(root/'evidence/source').write_bytes(b'changed')
    with pytest.raises(ValueError,match='identity mismatch'):
        prepare(root,tmp_path/'out','f'*40,'test-run')
    assert not (tmp_path/'out/run.json').exists()

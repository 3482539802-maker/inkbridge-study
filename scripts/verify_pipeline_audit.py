"""Final audit rejects partial, stale, foreign and inconsistent derived data."""
import json,tempfile,unittest
from pathlib import Path
from coordinate_stream import CoordinateStream
from verify_coordinate_acceptance import fixture
from audit_pipeline_session import audit


class AuditChecks(unittest.TestCase):
    def exercise(self,change=None):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw=root/'raw.jsonl';metadata=root/'session.json'
            points,state,frames=fixture()
            raw.write_text(''.join(json.dumps(p)+'\n' for p in points))
            metadata.write_text(json.dumps({'session_id':'synthetic','state':'completed','byte_length':raw.stat().st_size,
                                           'point_count':len(points),'dropped_count':0}))
            store=CoordinateStream(root/'derived',raw,'synthetic')
            try:
                store.ingest()
                for f in frames:store.add_geometry(f)
                while store.step(state,final=True):pass
                store.export()
                if change:change(store,raw,metadata)
            finally:store.close()
            original=raw.read_bytes();result=audit(root/'derived',raw,metadata)
            self.assertEqual(raw.read_bytes(),original)
            return result

    def test_whole_final_export_matches_raw_without_precision_claim(self):
        result=self.exercise()
        self.assertTrue(result['raw_index_coordinates_export_equal']);self.assertEqual(result['pen_points'],4)
        self.assertEqual(result['latency_ms']['source_observation_ms']['median'],500)
        self.assertFalse(result['physical_mm_accuracy_verified']);self.assertFalse(result['device_byte_equality_checked'])

    def test_unindexed_tail_and_incomplete_export_rejected(self):
        def raw_tail(store,raw,meta):
            with raw.open('ab') as out:out.write(b'{}\n')
        def export_tail(store,raw,meta):
            with (store.directory/'points.jsonl').open('ab') as out:out.write(b'{}\n')
        def partial_export(store,raw,meta):
            p=store.directory/'points.jsonl';p.write_bytes(p.read_bytes()[:-1])
        for change in (raw_tail,export_tail,partial_export):
            with self.assertRaises(ValueError):self.exercise(change)

    def test_stale_revision_or_wrong_provenance_rejected_even_if_export_matches(self):
        for change in ({'provisional_live_revision':True},{'session_id':'wrong'},
                       {'screen_xy':[9,9]},{'action_time_us':0}):
            def corrupt(store,raw,meta):
                seq,payload=store.db.execute('SELECT seq,payload FROM coordinates LIMIT 1').fetchone()
                p=json.loads(payload);p.update(change)
                with store.db:store.db.execute('UPDATE coordinates SET payload=? WHERE seq=?',(json.dumps(p),seq))
                store.export()
            with self.assertRaises(ValueError):self.exercise(corrupt)

    def test_bad_index_counts_metadata_or_live_state_rejected(self):
        def index(store,raw,meta):
            with store.db:store.db.execute('UPDATE raw_index SET start=start+1 WHERE seq=1')
        def count(store,raw,meta):
            with store.db:store.db.execute("UPDATE counters SET n=n+1 WHERE key='pens'")
        def ongoing(store,raw,meta):
            m=json.loads(meta.read_bytes());m['state']='recording';meta.write_text(json.dumps(m))
        def not_final(store,raw,meta):
            with store.db:store.set('final_complete',False)
        for change in (index,count,ongoing,not_final):
            with self.assertRaises(ValueError):self.exercise(change)


if __name__=='__main__':unittest.main(verbosity=2)

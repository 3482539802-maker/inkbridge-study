"""Historical raw input provenance and conservative recognition eligibility."""
import hashlib,json,sqlite3,tempfile,unittest
from contextlib import closing
from pathlib import Path
from recognition_ink_input import render_contacts,render_screen_contacts


class InkInputChecks(unittest.TestCase):
    def run_fixture(self,mutate=None,ids=(1,),scale=3,screen=False,live=False,preview=False):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw=root/'raw.jsonl';events=[];points=[];coordinates=[]
            for start in (1,3):
                e={'session_id':'s','start_seq':start,'end_seq':start+1,'points':2,'tool_type':1,
                   'pointer_id':0,'window_id':2,'display_id':0,'kind':'pen_contact_pen','provisional':False,
                   'location_evidence':{'scope':'within_bound_page_candidate','page_id':'p',
                                        'source_pdf_sha256':'a'*64,'pdf_page_1based':1}}
                events.append(e)
                for seq in (start,start+1):
                    p={'type':'point','session_id':'s','seq':seq,'tool_type':1,'pointer_id':0,'window_id':2,'display_id':0,
                        'pressure':.5,'down_time':start*1000,
                        'action':1 if seq==start else 3,'action_time':seq*1000,'x':seq*10.,'y':seq*4.}
                    c={'session_id':'s','seq':seq,'screen_xy':[p['x'],p['y']],'action_time_us':p['action_time'],
                        'provisional_live_revision':False,'status':'candidate','pdf_xy':[seq*2.,seq*3.],
                        'page_uuid':'p','source_pdf_sha256':'a'*64,'pdf_page_1based':1}
                    points.append(p);coordinates.append(c)
            if mutate:mutate(events,points,coordinates)
            with closing(sqlite3.connect(root/'pipeline.sqlite')) as db,db,raw.open('wb') as out:
                db.executescript('CREATE TABLE edit_events(payload); CREATE TABLE coordinates(seq,payload);'
                    'CREATE TABLE raw_index(seq,start,end,tool,pointer,win,display);')
                for p,c in zip(points,coordinates):
                    start=out.tell();out.write((json.dumps(p)+'\n').encode());end=out.tell()
                    db.execute('INSERT INTO raw_index VALUES (?,?,?,?,?,?,?)',(p['seq'],start,end,1,0,2,0))
                    if c:db.execute('INSERT INTO coordinates VALUES (?,?)',(p['seq'],json.dumps(c)))
                db.executemany('INSERT INTO edit_events VALUES (?)',[(json.dumps(e),) for e in events])
            before=hashlib.sha256(raw.read_bytes()).hexdigest()
            result=render_screen_contacts(root,raw,'s',ids) if screen else render_contacts(root,raw,'s',ids,scale,allow_provisional=live,allow_coordinate_preview=preview)
            self.assertEqual(before,hashlib.sha256(raw.read_bytes()).hexdigest())
            return result

    def test_complete_explicit_selection_has_byte_provenance_and_no_saved_state_claim(self):
        image,evidence=self.run_fixture(ids=(1,3))
        self.assertEqual(len(evidence['contacts']),2);self.assertEqual(image.shape[2],3)
        self.assertEqual(evidence['bounds_pdf_pt'],[2.,3.,8.,12.])
        self.assertFalse(evidence['edit_operations_applied']);self.assertFalse(evidence['saved_or_current_state_verified'])
        self.assertEqual(len(evidence['contacts'][0]['selected_raw_records_sha256']),64)

    def test_eraser_unknown_provisional_and_partial_page_contacts_rejected(self):
        for change in ({'kind':'erase_candidate_mode_unknown'},{'kind':'pen_contact_tool_unknown'},
                       {'provisional':True},{'session_id':'other'},{'points':3},
                       {'location_evidence':{'scope':'partial_or_unknown'}}):
            with self.assertRaises(ValueError):self.run_fixture(lambda es,ps,cs:es[0].update(change))

    def test_live_contact_is_opt_in_and_still_needs_committed_coordinate_evidence(self):
        image,evidence=self.run_fixture(lambda es,ps,cs:es[0].update(provisional=True),live=True)
        self.assertTrue(evidence['provisional_contact_input_allowed'])
        self.assertFalse(evidence['saved_or_current_state_verified'])
        for change in ({'status':'motion_candidate'},{'status':'unresolved'},{'provisional_live_revision':True}):
            def mutate(es,ps,cs):
                es[0]['provisional']=True;cs[0].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):self.run_fixture(mutate,live=True)

    def test_coordinate_and_raw_time_screen_or_tool_mismatch_rejected(self):
        for change in ({'action_time_us':999},{'screen_xy':[999,2]},{'seq':99},
                       {'page_uuid':'other'},{'status':'unresolved'},{'provisional_live_revision':True},
                       {'pdf_xy':[float('nan'),1]}):
            with self.assertRaises(ValueError):self.run_fixture(lambda es,ps,cs:cs[0].update(change))
        with self.assertRaises(ValueError):self.run_fixture(lambda es,ps,cs:ps[0].update(tool_type=0))
        with self.assertRaises(ValueError):self.run_fixture(lambda es,ps,cs:ps[0].update(action=2))
        with self.assertRaises(ValueError):self.run_fixture(lambda es,ps,cs:cs[0].clear())

    def test_coordinate_preview_is_separate_opt_in_and_keeps_all_identity_gates(self):
        def mark(es,ps,cs):
            for e in es:e['provisional']=True
            for c in cs:c['provisional_live_revision']=True
        image,e=self.run_fixture(mark,ids=(1,3),live=True,preview=True)
        self.assertTrue(e['coordinate_preview_used'])
        self.assertFalse(e['saved_or_current_state_verified'])
        with self.assertRaises(ValueError):self.run_fixture(mark,preview=True)
        for change in ({'status':'motion_candidate'},{'status':'unresolved'},
                       {'page_uuid':'other'},{'session_id':'other'},
                       {'screen_xy':[9,9]},{'action_time_us':99},{'pdf_xy':[float('nan'),0]}):
            def mutate(es,ps,cs):
                mark(es,ps,cs);cs[0].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.run_fixture(mutate,live=True,preview=True)

    def test_cross_page_and_budget_rejected(self):
        def cross(es,ps,cs):
            es[1]['location_evidence']['page_id']='q'
            cs[2]['page_uuid']=cs[3]['page_uuid']='q'
        with self.assertRaises(ValueError):self.run_fixture(cross,ids=(1,3))
        for ids in ((),(1,1),(True,),tuple(range(1,66))):
            with self.assertRaises(ValueError):self.run_fixture(ids=ids)
        with self.assertRaises(ValueError):self.run_fixture(scale=100)
        with self.assertRaises(ValueError):self.run_fixture(lambda es,ps,cs:cs[1].update(pdf_xy=[1e6,1e6]))

    def test_screen_input_keeps_complete_raw_without_promoting_missing_page(self):
        def missing(es,ps,cs):
            es[0]['location_evidence']={'scope':'partial_or_unknown'};cs[0].clear()
        image,e=self.run_fixture(missing,ids=(1,3),screen=True)
        self.assertEqual(len(e['contacts']),2);self.assertFalse(e['page_identity_verified'])
        self.assertFalse(e['screen_to_page_transform_applied']);self.assertFalse(e['canvas_stationarity_verified'])
        self.assertEqual(e['bounds_screen_px'],[10.,4.,40.,16.])

    def test_screen_context_gaps_time_and_tool_changes_rejected(self):
        for mutation in (
            lambda es,ps,cs:ps[2].update(tool_type=0),
            lambda es,ps,cs:ps[2].update(window_id=9),
            lambda es,ps,cs:ps[2].update(action_time=99_000_000),
            lambda es,ps,cs:ps[2].update(action_time=1),
            lambda es,ps,cs:es[1].update(kind='lasso_path_candidate'),
            lambda es,ps,cs:es[0].update(end_seq=1,points=1),
            lambda es,ps,cs:ps[1].update(action=2),
            lambda es,ps,cs:ps[2].update(x=1e6)):
            with self.assertRaises(ValueError):self.run_fixture(mutation,ids=(1,3),screen=True)


if __name__=='__main__':unittest.main(verbosity=2)

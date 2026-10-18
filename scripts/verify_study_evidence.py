import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from edit_events import initialize_ledger
from study_evidence import fragments,publish_study


def event(seq,kind='pen_contact_pen',start=None,end=None,page='page'):
    start=seq*1e6 if start is None else start;end=start+500000 if end is None else end
    return {'kind':kind,'session_id':'s','start_seq':seq,'end_seq':seq,'start_us':start,'end_us':end,
            'page_id':page,'window_id':1,'display_id':0,'provisional':False,
            'location_evidence':{'scope':'within_bound_page_candidate','source_pdf_sha256':'h'}}


class StudyEvidenceChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name)
        self.db=sqlite3.connect(self.directory/'test.sqlite');initialize_ledger(self.db)
        self.store=SimpleNamespace(db=self.db,directory=self.directory,session='s')
    def tearDown(self):self.db.close();self.temp.cleanup()
    def add(self,e):
        with self.db:self.db.execute('INSERT INTO edit_events VALUES (?,?,?)',(str(e['start_seq']),e['kind'],json.dumps(e)))

    def test_tools_do_not_become_writing_or_thinking_time(self):
        for i,kind in enumerate(('pen_contact_pen','erase_candidate_mode_unknown','lasso_path_candidate',
                                 'ui_or_outside_page_contact','pen_contact_tool_unknown'),1):self.add(event(i,kind))
        r=publish_study(self.store,True)
        self.assertEqual(r['totals']['writing_tool']['observed_contact_seconds'],.5)
        self.assertEqual(r['totals']['eraser_tool']['contacts'],1)
        self.assertFalse(r['total_problem_solving_time_known'])
        self.assertEqual(len(r['recent_fragments']),5)

    def test_interval_union_never_double_counts_overlapping_pens(self):
        self.add(event(1,start=0,end=2e6));self.add(event(2,start=1e6,end=3e6))
        self.assertEqual(publish_study(self.store)['totals']['writing_tool']['observed_contact_seconds'],3)

    def test_pages_pauses_and_boundaries_split_fragments(self):
        e=[event(1),event(2),event(3,page='other'),event(20,page='other'),event(21,'input_discontinuity'),event(22)]
        f=fragments(e)
        self.assertEqual(len(f),5);self.assertEqual(f[0]['contact_count'],2)
        self.assertTrue(all(not p['semantic_step_verified'] for p in f))

    def test_recent_window_is_bounded_but_totals_are_complete(self):
        for i in range(600):self.add(event(i))
        report=publish_study(self.store,True)
        self.assertEqual(report['totals']['writing_tool']['contacts'],600)
        self.assertEqual(sum(f['contact_count'] for f in report['recent_fragments']),512)
        self.assertEqual(report['recent_fragments'][0]['first_seq'],88)
        self.assertFalse(json.loads((self.directory/'assistant-context.json').read_bytes())['automatic_model_calls'])

    def test_empty_ledger_and_foreign_session(self):
        self.assertEqual(publish_study(self.store)['totals'],{})
        e=event(1);e['session_id']='other';self.add(e)
        with self.assertRaises(ValueError):publish_study(self.store)


if __name__=='__main__':unittest.main(verbosity=2)

"""Timed tool-channel rejection, restart barriers and recording lifecycle."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from coordinate_stream import CoordinateStream
from edit_observations import ToolReader,EditObservationWorker,SharedContextReader,MARKER,save,contact_context,transition_contact_context


class TimedToolChecks(unittest.TestCase):
    def test_shared_context_reuses_one_checked_read_and_keeps_restart_and_failure_barriers(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            reader=SharedContextReader(folder,['fake'],'synthetic')
            window={'window_id':1,'pid':2,'display_id':0,'focused':True}
            row={'window':window,'context':self.context('lasso'),'lo':1000,'window_end':1050,'hi':1200}
            reader.read=Mock(side_effect=[row,dict(row,lo=1300,window_end=1350,hi=1500),ValueError('PID mismatch')])
            self.assertEqual(reader.window_context(),(window,self.context('lasso')))
            self.assertEqual(reader.window_context(),(window,self.context('lasso')))
            with self.assertRaises(RuntimeError):reader.window_context()
            reader.close()
            with self.assertRaises(RuntimeError):reader.window_context()
            db=sqlite3.connect(Path(folder)/'pipeline.sqlite')
            rows=[json.loads(p) for p, in db.execute('SELECT payload FROM edit_observations ORDER BY id')];db.close()
            self.assertEqual(len(rows),4)
            self.assertEqual([r['valid'] for r in rows],[False,True,False,False])
            self.assertEqual(rows[1]['context']['editing']['selected_tool'],'lasso')
            self.assertEqual(reader.read.call_count,3)

    def test_slow_shared_read_cannot_become_valid_tool_evidence(self):
        window={'window_id':1,'pid':2,'display_id':0,'focused':True}
        with tempfile.TemporaryDirectory() as folder:
            shared=SharedContextReader(folder,['fake'],'synthetic')
            row=dict(window=window,context=self.context(),lo=1000,window_end=1100,hi=2200)
            shared.read=Mock(side_effect=[row,dict(row,lo=2300,window_end=2400,hi=2600)])
            self.assertEqual(shared.window_context(),(window,self.context()))
            self.assertEqual(shared.window_context(),(window,self.context()))
            self.assertFalse(shared.db.execute("SELECT json_extract(payload,'$.valid') FROM edit_observations ORDER BY id DESC").fetchone()[0])
            shared.close()

    def test_shared_reads_keep_both_fresh_focus_checks_and_disjoint_tool_intervals(self):
        import sqlite3
        window={'window_id':1,'pid':2,'display_id':0,'focused':True}
        with tempfile.TemporaryDirectory() as folder:
            reader=SharedContextReader(folder,['fake'],'synthetic')
            reader.reader.hint=window
            raw=reader.marker.join(['1792209975000000000\nbefore',
                '1792209975050000000\nPid: 2\ntree','1792209975200000000'])
            with patch('edit_observations.notes_window',return_value=window), \
                 patch('edit_observations.TabletFrames.parse_context',return_value=self.context()):
                parsed=reader.parse(raw)
            self.assertAlmostEqual(parsed['window_end']-parsed['lo'],50,delta=.001)
            reader.reader.command=Mock(return_value=raw)
            with patch.object(reader,'parse',return_value=parsed):reader.read()
            self.assertEqual(reader.reader.command.call_args.args[0].count('hidumper -s WindowManagerService -a -a'),1)
            reader.read=Mock(side_effect=[dict(parsed,lo=1000+i*300,window_end=1050+i*300,hi=1200+i*300)
                                         for i in range(4)])
            for _ in range(4):reader.window_context()
            rows=[json.loads(p) for p, in reader.db.execute('SELECT payload FROM edit_observations WHERE lo IS NOT NULL ORDER BY id')]
            self.assertEqual(len(rows),2);self.assertTrue(all(r['valid'] for r in rows))
            self.assertLess(rows[0]['device_wall_interval_ms'][1],rows[1]['device_wall_interval_ms'][0])
            reader.close()

    def test_unpaired_tree_cannot_hide_pen_lasso_pen_excursion(self):
        window={'window_id':1,'pid':2,'display_id':0,'focused':True}
        with tempfile.TemporaryDirectory() as folder:
            reader=SharedContextReader(folder,['fake'],'synthetic')
            reader.read=Mock(side_effect=[dict(window=window,context=self.context(tool),
                lo=1000+i*300,window_end=1050+i*300,hi=1200+i*300)
                for i,tool in enumerate(('pen','lasso','pen','pen'))])
            for _ in range(4):reader.window_context()
            rows=[json.loads(p) for p, in reader.db.execute('SELECT payload FROM edit_observations ORDER BY id')]
            self.assertEqual([r.get('context',{}).get('editing',{}).get('selected_tool') for r in rows if r['valid']],['pen','pen'])
            self.assertIn('shared_context_observed_tool_or_identity_transition',[r.get('reason') for r in rows])
            reader.close()

    def test_shared_reader_discards_result_when_user_stops_during_read(self):
        with tempfile.TemporaryDirectory() as folder:
            active=Mock(side_effect=[True,False])
            reader=SharedContextReader(folder,['fake'],'synthetic',active);reader.read=Mock(return_value={})
            with self.assertRaises(RuntimeError):reader.window_context()
            rows=[json.loads(p) for p, in reader.db.execute('SELECT payload FROM edit_observations')]
            self.assertTrue(all(not r['valid'] for r in rows));self.assertIsNone(reader.pending)
            reader.close()

    def test_dense_shared_context_keeps_lasso_evidence_and_intervening_invalid_sample(self):
        import sqlite3
        from types import SimpleNamespace
        db=sqlite3.connect(':memory:')
        from edit_observations import initialize
        initialize(db);db.execute('CREATE TABLE raw_index(seq,tool,action,win,display)')
        for i in range(190):
            row=dict(valid=True,session_id='synthetic',device_wall_interval_ms=[i*180,i*180+100],
                window={'window_id':2,'display_id':0,'focused':True},context=self.context('lasso'))
            save(db,row)
        store=SimpleNamespace(db=db,session='synthetic')
        event=dict(start_us=8_000_000,end_us=26_000_000,window_id=2,display_id=0)
        assessment=dict(usable_candidate=True,offset_ms=0,uncertainty_ms=1,action_time_range_us=[0,40_000_000])
        self.assertEqual(contact_context(store,event,assessment)[1]['tool'],'lasso')
        db.execute("UPDATE edit_observations SET payload=json_set(payload,'$.valid',json('false')) WHERE id=60")
        self.assertEqual(contact_context(store,event,assessment),(True,None));db.close()

    def test_nonwriting_tool_transition_needs_inside_and_after_samples_and_clean_raw(self):
        import sqlite3
        from types import SimpleNamespace
        db=sqlite3.connect(':memory:')
        db.execute('CREATE TABLE raw_index(seq,tool,action,win,display)')
        db.execute('INSERT INTO raw_index VALUES (10,1,1,2,0)')
        event={'start_seq':10,'end_seq':20,'window_id':2,'display_id':0}
        def item(i,span,tool):return {'id':i,'valid':True,'span':span,'title':'note','page_ids':['p'],
            'page_id':'p','tool':tool,'lasso_menu_available_actions':[]}
        samples=[item(1,[1000,1100],'pen'),item(2,[2200,2300],'lasso'),item(3,[3200,3300],'lasso')]
        get=lambda s:transition_contact_context(SimpleNamespace(db=db),event,s,2000,3000,1)
        context=get(samples)
        self.assertEqual(context['tool'],'lasso');self.assertFalse(context['full_contact_tool_verified'])
        self.assertEqual(context['tool_observation_ids'],[1,2,3])
        self.assertIsNone(get([samples[0],samples[2]]),'never infer from a later tool alone')
        from copy import deepcopy
        for mutation in ('invalid','context','pen','after'):
            altered=deepcopy(samples)
            if mutation=='invalid':altered[1]['valid']=False
            if mutation=='context':altered[2]['title']='other'
            if mutation=='pen':altered[1]['tool']='pen'
            if mutation=='after':altered[2]['tool']='pen'
            self.assertIsNone(get(altered),mutation)
        db.execute('INSERT INTO raw_index VALUES (15,0,1,2,0)')
        self.assertIsNone(get(samples),'finger input could have switched tool mid-contact')
        db.close()

    def context(self,tool='eraser'):
        return {'document_title_candidate':'note','active_tab_marker_unique':True,
            'page_layers':[{'page_id':'p'}],'editing':{'selected_tool':tool,'eraser_mode':'stroke'}}

    def test_real_date_envelope_has_two_focus_checks_and_no_image(self):
        reader=ToolReader(['fake']);window={'window_id':1,'pid':2,'display_id':0,'focused':True}
        raw=MARKER.join(['1792209975000000000','before','Pid: 2\ntree','after','1792209975200000000'])
        with patch('edit_observations.notes_window',return_value=window), \
             patch('edit_observations.TabletFrames.parse_context',return_value=self.context()):
            row=reader.parse(raw)
        self.assertTrue(row['valid']);self.assertEqual(row['device_wall_interval_ms'][1]-row['device_wall_interval_ms'][0],200)
        self.assertEqual(row['screenshots'],0)
        reader.command=Mock(return_value=raw)
        with patch.object(reader,'parse',return_value=row):reader.read()
        command=reader.command.call_args.args[0]
        self.assertEqual(command.count('hidumper -s WindowManagerService -a -a'),2)
        self.assertNotIn('snapshot',command)

    def test_bad_precision_slow_read_focus_and_pid_changes_rejected(self):
        reader=ToolReader(['fake'])
        with self.assertRaises(ValueError):reader.parse('bad')
        with self.assertRaises(ValueError):reader.parse(MARKER.join(['1792209975%N','','','','1792209975%N']))
        row=reader.parse(MARKER.join(['1792209975000000000','','','','1792209977000000000']))
        self.assertFalse(row['valid'])
        raw=MARKER.join(['1792209975000000000','before','tree','after','1792209975200000000'])
        w={'window_id':1,'pid':2,'display_id':0,'focused':True}
        with patch('edit_observations.notes_window',side_effect=[w,dict(w,focused=False)]):
            self.assertFalse(reader.parse(raw)['valid'])
        with patch('edit_observations.notes_window',return_value=w), \
             patch('edit_observations.TabletFrames.parse_context',side_effect=ValueError('PID mismatch')):
            self.assertFalse(reader.parse(raw)['valid'])

    def run_context(self,mutate=None):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);store=CoordinateStream(root,root/'raw.jsonl','synthetic')
            try:
                for i,span in enumerate(([1900,2000],[3900,4000],[5900,6000],[7900,8000])):
                    row={'valid':True,'session_id':'synthetic','device_wall_interval_ms':span,
                         'window':{'window_id':2,'display_id':0,'focused':True},'context':self.context()}
                    if mutate:mutate(i,row,store)
                    save(store.db,row)
                assessment={'usable_candidate':True,'offset_ms':0,'uncertainty_ms':1,'action_time_range_us':[0,10000000]}
                return contact_context(store,{'start_us':3000000,'end_us':7000000,'window_id':2,'display_id':0},assessment)
            finally:store.close()

    def test_tool_mapping_needs_no_geometry_and_carries_provenance(self):
        present,context=self.run_context()
        self.assertTrue(present);self.assertEqual(context['tool'],'eraser')
        self.assertEqual(context['tool_observation_ids'],[1,4])
        self.assertEqual(context['evidence'],'independent_device_timed_ui_samples_not_atomic_edit_subscription')

    def test_intervening_tool_changes_failures_and_restart_barriers_reject(self):
        def switch(i,row,store):
            if i==1:row['context']['editing']['selected_tool']='lasso'
        def failure(i,row,store):
            if i==1:row['valid']=False
        def restart(i,row,store):
            if i==2:save(store.db,{'valid':False,'reason':'restart'})
        def wrong_session(i,row,store):
            if i==1:row['session_id']='other'
        for mutation in (switch,failure,restart,wrong_session):
            self.assertEqual(self.run_context(mutation),(True,None))

    def test_page_switch_and_wall_time_reversal_are_barriers(self):
        def page(i,row,store):
            if i==1:row['context']['page_layers'][0]['page_id']='other'
        def reversal(i,row,store):
            if i==2:row['device_wall_interval_ms']=[3800,3900]
        for mutation in (page,reversal):self.assertEqual(self.run_context(mutation),(True,None))

    def test_two_visible_pages_keep_tool_but_do_not_choose_a_page(self):
        def multipage(i,row,store):row['context']['page_layers'].append({'page_id':'q'})
        _,context=self.run_context(multipage)
        self.assertEqual(context['tool'],'eraser');self.assertIsNone(context['page_id'])
        self.assertEqual(context['page_ids'],['p','q'])
        self.assertEqual(context['page_assignment'],'multiple_visible_pages_unassigned')

    def test_legacy_multipage_recovery_requires_original_specific_gate_evidence(self):
        def multipage(i,row,store):
            row.update(schema=1,reason='unstable_context',valid=False,atomic_observation=False,screenshots=0)
            row['context']['page_layers'].append({'page_id':'q'})
        _,context=self.run_context(multipage)
        self.assertTrue(context['legacy_multipage_samples_recovered'])
        for change in ({'schema':2},{'reason':'PID mismatch'},{'atomic_observation':True},
                       {'screenshots':1},{'device_wall_interval_ms':[5900,7500]}):
            def invalid(i,row,store):
                multipage(i,row,store)
                if i==2:row.update(change)
            self.assertEqual(self.run_context(invalid),(True,None))

    def test_multipage_set_change_duplicate_or_unknown_page_is_a_barrier(self):
        for second in ('r','p',None):
            def change(i,row,store):row['context']['page_layers'].append({'page_id':second if i==2 else 'q'})
            self.assertEqual(self.run_context(change),(True,None))

    def test_parser_accepts_two_pages_with_same_focus_and_pid(self):
        reader=ToolReader(['fake']);window={'window_id':1,'pid':2,'display_id':0,'focused':True}
        raw=MARKER.join(['1792209975000000000','before','tree','after','1792209975200000000'])
        context=self.context();context['page_layers'].append({'page_id':'q'})
        with patch('edit_observations.notes_window',return_value=window), \
             patch('edit_observations.TabletFrames.parse_context',return_value=context):
            row=reader.parse(raw)
        self.assertTrue(row['valid']);self.assertEqual(row['schema'],2)

    def test_missing_mode_retains_eraser_tool_but_not_guessed_mode(self):
        def hide_mode(i,row,store):
            if i==2:row['context']['editing']['eraser_mode']=None
        present,context=self.run_context(hide_mode)
        self.assertTrue(present);self.assertEqual(context['tool'],'eraser')
        self.assertIsNone(context['eraser_mode']);self.assertIsNone(context['eraser_mode_evidence'])
        hint=context['eraser_mode_bracket_hint']
        self.assertEqual(hint['mode'],'stroke');self.assertTrue(hint['unobserved_mode_switch_possible'])
        self.assertFalse(hint['used_to_apply_erasure'])

    def test_conflicting_mode_samples_cannot_form_bracket_hint(self):
        def conflict(i,row,store):
            row['context']['editing']['eraser_mode']=(None if i==1 else 'point' if i==2 else 'stroke')
        _,context=self.run_context(conflict)
        self.assertEqual(context['tool'],'eraser');self.assertIsNone(context['eraser_mode'])
        self.assertIsNone(context['eraser_mode_bracket_hint'])

    def test_channel_absence_is_distinct_from_invalid_present_channel(self):
        with tempfile.TemporaryDirectory() as folder:
            store=CoordinateStream(folder,Path(folder)/'raw.jsonl','synthetic')
            try:
                self.assertEqual(contact_context(store,{},{}),(False,None))
                save(store.db,{'valid':False})
                self.assertEqual(contact_context(store,{},{}),(True,None))
            finally:store.close()

    def test_worker_never_reads_ui_after_user_stops(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(ToolReader,'read') as read:
                worker=EditObservationWorker(folder,['fake'],'synthetic',lambda _:False)
                worker.close();read.assert_not_called()
            self.assertEqual(json.loads((Path(folder)/'edit-observation-status.json').read_bytes())['stage'],'stopped')

    def test_worker_discards_a_sample_if_user_stops_during_read(self):
        with tempfile.TemporaryDirectory() as folder:
            active=Mock(side_effect=[True,False])
            with patch.object(ToolReader,'read',return_value={'valid':True}) as read:
                worker=EditObservationWorker(folder,['fake'],'synthetic',active)
                worker.thread.join(timeout=3);worker.close();read.assert_called_once()
            store=CoordinateStream(folder,Path(folder)/'raw.jsonl','synthetic')
            try:
                rows=[json.loads(p) for p, in store.db.execute('SELECT payload FROM edit_observations')]
                self.assertTrue(all(not row['valid'] for row in rows))
            finally:store.close()


if __name__=='__main__':unittest.main(verbosity=2)

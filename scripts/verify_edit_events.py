import copy
import json
from pathlib import Path
import tempfile
import unittest

from edit_events import EditDetector,classify,process_edits,edit_summary,tool_for_contact
from notes_edit_context import summarize_edit_tools
from verify_coordinate_alignment import point
from verify_coordinate_acceptance import fixture
from coordinate_stream import CoordinateStream


def finger(seq,ms,pointer,action,x=100,y=100):
    return dict(point(seq,ms),tool_type=0,pointer_id=pointer,action=action,x=x,y=y)


class EditChecks(unittest.TestCase):
    def tap(self):return [finger(1,1000,1,1),finger(2,1050,2,1,200),
                          finger(3,1120,1,3),finger(4,1140,2,3,200)]
    def detect(self,points):
        d=EditDetector();events=[]
        for p in points:events.extend(d.feed(p))
        return events
    def test_two_finger_tap_is_candidate_not_last_stroke_deletion(self):
        events=self.detect(self.tap());self.assertEqual(len(events),1)
        event=classify(events[0]);self.assertEqual(event['kind'],'undo_gesture_candidate')
        self.assertFalse(event['applied_to_document']);self.assertIsNone(event['affected_stroke_ids'])
    def test_pinch_drag_late_second_finger_and_gaps_do_not_undo(self):
        for mode in ('motion','late','gap','cancel','pen','third'):
            points=self.tap()
            if mode=='motion':points[2]['x']+=50
            if mode=='late':points[1]['action_time']+=180000
            if mode=='gap':points[2]['seq']+=1
            if mode=='cancel':points[2]['action']=0
            if mode=='pen':points[1]['tool_type']=1
            if mode=='third':
                points.insert(2,finger(3,1070,3,1,300))
                for i,p in enumerate(points):p['seq']=i+1
            self.assertFalse(any(e['kind']=='undo_gesture_candidate' for e in self.detect(points)),mode)
    def test_partial_episode_survives_restart(self):
        d=EditDetector();points=self.tap()
        for p in points[:2]:d.feed(p)
        d=EditDetector(json.loads(json.dumps(d.state)))
        result=[]
        for p in points[2:]:result.extend(d.feed(p))
        self.assertEqual(result[0]['kind'],'undo_gesture_candidate')
    def test_finger_move_without_down_never_undo(self):
        points=self.tap();points[0]['action']=2
        self.assertFalse(any(e['kind']=='undo_gesture_candidate' for e in self.detect(points)))
    def test_palm_cancel_with_same_pointer_id_does_not_lose_pen(self):
        points=[finger(1,1000,0,1),dict(point(2,1010),action=1,pointer_id=0),
                finger(3,1014,0,0),dict(point(4,1020),action=2,pointer_id=0),
                dict(point(5,1050),action=3,pointer_id=0)]
        events=self.detect(points)
        pens=[e for e in events if e['kind']=='physical_pen_contact']
        self.assertEqual(len(pens),1);self.assertEqual(pens[0]['start_seq'],2)
        self.assertEqual(pens[0]['points'],3)
        self.assertFalse(any(e['kind']=='undo_gesture_candidate' for e in events))
        points[2]['tool_type']=1
        self.assertFalse(any(e['kind']=='physical_pen_contact' for e in self.detect(points)))
    def test_selected_lasso_not_drawn_as_ordinary_pen_and_targets_unknown(self):
        contact={'kind':'physical_pen_contact','start_xy':[10,10],'end_xy':[12,12],'bounds':[10,10,100,100]}
        result=classify(contact,{'tool':'lasso','page_id':'p','title':'n'})
        self.assertEqual(result['kind'],'lasso_path_candidate')
        self.assertEqual(result['path_shape'],'closed_loop');self.assertIsNone(result['page_translation'])
    def test_eraser_modes_are_distinct_and_preference_does_not_fill_missing_mode(self):
        event={'kind':'physical_pen_contact'}
        for mode,kind in [('stroke','stroke_erase_candidate'),('point','point_erase_candidate'),(None,'erase_candidate_mode_unknown')]:
            result=classify(event,{'tool':'eraser','eraser_mode':mode,'page_id':'p','title':'n'})
            self.assertEqual(result['kind'],kind);self.assertFalse(result['applied_to_document'])
    def test_sidebar_pen_contact_is_not_a_lasso_path(self):
        result=classify({'kind':'physical_pen_contact'},
            {'tool':'lasso','page_id':'p','title':'n','contact_within_sampled_page_bounds':False})
        self.assertEqual(result['kind'],'ui_or_outside_page_contact')
    def test_surrounding_tool_samples_classify_but_missing_or_changed_samples_do_not(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw=root/'raw.jsonl';points,state,frames=fixture()
            raw.write_text(''.join(json.dumps(p)+'\n' for p in points))
            store=CoordinateStream(root/'index',raw,'synthetic');store.ingest()
            try:
                for i,f in enumerate(frames):
                    f['observation']['context']['editing']={'selected_tool':'eraser','eraser_mode':'stroke'}
                    f['observation']['context_before']=copy.deepcopy(f['observation']['context'])
                    store.add_geometry(dict(f,index=i))
                event={'kind':'physical_pen_contact','start_us':6100000,'end_us':7000000,
                       'window_id':2,'display_id':0,'bounds':[100,100,200,200]}
                context=tool_for_contact(store,event,state)
                self.assertEqual(classify(event,context)['kind'],'stroke_erase_candidate')
                self.assertIsNone(tool_for_contact(store,dict(event,window_id=3),state))
                # A tool change visible at the following sample rejects the association.
                f=copy.deepcopy(frames[2]);f['observation']['context']['editing']['selected_tool']='lasso'
                f['observation']['context_before']=copy.deepcopy(f['observation']['context'])
                with store.db:store.db.execute('UPDATE geometry SET payload=? WHERE id=2',(json.dumps(dict(f,index=2)),))
                self.assertIsNone(tool_for_contact(store,event,state))
            finally:store.close()
    def test_component_visibility_and_explicit_mode_selection(self):
        tree={'$type':'root','$children':[{'compid':'eraserButtonSelected','$rect':'[1,1],[80,80]'},
            {'compid':'lassoButtonSelected','visible':'false'},
            {'$attrs':{'content':'笔画擦除'}},{'$attrs':{'content':'点擦除','selected':True}}]}
        result=summarize_edit_tools(tree)
        self.assertEqual(result['selected_tool'],'eraser');self.assertEqual(result['eraser_mode'],'point')
        tree['$children'][3]['$attrs'].pop('selected')
        self.assertIsNone(summarize_edit_tools(tree)['eraser_mode'])
    def test_observed_eraser_highlight_is_a_candidate_and_never_persists(self):
        # Minimal projection of the actual 2026-10-17 menu observation.
        point_mode={'compid':'pointEraserButton','$children':[]}
        stroke_mode={'compid':'lineEraserButton','$children':[]}
        tree={'$children':[point_mode,stroke_mode]}
        blue={'$type':'Row','backgroundColor':'#FF0A59F7'}
        for owner,expected in ((stroke_mode,'stroke'),(point_mode,'point'),(stroke_mode,'stroke')):
            point_mode['$children']=[];stroke_mode['$children']=[];owner['$children']=[blue]
            result=summarize_edit_tools(tree)
            self.assertEqual(result['eraser_mode_style_candidate'],expected)
            self.assertIsNone(result['eraser_mode'])
        point_mode['$children']=[blue]
        self.assertIsNone(summarize_edit_tools(tree)['eraser_mode_style_candidate'])
        self.assertIsNone(summarize_edit_tools({})['eraser_mode_style_candidate'])
        point_mode['visible']='false'
        self.assertEqual(summarize_edit_tools(tree)['eraser_mode_style_candidate'],'stroke')
    def test_lasso_menu_availability_is_not_executed_delete(self):
        tree={'$children':[{'$attrs':{'content':'删除'}},
            {'compid':'lassoPopWindow','$children':[{'$attrs':{'content':'删除'}},
                {'$attrs':{'content':'调整大小'}}]}]}
        result=summarize_edit_tools(tree)
        self.assertEqual(result['lasso_menu_available_actions'],['delete','resize'])
        self.assertFalse(result['lasso_menu_actions_executed'])
        tree['$children'][1]['visible']='false'
        self.assertEqual(summarize_edit_tools(tree)['lasso_menu_available_actions'],[])
    def test_ledger_version_upgrade_replays_derived_events(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw=root/'raw.jsonl';_,state,_=fixture()
            raw.write_text(''.join(json.dumps(p)+'\n' for p in self.tap()))
            store=CoordinateStream(root/'index',raw,'synthetic');store.ingest()
            try:
                while not process_edits(store,state,final=True):pass
                with store.db:
                    store.db.execute('DELETE FROM edit_events');store.set('edit_detector_version',0)
                while not process_edits(store,state,final=True):pass
                self.assertEqual(edit_summary(store)['counts']['undo_gesture_candidate'],1)
            finally:store.close()
    def test_incremental_ledger_resume_and_final_replay_leave_raw_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw=root/'raw.jsonl';points,state,frames=fixture()
            raw.write_text(''.join(json.dumps(p)+'\n' for p in self.tap()))
            before=raw.read_bytes()
            store=CoordinateStream(root/'index',raw,'synthetic');store.ingest()
            try:
                while not process_edits(store,state,final=True):pass
                summary=edit_summary(store)
                self.assertEqual(summary['counts']['undo_gesture_candidate'],1)
                process_edits(store,state,final=True)
                self.assertEqual(edit_summary(store)['counts']['undo_gesture_candidate'],1)
            finally:store.close()
            self.assertEqual(raw.read_bytes(),before)


if __name__=='__main__':unittest.main(verbosity=2)

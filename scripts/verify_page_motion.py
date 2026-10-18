"""Known motions and refusals; generated images are never saved or sent."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from page_motion import PageMotionProbe,MotionAnchorChain,check_visual_anchors
from visual_registration import project
from verify_coordinate_alignment import frame
from verify_multipage_geometry import binding
from coordinate_page_binding import bind_frame


def texture(seed=7,width=960,height=640):
    rng=np.random.default_rng(seed)
    image=np.full((height,width,3),245,np.uint8)
    for _ in range(650):
        x=int(rng.integers(20,width-30));y=int(rng.integers(20,height-20))
        text=chr(int(rng.integers(65,91)))
        cv2.putText(image,text,(x,y),cv2.FONT_HERSHEY_SIMPLEX,.4,(40,40,40),1,cv2.LINE_AA)
    return image


def context(width=960,height=640):
    f=frame(1000)
    c=f['observation']['context'];c['page_layers'][0]['unclipped_window_bounds']=[0,0,width,height]
    return f['observation']['window'],c


def metadata(n,width=960,height=640,reduction=1):
    return dict(request_id='nonce-'+str(n),timestamp_raw=1_000_000_000+n*400_000_000,
        width=width,height=height,pixel_reduction=reduction)


class MotionChecks(unittest.TestCase):
    def chain_rows(self):
        rows=[]
        for i in range(3):
            row=bind_frame(frame(1000+i*400),binding());row['index']=i
            row['observation'].update(native_frame=metadata(i),motion_context_identity_generation_range=[0,0])
            row['registration']['pdf_to_screen']=[[2,0,i*10],[0,2,0]]
            row['registration']['screen_to_pdf']=[[.5,0,-i*5],[0,.5,0]]
            if i:
                row['observation']['page_motion']=dict(candidate=True,previous_request_id='nonce-'+str(i-1),
                    current_request_id='nonce-'+str(i),previous_timestamp_raw=metadata(i-1)['timestamp_raw'],
                    current_timestamp_raw=metadata(i)['timestamp_raw'],pages=[dict(page_uuid='page',candidate=True,
                        previous_screen_to_current_screen=[[1,0,10],[0,1,0]])])
            rows.append(row)
        rows[1]['registration'].update(valid=False,reason='features_unavailable')
        return rows

    def test_chain_checks_relocalization_after_missing_fit_without_locating_any_pen(self):
        chain=MotionAnchorChain();rows=self.chain_rows();original=deepcopy(rows)
        chain.observe(rows[0]);self.assertFalse(chain.observe(rows[1])['checked'])
        result=chain.observe(rows[2])
        self.assertTrue(result['checked']);self.assertEqual(result['geometry_indices'],[0,1,2])
        self.assertTrue(result['pages'][0]['agrees_with_independent_fits'])
        self.assertFalse(result['coordinate_mapping_allowed']);self.assertFalse(result['intermediate_pen_positions_verified'])
        self.assertEqual(rows,original)

    def test_chain_rejects_identity_excursion_missing_link_and_drift(self):
        for change in ('identity','missing','drift','stale','context_failure'):
            with self.subTest(change=change):
                chain=MotionAnchorChain();rows=self.chain_rows();chain.observe(rows[0])
                if change=='identity':rows[1]['observation']['motion_context_identity_generation_range']=[0,2]
                elif change=='missing':rows[1]['observation']['page_motion']['previous_request_id']='wrong'
                elif change=='stale':rows[1]['observation']['native_frame']['timestamp_raw']+=3_000_000_000
                elif change=='context_failure':rows[1]['observation']['motion_context_identity_generation_range']=[0,1]
                else:rows[2]['registration']['pdf_to_screen'][0][2]+=12
                chain.observe(rows[1]);result=chain.observe(rows[2])
                if change=='drift':
                    self.assertTrue(result['checked']);self.assertFalse(result['pages'][0]['agrees_with_independent_fits'])
                else:self.assertFalse(result['checked'])

    def test_bounded_runtime_audit_separates_estimates_and_independent_anchor_checks(self):
        from audit_frame_runtime import audit
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            db=sqlite3.connect(root/'pipeline.sqlite');db.execute('CREATE TABLE geometry(id INTEGER,payload TEXT)')
            row={'index':0,'observation':{'valid':False,'reason':'context_changed','page_motion':{
                'candidate':True,'processing_ms':20,'pages':[{'candidate':True}]}},
                'page_motion_anchor_check':{'checked':True,'pages':[{'agrees_with_independent_fits':False,
                    'max_anchor_disagreement_screen_px':9}]}}
            db.execute('INSERT INTO geometry VALUES (?,?)',(0,json.dumps(row)));db.commit();db.close()
            result=audit(root)
            self.assertEqual(result['page_motion_evidence']['counts']['page_candidates'],1)
            self.assertEqual(result['page_motion_evidence']['anchor_checks']['agreeing_pages'],0)
            self.assertFalse(result['page_motion_evidence']['coordinate_mapping_allowed'])
            self.assertEqual(result['metrics']['page_motion.processing_ms']['median'],20)

    def measure(self,matrix,reduction=1,ink=False):
        a=texture();b=cv2.warpAffine(a,np.float32(matrix),(960,640),borderValue=(245,245,245))
        if ink:cv2.line(b,(340,200),(440,260),(0,0,0),5)
        w,c=context(960*reduction,640*reduction)
        p=PageMotionProbe()
        try:
            self.assertFalse(p.observe(a,metadata(0,960*reduction,640*reduction,reduction),w,c)['candidate'])
            result=p.observe(b,metadata(1,960*reduction,640*reduction,reduction),w,c)
            self.assertTrue(result['candidate'],result)
            self.assertFalse(result['coordinate_mapping_allowed'])
            self.assertFalse(result['independent_clock_verified'])
            json.dumps(result,allow_nan=False)
            return result
        finally:p.close();self.assertIsNone(p.previous)

    def test_translation_zoom_and_new_handwriting_use_background_motion(self):
        for m in ([[1,0,13],[0,1,-9]],[[1.025,0,-12],[0,1.025,-8]]):
            with self.subTest(m=m):
                result=self.measure(m,ink=True)['pages'][0]
                actual=np.asarray(result['previous_screen_to_current_screen'])
                pts=[[100,100],[800,100],[100,500],[800,500]]
                self.assertLess(float(max(np.linalg.norm(project(actual,pts)-project(m,pts),axis=1))),1.)

    def test_reduced_pixels_restore_original_screen_units(self):
        result=self.measure([[1,0,8],[0,1,-6]],reduction=2)['pages'][0]
        m=result['previous_screen_to_current_screen']
        self.assertAlmostEqual(m[0][2],16,delta=.5)
        self.assertAlmostEqual(m[1][2],-12,delta=.5)

    def test_blank_image_and_unrelated_page_never_become_motion_candidate(self):
        w,c=context()
        for a,b in ((np.full((640,960,3),255,np.uint8),)*2,(texture(4),texture(15))):
            p=PageMotionProbe()
            try:
                p.observe(a,metadata(0),w,c)
                self.assertFalse(p.observe(b,metadata(1),w,c)['candidate'])
            finally:p.close()

    def test_identity_time_packet_and_context_changes_reset_evidence(self):
        for change in ('title','window','page','unfocused','duplicate','reversed','gap','bad_reduction','overlap'):
            with self.subTest(change=change):
                w,c=context();p=PageMotionProbe();image=texture();m=metadata(1)
                try:
                    p.observe(image,metadata(0),w,c)
                    if change=='title':c['document_title_candidate']='other'
                    elif change=='window':w['window_id']=99
                    elif change=='page':c['page_layers'][0]['page_id']='other'
                    elif change=='unfocused':w['focused']=False
                    elif change=='duplicate':m['request_id']='nonce-0'
                    elif change=='reversed':m['timestamp_raw']=999
                    elif change=='gap':m['timestamp_raw']+=2_000_000_000
                    elif change=='bad_reduction':m['pixel_reduction']=True
                    else:c['page_layers'].append(dict(page_id='other',unclipped_window_bounds=[100,100,800,500]))
                    result=p.observe(image,m,w,c)
                    self.assertFalse(result['candidate'])
                finally:p.close()

    def test_gray_pixels_expire_without_new_frames_and_close_cancels_timer(self):
        w,c=context();p=PageMotionProbe(ttl=.03)
        p.observe(texture(),metadata(0),w,c)
        timer=p.timer;timer.join(.5)
        self.assertFalse(timer.is_alive());self.assertIsNone(p.previous)
        p.observe(texture(),metadata(1),w,c);p.close()
        self.assertIsNone(p.previous);self.assertIsNone(p.timer)

    def test_two_pages_follow_independent_motion_and_ignore_outside_toolbar(self):
        a=texture();b=a.copy();b[:300]=cv2.warpAffine(a[:300],np.float32([[1,0,9],[0,1,0]]),(960,300),borderValue=(245,)*3)
        b[340:]=cv2.warpAffine(a[340:],np.float32([[1,0,-7],[0,1,0]]),(960,300),borderValue=(245,)*3)
        w,c=context();c['page_layers']=[dict(page_id='page',unclipped_window_bounds=[0,0,960,300]),
            dict(page_id='second',unclipped_window_bounds=[0,340,960,640])]
        p=PageMotionProbe()
        try:
            p.observe(a,metadata(0),w,c);result=p.observe(b,metadata(1),w,c)
            self.assertEqual(len(result['pages']),2)
            for row,expected in zip(result['pages'],(9,-7)):
                self.assertTrue(row['candidate'],row)
                self.assertAlmostEqual(row['previous_screen_to_current_screen'][0][2],expected,delta=.5)
        finally:p.close()

    def test_independent_pdf_anchors_check_error_and_do_not_revive_invalid_frame(self):
        hint=self.measure([[1,0,13],[0,1,-9]])
        a=bind_frame(frame(1000),binding());b=deepcopy(a)
        a['observation']['native_frame']=metadata(0);b['observation']['native_frame']=metadata(1)
        b['observation']['page_motion']=hint
        b['registration']['pdf_to_screen']=[[2,0,13],[0,2,-9]]
        b['registration']['screen_to_pdf']=[[.5,0,-6.5],[0,.5,4.5]]
        original=deepcopy((a,b))
        result=check_visual_anchors(a,b)
        self.assertTrue(result['checked']);self.assertTrue(result['pages'][0]['agrees_with_independent_fits'])
        self.assertEqual((a,b),original)
        b['registration']['pdf_to_screen'][0][2]+=10
        self.assertFalse(check_visual_anchors(a,b)['pages'][0]['agrees_with_independent_fits'])
        b['observation']['valid']=False
        self.assertFalse(check_visual_anchors(a,b)['checked'])
        self.assertFalse(check_visual_anchors(a,b)['coordinate_mapping_allowed'])

    def test_algorithm_failure_only_disables_motion_evidence(self):
        w,c=context();p=PageMotionProbe()
        try:
            with patch('page_motion.cv2.goodFeaturesToTrack',side_effect=cv2.error('synthetic failure')):
                result=p.observe(texture(),metadata(0),w,c)
            self.assertFalse(result['candidate']);self.assertIsNone(p.previous)
        finally:p.close()


if __name__=='__main__':unittest.main(verbosity=2)

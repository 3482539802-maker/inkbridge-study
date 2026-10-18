"""Two visible pages require independent pixels, timing and raw barriers."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import fitz
import numpy as np

from coordinate_alignment import align, prepare_pairs
from coordinate_page_binding import bind_frame
from multipage_geometry import REASON, match_pages, page_frames,rebind_pages
from visual_registration import PDFRegistration, project
from verify_coordinate_alignment import clocks, frame, point


def binding():
    return {'title':'test','note_id':'note','source_pdf_sha256':'a'*64,
        'source_hinote_sha256':'b'*64,'pages':{'page':1,'second':2}}


def multipage_frame(t):
    parent=frame(t)
    layers=[{'page_id':'page','unclipped_window_bounds':[0,0,1000,800]},
            {'page_id':'second','unclipped_window_bounds':[0,850,1000,1650]}]
    parent['observation']['context']['page_layers']=layers
    parent['registration']['screen_size']=[1000,1650]
    children=[]
    for number in (1,2):
        r=deepcopy(parent['registration'])
        r['pdf_page_1based']=number
        delta=850*(number-1)
        r['support_hull_screen_px']=[[0,delta],[1000,delta],[1000,800+delta],[0,800+delta]]
        r['pdf_to_screen'][1][2]=delta;r['screen_to_pdf'][1][2]=-delta/2
        child=bind_frame({'observation':parent['observation'],'registration':r},binding())
        assert child['registration']['valid']
        children.append({'context':child['observation']['context'],'registration':child['registration']})
    parent['registration']={'valid':False,'reason':REASON,'source_pdf_sha256':'a'*64,'page_candidates':children}
    return parent


class CoordinateChecks(unittest.TestCase):
    def run_alignment(self,frames,points):
        prefix=[point(i+1,t-10) for i,t in enumerate((1000,6000,11000))]
        actual=prefix+[dict(p,seq=p['seq']+3) for p in points]
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);raw=root/'raw.jsonl'
            raw.write_text(''.join(json.dumps(p)+'\n' for p in actual))
            original=raw.read_bytes()
            summary=align(raw,clocks(),frames,root/'points.jsonl',stable_runs=True)
            rows=[json.loads(line) for line in (root/'points.jsonl').read_text().splitlines()]
            self.assertEqual(raw.read_bytes(),original)
        return summary,rows[3:]

    def test_each_page_has_its_own_transform_and_page_id(self):
        frames=[multipage_frame(11000),multipage_frame(13000)]
        summary,rows=self.run_alignment(frames,[point(1,12000),dict(point(2,12100),y=1250)])
        self.assertEqual([(p['status'],p['page_uuid'],p['pdf_xy']) for p in rows],
            [('candidate','page',[100,200]),('candidate','second',[100,200])])
        self.assertEqual(summary['geometry_pairs'],2)
        self.assertEqual(summary['valid_geometry_observations'],2)

    def test_many_page_changes_and_return_do_not_stall_or_bridge_gaps(self):
        frames=[]
        for page in range(60):
            for t in (11000+page*3000,12000+page*3000):
                f=multipage_frame(t)
                for layer in f['observation']['context']['page_layers']:
                    layer['page_id']+=(str(page) if page else '')
                for child in f['registration']['page_candidates']:
                    c=child['context'];r=child['registration']
                    uuid=c['page_layers'][0]['page_id']+(str(page) if page else '')
                    c['page_layers'][0]['page_id']=uuid
                    c['visible_page_layers']=deepcopy(f['observation']['context']['page_layers'])
                    r['native_background_binding']['page_uuid']=uuid
                frames.append(f)
        # Returning after 59 different identities cannot bridge intervening
        # missing fits, even with the three-frame stable-run option enabled.
        frames.append(multipage_frame(200000))
        frames.append(multipage_frame(201000))
        pairs=prepare_pairs(frames,True)
        self.assertEqual(len(pairs),122)
        self.assertTrue(all(p['end']-p['start']<2000 for p in pairs))

    def test_cli_explicit_export_keeps_independent_page_fits(self):
        from coordinate_alignment import main
        frames=[multipage_frame(11000),multipage_frame(13000)]
        points=[point(i+1,t-10) for i,t in enumerate((1000,6000,11000))]
        points+=[dict(point(4,12000),y=1250)]
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for name,data in [('raw.jsonl',''.join(json.dumps(p)+'\n' for p in points)),
                ('clock.json',json.dumps(clocks())),
                ('geometry.jsonl',''.join(json.dumps(f)+'\n' for f in frames))]:
                (root/name).write_text(data)
            argv=['coordinate_alignment.py']
            for name,value in [('raw','raw.jsonl'),('clock','clock.json'),('geometry','geometry.jsonl'),
                ('hinote','export.hinote'),('pdf','original.pdf'),('output','result')]:
                argv+=['--'+name,str(root/value)]
            with patch('sys.argv',argv),patch('coordinate_page_binding.load_binding',return_value=binding()):main()
            rows=[json.loads(line) for line in (root/'result/points.jsonl').read_text().splitlines()]
            self.assertEqual(rows[-1]['page_uuid'],'second')
            self.assertEqual(rows[-1]['pdf_xy'],[100,200])

    def test_failed_middle_page_fit_is_a_barrier_only_for_that_page(self):
        frames=[multipage_frame(t) for t in (11000,12000,13000)]
        frames[1]['registration']['page_candidates'].pop()
        _,rows=self.run_alignment(frames,[point(1,11900),dict(point(2,11910),y=1250)])
        self.assertEqual(rows[0]['status'],'candidate')
        self.assertEqual(rows[1]['reason'],'outside_feature_support')
        self.assertIsNone(rows[1]['pdf_xy'])
        self.assertFalse(any(p['a']['observation']['context']['page_layers'][0]['page_id']=='second'
            for p in prepare_pairs(frames,True)))

    def test_context_failure_motion_and_raw_finger_still_block_both_pages(self):
        frames=[multipage_frame(11000),multipage_frame(13000)]
        for mode in ('focus','context','motion','finger'):
            actual=deepcopy(frames);points=[dict(point(1,12000),y=1250)]
            if mode=='focus':actual[1]['observation']['window']['focused']=False
            elif mode=='context':actual[1]['observation']['valid']=False
            elif mode=='motion':
                for child in actual[1]['registration']['page_candidates']:
                    child['registration']['pdf_to_screen'][0][2]+=10
                    child['registration']['screen_to_pdf'][0][2]-=5
            else:points.insert(0,point(1,11500,tool=0));points[-1]['seq']=2
            _,rows=self.run_alignment(actual,points)
            self.assertIsNone(rows[-1]['pdf_xy'],mode)

    def test_corrupt_or_duplicate_child_cannot_revive_parent(self):
        original=multipage_frame(11000)
        for mode in ('duplicate','hash','context','uuid'):
            row=deepcopy(original)
            if mode=='duplicate':row['registration']['page_candidates'].append(deepcopy(row['registration']['page_candidates'][0]))
            elif mode=='hash':row['registration']['source_pdf_sha256']='c'*64
            elif mode=='context':row['observation']['context']['document_title_candidate']='other'
            else:row['registration']['page_candidates'][0]['context']['page_layers'][0]['page_id']='wrong'
            self.assertEqual(page_frames(row),[],mode)

    def test_explicit_rebinding_cannot_keep_a_wrong_document_or_pdf(self):
        row=multipage_frame(11000)
        original=deepcopy(row)
        for key,value in [('source_pdf_sha256','c'*64),('title','other'),('pages',{'page':2,'second':1})]:
            changed=dict(binding(),**{key:value})
            self.assertEqual(page_frames(rebind_pages(row,changed)),[],key)
        rebound=rebind_pages(row,dict(binding(),source_hinote_sha256='d'*64))
        self.assertEqual(len(page_frames(rebound)),2)
        self.assertEqual(row,original)
        self.assertTrue(all(c['registration']['native_background_binding']['source_hinote_sha256']=='d'*64
            for c in rebound['registration']['page_candidates']))

    def test_ambiguous_spatial_support_stays_unknown(self):
        frames=[multipage_frame(11000),multipage_frame(13000)]
        # Corrupt synthetic evidence with overlapping visual support cannot
        # resolve by page order or stronger feature score.
        for f in frames:
            child=f['registration']['page_candidates'][1]
            child['registration']['support_hull_screen_px']=[[0,0],[1000,0],[1000,1650],[0,1650]]
        _,rows=self.run_alignment(frames,[point(1,12000)])
        self.assertEqual(rows[0]['reason'],'ambiguous_bracketing_context')

    def test_all_pages_excluded_is_not_ambiguous_and_assigns_no_page(self):
        frames=[multipage_frame(11000),multipage_frame(13000)]
        for f in frames:
            for c in f['registration']['page_candidates']:c['registration']['pdf_size_pt']=[500,400]
        # The gap between both bound rectangles is outside both visible pages.
        _,rows=self.run_alignment(frames,[dict(point(1,12000),y=825)])
        self.assertEqual(rows[0]['reason'],'outside_bound_page')
        self.assertIsNone(rows[0]['pdf_xy'])
        self.assertNotIn('page_uuid',rows[0])
        self.assertTrue(rows[0]['evidence']['all_bracketed_pages_excluded'])
        self.assertEqual({p['pdf_page_1based'] for p in rows[0]['evidence']['excluded_pages']},{1,2})
        # A held finger can move pages between snapshots: no page-exclusion
        # claim is allowed when every bracket is blocked by raw input.
        _,blocked=self.run_alignment(frames,[point(1,11500,tool=0),dict(point(2,12000),y=825)])
        self.assertEqual(blocked[-1]['reason'],'input_motion_or_context_barrier')

    def test_missing_feature_support_is_not_proof_of_all_page_exclusion(self):
        frames=[multipage_frame(11000),multipage_frame(13000)]
        for f in frames:
            f['registration']['page_candidates'][0]['registration']['support_hull_screen_px']=[
                [500,0],[1000,0],[1000,800],[500,800]]
        _,rows=self.run_alignment(frames,[point(1,12000)])
        self.assertEqual(rows[0]['reason'],'ambiguous_bracketing_context')
        self.assertIsNone(rows[0]['pdf_xy'])

    def test_incremental_native_clock_replay_preserves_both_page_lanes(self):
        from coordinate_stream import CoordinateStream
        from native_frame_timing import bind
        from verify_coordinate_acceptance import fixture
        points,state,native_frames=fixture()
        points[2]['y']=1250
        frames=[]
        for i,(t,original) in enumerate(zip((1000,6000,8000,11000),native_frames)):
            row=multipage_frame(t)
            row['observation']['native_frame']=original['observation']['native_frame']
            frames.append(dict(row,index=i))
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);raw=root/'raw.jsonl'
            raw.write_text(''.join(json.dumps(p)+'\n' for p in points))
            bound,_=bind(frames,state)
            align(raw,state,bound,root/'offline.jsonl',stable_runs=True)
            stream=CoordinateStream(root/'stream',raw,'synthetic')
            try:
                for f in frames:stream.add_geometry(f)
                stream.ingest()
                while stream.step(state,final=True,limit=1):pass
                stream.export()
            finally:stream.close()
            offline=[json.loads(s) for s in (root/'offline.jsonl').read_text().splitlines()]
            online=[json.loads(s) for s in (root/'stream/points.jsonl').read_text().splitlines()]
            keys=lambda rows:[(p['seq'],p['status'],p.get('page_uuid'),p['pdf_xy']) for p in rows]
            self.assertEqual(keys(offline),keys(online))
            self.assertEqual(online[2]['page_uuid'],'second')
            # Acceptance counts physical frames once while validating each
            # strict candidate against the appropriate page within each frame.
            from coordinate_acceptance import audit
            for name,data in [('clock.json',json.dumps(state)),
                ('geometry.jsonl',''.join(json.dumps(f)+'\n' for f in bound))]:
                (root/name).write_text(data)
            report=audit(raw,root/'clock.json',root/'geometry.jsonl',root/'stream/points.jsonl')
            self.assertEqual(report['native_frames'],4)
            self.assertGreater(report['native_bracketed_candidate_pen_points'],0)
            self.assertFalse(report['physical_mm_accuracy_verified'])


class VisualChecks(unittest.TestCase):
    def test_two_real_pdf_pages_in_one_synthetic_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            pdf=Path(temporary)/'two-pages.pdf'
            with fitz.open() as document:
                for n in range(2):
                    p=document.new_page(width=612,height=792)
                    random=np.random.default_rng(83+n)
                    for j in range(27):
                        p.insert_text((35+(j%3)*13,35+j*26),
                            f'Page {n+1} item {j+1}: integral x {random.integers(100,999999)} + {j*71+n*313}')
                document.save(pdf)
            matcher=PDFRegistration(pdf,[0,1])
            try:
                image=np.full((1450,2100,3),235,np.uint8);layers=[]
                with fitz.open(pdf) as document:
                    for i in range(2):
                        pix=document[i].get_pixmap(matrix=fitz.Matrix(1.5,1.5))
                        page=np.frombuffer(pix.samples,np.uint8).reshape(pix.height,pix.width,3)
                        left=60+i*1010;top=90
                        image[top:top+pix.height,left:left+pix.width]=page
                        layers.append({'page_id':('page','second')[i],
                            'unclipped_window_bounds':[left,top,left+pix.width,top+pix.height]})
                o=frame(11000)['observation'];o['context']['page_layers']=layers
                b=binding();b['source_pdf_sha256']=matcher.sha256
                result=match_pages(matcher,image,o,b)
                self.assertEqual(len(result['page_candidates']),2,result)
                self.assertFalse(result['valid'],'root still requires point-specific page selection')
                for i,child in enumerate(result['page_candidates']):
                    mapped=project(child['registration']['pdf_to_screen'],[[200,300]])[0]
                    expected=np.array([60+i*1010+300,90+450])
                    self.assertLess(np.linalg.norm(mapped-expected),3)
                self.assertFalse(result['rectangle_used_for_transform'])
                o['context']['page_layers'][1]['unclipped_window_bounds']=[100,100,900,1300]
                self.assertEqual(match_pages(matcher,image,o,b)['reason'],'multipage_rectangles_invalid_or_overlapping')
            finally:matcher.close()


if __name__=='__main__':unittest.main(verbosity=2)

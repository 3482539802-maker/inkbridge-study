"""Meaningful checks for tool separation, gesture ambiguity and UI geometry."""
import json
from pathlib import Path
import tempfile
from ink_motion import analyze_points, page_motion


def event(seq, action, x, y, pointer=0, tool=0, window=1, at=None):
    return dict(type='point', session_id='synthetic', seq=seq, action=action, pointer_id=pointer,
                x=x, y=y, action_time=seq*1000 if at is None else at, down_time=0,
                pressure=6.41 if tool == 0 else .5, tool_type=tool, window_id=window, display_id=0)


def main():
    checks = []
    r = analyze_points([event(1,1,10,10,tool=1), event(2,2,15,15,tool=1), event(3,3,20,20,tool=1),
                        event(4,1,100,100), event(5,2,100,200), event(6,3,100,200)])
    assert r['tool_point_counts'] == {'pen':3,'finger':3}
    assert len(r['finger_gestures']) == 1 and r['finger_gestures'][0]['page_displacement'] is None
    assert r['contacts'][0]['kind'] == 'pen_path' and r['contacts'][1]['delta_screen_px'] == [0,100]
    checks.append('pen and finger separation, raw finger pressure > 1, no invented page displacement')
    r = analyze_points([event(1,1,100,100,0),event(2,1,200,100,1),
                        event(3,2,300,100,1),event(4,3,300,100,1),event(5,3,100,100,0)])
    assert r['two_finger_intervals'][0]['finger_distance_ratio'] == 2
    assert r['finger_gestures'][0]['max_fingers'] == 2
    checks.append('two pointer distance ratio and centroid, no application scale claim')
    r = analyze_points([event(1,1,100,100),event(2,1,110,110,1,tool=1),
                        event(3,3,120,120,1,tool=1),event(4,3,100,100)])
    assert r['finger_gestures'][0]['pen_overlap_observed']
    checks.append('palm or mixed pen/finger remains ambiguous')
    for tail in ([event(3,3,10,20)], [event(2,0,10,20)], [event(2,3,10,20,at=0)]):
        r = analyze_points([event(1,1,10,10)]+tail)
        assert not r['finger_gestures'][0]['complete']
    checks.append('sequence gaps, cancel and clock reset do not bridge contact intervals')
    r=analyze_points([event(1,1,100,100,window=1),event(2,1,200,100,window=2),
                      event(3,3,100,100,window=1),event(4,3,200,100,window=2)])
    assert not r['two_finger_intervals'] and len(r['finger_gestures']) == 2
    checks.append('contacts in different windows cannot become a pinch')
    r=analyze_points([event(1,2,100,100),event(2,3,120,120),event(3,1,1,1,tool=2)])
    assert not r['finger_gestures'][0]['complete'] and r['contacts'][-1]['kind'] == 'eraser_path'
    assert not r['contacts'][-1]['complete']
    checks.append('missing down, open tail and eraser remain distinct')
    a={'document_title':'test','window_id':'1','display_id':'0','visible_pages':[
        {'page_id':'p','original_bounds':[0,-100,600,700],'visible_bounds':[0,100,600,700]}]}
    b={'document_title':'test','window_id':'1','display_id':'0','visible_pages':[
        {'page_id':'p','original_bounds':[-50,-400,1150,1200],'visible_bounds':[0,100,600,700]}]}
    motion=page_motion(a,b)[0]
    assert motion['scale_ratio_xy'] == [2,2] and motion['origin_delta_screen_px'] == [-50,-300]
    assert motion['screen_to_screen_candidate'] == [2,0,-50,0,2,-200]
    assert not motion['pdf_mapping_calibrated']
    checks.append('UI original bounds distinguish clipped viewport from page origin/size')
    for patch in ({'document_title':'other'}, {'window_id':'2'}, {'error':'lost'},
                  {'visible_pages':[{'page_id':'q','original_bounds':[0,0,1,1]}]}):
        assert not page_motion(a,dict(b,**patch))
    assert not page_motion(a,dict(b,visible_pages=b['visible_pages']*2))
    checks.append('document, page and window changes or failed/ambiguous observations reject matching')
    report={'passed':True,'data_source':'synthetic_checks','checks':checks,
            'new_device_gesture_validation':False}
    dest=Path(__file__).resolve().parents[1]/'output/tests/ink-motion-report.json'
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__': main()

"""Bounded study view of timed, tool-associated contacts, independent of OCR.

Reads the durable edit ledger rather than rereading a whole raw journal. Contact
time is not thinking time, semantic step time, or proof of saved handwriting.
"""
from html import escape
import json
from pathlib import Path
import sqlite3

from capture_clock_history import atomic_bytes

VERSION=2
CATEGORY_SQL="""CASE
    WHEN kind='pen_contact_pen' THEN 'writing_tool'
    WHEN kind IN ('erase_candidate_mode_unknown','stroke_erase_candidate','point_erase_candidate','region_erase_candidate') THEN 'eraser_tool'
    WHEN kind='lasso_path_candidate' THEN 'lasso_tool'
    WHEN kind='ui_or_outside_page_contact' THEN 'outside_page'
    WHEN kind='undo_gesture_candidate' THEN 'undo_gesture'
    WHEN kind='input_discontinuity' THEN 'boundary'
    ELSE 'unknown_tool' END"""
LABELS={'writing_tool':'书写工具接触','eraser_tool':'橡皮工具接触','lasso_tool':'套索工具接触',
        'outside_page':'页外／界面接触','undo_gesture':'撤回手势候选','boundary':'输入／窗口边界','unknown_tool':'工具未知'}


def category(kind):
    if kind=='pen_contact_pen':return 'writing_tool'
    if kind in ('erase_candidate_mode_unknown','stroke_erase_candidate','point_erase_candidate','region_erase_candidate'):return 'eraser_tool'
    return {'lasso_path_candidate':'lasso_tool','ui_or_outside_page_contact':'outside_page',
            'undo_gesture_candidate':'undo_gesture','input_discontinuity':'boundary'}.get(kind,'unknown_tool')


def fragments(events,gap_us=8_000_000):
    result=[];current=None;previous_end=None
    for e in sorted(events,key=lambda e:(e['start_us'],e['start_seq'])):
        cat=category(e['kind']);page=e.get('page_id');source=e.get('location_evidence',{}).get('source_pdf_sha256')
        key=(cat,page,source,e.get('window_id'),e.get('display_id'))
        if (current is None or current['_key']!=key or previous_end is None or
            e['start_us']-previous_end>gap_us or e['start_us']<previous_end or cat=='boundary'):
            current={'id':f"{e['session_id']}:{e['start_seq']}",'_key':key,'category':cat,
                'page_uuid':page,'source_pdf_sha256':source,'title':e.get('document_title'),
                'pdf_page_1based':e.get('location_evidence',{}).get('pdf_page_1based'),
                'first_seq':e['start_seq'],'last_seq':e['end_seq'],'start_us':e['start_us'],
                'end_us':e['end_us'],'contact_count':0,'observed_contact_us':0,
                'provisional':False,'position_incomplete_contacts':0,'semantic_step_verified':False}
            result.append(current)
        current['contact_count']+=1
        current['last_seq']=max(current['last_seq'],e['end_seq']);current['end_us']=max(current['end_us'],e['end_us'])
        current['observed_contact_us']+=max(0,e['end_us']-e['start_us'])
        current['provisional']|=e.get('provisional',True)
        current['position_incomplete_contacts']+=e.get('location_evidence',{}).get('scope')!='within_bound_page_candidate'
        previous_end=e['end_us']
        if cat=='boundary':current=None
    for f in result:
        del f['_key']
        f['observed_contact_seconds']=f.pop('observed_contact_us')/1e6
        f['fragment_span_seconds']=(f['end_us']-f['start_us'])/1e6
    return result


def snapshot_refs(path,session):
    if not path or not Path(path).exists():return []
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        return [{'before_sha256':a,'after_sha256':b,'basis':'explicit_export_pair_for_session',
                 'live_state_verified':False} for a,b in db.execute(
                     'SELECT before_hash,after_hash FROM comparisons WHERE session=? ORDER BY rowid DESC LIMIT 8',(session,))]
    finally:db.close()


def publish_study(store,final=False,saved_store=None,text_store=None):
    from edit_events import initialize_ledger
    initialize_ledger(store.db)
    # SQLite performs interval union per category. Overlapping contacts are not
    # double-counted; the categories themselves must not be added as total time.
    timings=store.db.execute(f'''WITH contacts AS (
        SELECT {CATEGORY_SQL} AS category,json_extract(payload,'$.start_us') AS start,
               json_extract(payload,'$.end_us') AS end FROM edit_events),
        intervals AS (SELECT *,MAX(end) OVER(PARTITION BY category ORDER BY start,end
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prior_end FROM contacts)
        SELECT category,COUNT(*),SUM(MAX(0,end-MAX(start,COALESCE(prior_end,start))))/1000000.0
        FROM intervals GROUP BY category''').fetchall()
    events=[json.loads(p) for p, in store.db.execute('SELECT payload FROM edit_events ORDER BY rowid DESC LIMIT 512')]
    if any(e.get('session_id')!=store.session for e in events):raise ValueError('Study ledger session mismatch')
    recent=fragments(events)
    from recognition_context import original_text_refs
    source_text=original_text_refs(text_store,[(f['source_pdf_sha256'],f['pdf_page_1based']) for f in recent
        if isinstance(f['source_pdf_sha256'],str) and type(f['pdf_page_1based']) is int])
    report={'schema':'inkbridge-study-evidence-v2','session_id':store.session,
        'timing_basis':'device_action_time_microseconds','finalized':final,
        'totals':{k:{'contacts':n,'observed_contact_seconds':seconds} for k,n,seconds in timings},
        'recent_contact_limit':512,'recent_fragments':recent,
        'saved_snapshots':snapshot_refs(saved_store,store.session),
        'semantic_steps_verified':False,'total_problem_solving_time_known':False,
        'automatic_model_calls':False,'recognition_available':False,'native_live_state_verified':False,
        'original_page_text':source_text,'original_page_text_available':bool(source_text['pages']),
        'handwriting_recognition_available':False,
        'limits':['Tool contact time is not thinking time or semantic solution-step duration.',
                  'Tool observations and page positions remain candidates; missing context stays unknown.',
                  'Recent fragments show at most 512 completed contacts; totals cover the whole ledger.',
                  'Saved snapshots describe exports, not the current live tablet state.']}
    atomic_bytes(store.directory/'study-evidence.json',json.dumps(report,ensure_ascii=False,indent=2).encode())
    context=dict(report);context['recent_fragments']=recent[-50:];context['recent_fragment_limit']=50
    atomic_bytes(store.directory/'assistant-context.json',json.dumps(context,ensure_ascii=False,indent=2).encode())
    cards=''.join('<li>'+escape(LABELS.get(k,k))+f'：{n}次，接触 {seconds:.2f}秒</li>' for k,n,seconds in timings)
    rows=[]
    for f in reversed(recent):
        rows.append('<tr>'+''.join('<td>'+escape(str(x))+'</td>' for x in (
            f['first_seq'],f['last_seq'],LABELS[f['category']],f['contact_count'],
            f"{f['observed_contact_seconds']:.2f}",f"{f['fragment_span_seconds']:.2f}",
            f['page_uuid'] or '未知','临时' if f['provisional'] else '最终候选'))+'</tr>')
    html='''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="5"><title>学习过程证据</title><style>body{font:16px/1.6 system-ui;margin:28px;color:#234}table{border-collapse:collapse}td,th{padding:9px;border-bottom:1px solid #ddd}td:nth-last-child(2){font-size:12px}</style><h1>学习过程证据</h1><p>已区分书写、橡皮、套索和页外接触。接触时长来自设备事件时间；片段按工具、页面和8秒停顿分组，不能当作已识别的解题步骤或思考时间。</p><ul>'''+cards+'</ul><p>以下最多显示最近512次完整接触；汇总覆盖整份编辑账本。已关联保存快照 '+str(len(report['saved_snapshots']))+' 组，快照不是当前平板状态。</p><p><a href="assistant-context.json">精简学习上下文</a> · <a href="study-evidence.json">完整数值证据</a> · <a href="edits.html">编辑候选</a></p><table><tr>'+''.join('<th>'+s+'</th>' for s in ('开始序号','结束序号','工具／操作','接触数','接触秒数','片段跨度秒','页面UUID','状态'))+'</tr>'+''.join(rows)+'</table>'
    atomic_bytes(store.directory/'study.html',html.encode())
    return report

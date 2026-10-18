"""Incremental candidate edit ledger; never mutates raw or saved Notes objects.

Raw tool_type is the physical input device, not the selected Notes tool.
Undo affects an application operation stack, not necessarily the last stroke.
"""
import json
import hashlib
import math

from ink_protocol import PEN_TOOLS
from native_frame_timing import bind

VERSION=7


class EditDetector:
    def __init__(self,state=None):
        self.state=state or {'last':None,'contacts':{},'fingers':None}

    def feed(self,p):
        s=self.state;output=[];t=p['action_time'];seq=p['seq']
        stream=[p.get('session_id'),p['display_id'],p['window_id']]
        last=s['last']
        if last and (seq!=last['seq']+1 or t<last['t'] or stream!=last['stream']):
            s['contacts']={};s['fingers']=None
            output.append({'kind':'input_discontinuity','start_seq':seq,'end_seq':seq,
                           'start_us':t,'end_us':t,'window_id':p['window_id'],'display_id':p['display_id']})
        s['last']={'seq':seq,'t':t,'stream':stream}
        tool=p['tool_type'];key=f"{tool}:{p['pointer_id']}";xy=[p['x'],p['y']]
        if p['action']==0:
            # Finger/palm and pen can both use pointer 0. A palm rejection
            # CANCEL must not discard a concurrent complete pen contact.
            s['contacts'].pop(key,None)
            s['fingers']=None
            return output
        if p['action']==1:
            if key in s['contacts'] or len(s['contacts'])>=16:
                s['contacts']={};s['fingers']=None
            s['contacts'][key]={'start_seq':seq,'end_seq':seq,'start_us':t,'end_us':t,
                'pointer_id':p['pointer_id'],
                'tool_type':tool,'start_xy':xy,'end_xy':xy,'max_distance_px':0.,'path_length_px':0.,
                'bounds':[p['x'],p['y'],p['x'],p['y']],'points':0,
                'window_id':p['window_id'],'display_id':p['display_id']}
        c=s['contacts'].get(key)
        if c is None or c['tool_type']!=tool:
            if s['fingers']:s['fingers']['invalid']=True
            return output
        c['max_distance_px']=max(c['max_distance_px'],math.dist(c['start_xy'],xy))
        c['path_length_px']+=math.dist(c['end_xy'],xy)
        c.update(end_seq=seq,end_us=t,end_xy=xy,points=c['points']+1)
        c['bounds']=[min(c['bounds'][0],p['x']),min(c['bounds'][1],p['y']),
                     max(c['bounds'][2],p['x']),max(c['bounds'][3],p['y'])]
        if tool==0:
            if s['fingers'] is None:
                s['fingers']={'contacts':[],'invalid':False,'max_active':0,'pen_overlap':False}
            e=s['fingers'];active=[v for v in s['contacts'].values() if v['tool_type']==0]
            e['max_active']=max(e['max_active'],len(active))
            e['pen_overlap']|=any(v['tool_type']!=0 for v in s['contacts'].values())
            if len(e['contacts'])+len(active)>2:e['invalid']=True
            if p['action']==3:
                if len(e['contacts'])<3:e['contacts'].append(c.copy())
                if len(active)==1:
                    completed=e['contacts']
                    if (len(completed)==2 and e['max_active']==2 and not e['invalid'] and not e['pen_overlap']):
                        a,b=completed
                        if (abs(a['start_us']-b['start_us'])<=120000 and
                            max(a['start_us'],b['start_us'])<min(a['end_us'],b['end_us']) and
                            max(a['end_us'],b['end_us'])-min(a['start_us'],b['start_us'])<=350000 and
                            max(a['max_distance_px'],b['max_distance_px'])<=18):
                            output.append({'kind':'undo_gesture_candidate','start_seq':min(a['start_seq'],b['start_seq']),
                                'end_seq':seq,'start_us':min(a['start_us'],b['start_us']),'end_us':t,
                                'window_id':p['window_id'],'display_id':p['display_id'],
                                'gesture':'two_finger_simultaneous_tap','max_motion_px':max(a['max_distance_px'],b['max_distance_px']),
                                'mapping_evidence':'user_declared_two_finger_tap_means_undo'})
                    s['fingers']=None
        elif s['fingers']:
            s['fingers']['pen_overlap']=True
        if p['action']==3:
            if tool in PEN_TOOLS or tool==2:output.append(dict(c,kind='physical_pen_contact' if tool in PEN_TOOLS else 'physical_eraser_contact'))
            s['contacts'].pop(key,None)
        return output


def tool_for_contact(store,event,state):
    """Two surrounding sampled states are evidence for a candidate only.

    UI reads surround frame exposure non-atomically; no assertion that a fast
    tool switch between samples was observed. Long paths stay unknown.
    """
    if event['end_us']-event['start_us']>20_000_000:return None
    frames=store.frames_for(event['start_us']-3_000_000,event['end_us']+3_000_000)
    if not frames:return None
    frames,calibration=bind(frames,state)
    if not calibration.get('usable_candidate'):return None
    before=[];after=[];observed=[]
    for frame in frames:
        o=frame.get('observation',{});context=o.get('context') or {};initial=o.get('context_before') or {}
        window=o.get('window',{});native=o.get('native_frame',{})
        t=native.get('timestamp_raw',0)/1000
        edit=context.get('editing',{});first_edit=initial.get('editing',{})
        if (not o.get('valid') or not o.get('timestamp_used_for_coordinates') or not window.get('focused') or
            window.get('window_id')!=event['window_id'] or window.get('display_id')!=event['display_id'] or
            not context.get('active_tab_marker_unique') or not edit or edit!=first_edit):continue
        ids=[p['page_id'] for p in context.get('page_layers',[])]
        if len(ids)!=1:continue
        item={'t':t,'title':context['document_title_candidate'],'page_id':ids[0],
              'tool':edit.get('selected_tool'),
              'eraser_mode':edit.get('eraser_mode') or edit.get('eraser_mode_style_candidate'),
              'eraser_mode_evidence':'explicit_selected' if edit.get('eraser_mode') else edit.get('eraser_mode_style_evidence'),
              'lasso_menu_available_actions':edit.get('lasso_menu_available_actions',[]),
              'frame_index':frame['index'],
              'page_bounds':context['page_layers'][0].get('screen_bounds')}
        observed.append(item)
        if event['start_us']-3_000_000<=t<=event['start_us']:before.append(item)
        if event['end_us']<=t<=event['end_us']+3_000_000:after.append(item)
    if not before or not after:return None
    a=max(before,key=lambda p:p['t']);b=min(after,key=lambda p:p['t'])
    if any(a[k]!=b[k] for k in ('title','page_id','tool','eraser_mode')):return None
    if any(any(item[k]!=a[k] for k in ('title','page_id','tool','eraser_mode'))
           for item in observed if a['t']<=item['t']<=b['t']):return None
    within=None
    box=event.get('bounds')
    if box and a['page_bounds'] and b['page_bounds']:
        within=all(r[0]<=box[0]<=box[2]<=r[2] and r[1]<=box[1]<=box[3]<=r[3]
                   for r in (a['page_bounds'],b['page_bounds']))
    return dict(a,frame_indices=[a['frame_index'],b['frame_index']],
                contact_within_sampled_page_bounds=within,
                evidence='matching_surrounding_ui_samples_not_atomic_edit_subscription')


def classify(event,context=None):
    result=dict(event,status='candidate',native_edit_result_verified=False,
                affected_stroke_ids=None,applied_to_document=False)
    if context:
        result['context']=context
        result['page_id']=context['page_id'];result['document_title']=context['title']
    tool=context.get('tool') if context else None
    if context and context.get('contact_within_sampled_page_bounds') is False:
        result['kind']='ui_or_outside_page_contact'
    elif event['kind']=='physical_eraser_contact' or (event['kind']=='physical_pen_contact' and tool=='eraser'):
        mode=context.get('eraser_mode') if context else None
        result.update(kind={'stroke':'stroke_erase_candidate','point':'point_erase_candidate','region':'region_erase_candidate'}.get(mode,'erase_candidate_mode_unknown'),
                      eraser_mode=mode,user_usual_eraser_mode='stroke',
                      usual_mode_is_not_observed_mode=True)
    elif event['kind']=='physical_pen_contact' and tool=='lasso':
        b=event['bounds'];closed=math.dist(event['start_xy'],event['end_xy'])<=25
        result.update(kind='lasso_path_candidate',
            path_shape='closed_loop' if closed and min(b[2]-b[0],b[3]-b[1])>=20 else 'open_path_or_drag',
            possible_operations=['select','move_selection','resize_selection'],
            selected_objects_known=False,page_translation=None)
    elif event['kind']=='physical_pen_contact':
        result['kind']='pen_contact_tool_unknown' if tool is None else 'pen_contact_'+tool
    return result


def initialize_ledger(db):
    db.execute('CREATE TABLE IF NOT EXISTS edit_events(id TEXT PRIMARY KEY,kind TEXT,payload TEXT NOT NULL)')


def process_edits(store,state,final=False):
    db=store.db
    initialize_ledger(db)
    assessment,_=store.verify_clock(state,final)
    final_key=hashlib.sha256(json.dumps([state,store.get('final_clock')],sort_keys=True).encode()).hexdigest() if final else None
    with db:
        if store.get('edit_detector_version')!=VERSION or (final and store.get('edits_final_clock')!=final_key):
            db.execute('DELETE FROM edit_events');store.set('edits_cursor',0);store.set('edit_detector',None)
            store.set('edit_detector_version',VERSION);store.set('edits_final_clock',final_key)
    limit_time=float('inf') if final else assessment.get('action_time_range_us',[-1,-1])[1]-7_000_000
    rows=db.execute('SELECT seq,start,end FROM raw_index WHERE seq>? AND t<=? ORDER BY seq LIMIT 2048',
                    (store.get('edits_cursor',0),limit_time)).fetchall()
    if not rows:
        return final
    detector=EditDetector(store.get('edit_detector'))
    with store.raw.open('rb') as source,db:
        for seq,start,end in rows:
            source.seek(start);point=json.loads(source.read(end-start))
            for event in detector.feed(point):
                from edit_observations import contact_context
                independent,context=contact_context(store,event,assessment)
                if not independent and assessment.get('usable_candidate'):
                    context=tool_for_contact(store,event,state)
                result=classify(event,context)
                if final and event.get('tool_type') in PEN_TOOLS:
                    from edit_spatial_evidence import contact_location
                    result['location_evidence']=contact_location(store,result)
                    if (context and not context.get('page_id') and
                        result['location_evidence']['scope']=='within_bound_page_candidate'):
                        result['page_id']=result['location_evidence']['page_id']
                        result['page_assignment']='final_coordinate_candidate_within_sampled_page_set'
                    if result['location_evidence']['scope']=='outside_bound_page':
                        result['tool_contact_kind']=result['kind']
                        result['kind']='ui_or_outside_page_contact'
                result.update(session_id=store.session,provisional=not final)
                identity=f"{store.session}:{event['start_seq']}:{event['end_seq']}:{event['kind']}"
                db.execute('INSERT OR REPLACE INTO edit_events VALUES (?,?,?)',(identity,result['kind'],json.dumps(result)))
        store.set('edits_cursor',rows[-1][0]);store.set('edit_detector',detector.state)
    return False


def publish_edits(store,final=False):
    from capture_clock_history import atomic_bytes
    from html import escape
    summary=edit_summary(store)
    atomic_bytes(store.directory/'edit-summary.json',json.dumps(summary,ensure_ascii=False,indent=2).encode())
    rows=[]
    labels={'undo_gesture_candidate':'双指撤回候选','input_discontinuity':'输入或窗口边界',
            'stroke_erase_candidate':'笔画擦除候选','point_erase_candidate':'点擦除候选',
            'region_erase_candidate':'圈选擦除候选','erase_candidate_mode_unknown':'擦除候选（模式未知）',
            'lasso_path_candidate':'套索轨迹候选','pen_contact_tool_unknown':'笔接触（工具未知）',
            'ui_or_outside_page_contact':'界面或页外接触','pen_contact_pen':'书写工具接触'}
    for kind,payload in store.db.execute('SELECT kind,payload FROM edit_events ORDER BY rowid DESC LIMIT 100'):
        event=json.loads(payload)
        hint=event.get('context',{}).get('eraser_mode_bracket_hint')
        mode_names={'stroke':'笔画','point':'点','region':'圈选',None:'未知'}
        hint_text=(mode_names.get(hint['mode'],'未知')+'擦除（前后样本线索，未确认）') if hint else '无'
        scope=event.get('location_evidence',{}).get('scope')
        location={'outside_bound_page':'整段在所选页面外',
                  'within_bound_page_candidate':'整段页面坐标候选',
                  'partial_or_unknown':'部分或全部坐标未知'}.get(scope,'未知／待最终定位')
        rows.append('<tr>'+''.join('<td>'+escape(str(v))+'</td>' for v in
            (event['start_seq'],event['end_seq'],labels.get(kind,kind),
             mode_names.get(event.get('eraser_mode'),'未知'),hint_text,location,
             event.get('page_id','未知')))+'<td>未确认</td></tr>')
    html='<meta charset="utf-8"><meta http-equiv="refresh" content="3"><title>编辑操作候选</title><style>body{font:16px system-ui;margin:32px}td,th{padding:10px;border-bottom:1px solid #ccc}</style><h1>编辑操作候选</h1><p>原始笔接触不一定是书写。以下区分双指撤回手势、橡皮和套索轨迹；选中对象、实际删除或移动结果尚未确认，未自动修改笔记或原始记录。</p><p>只显示最近100项；完整记录保存于本会话数据库。撤回可能撤销擦除或移动，不能直接当作删除上一笔。</p><table><tr><th>起始序号</th><th>结束序号</th><th>候选类型</th><th>橡皮模式</th><th>页面</th><th>最终效果</th></tr>'+''.join(rows)+'</table>'
    html=html.replace('<th>橡皮模式</th>','<th>橡皮模式</th><th>模式线索（不用于擦除）</th><th>页面位置证据</th>')
    atomic_bytes(store.directory/'edits.html',html.encode())
    if final:
        import os
        path=store.directory/'edit-events.jsonl';temporary=path.with_suffix('.jsonl.tmp')
        with temporary.open('w',encoding='utf-8',newline='\n') as out:
            for payload, in store.db.execute('SELECT payload FROM edit_events ORDER BY rowid'):
                out.write(payload+'\n')
            out.flush();os.fsync(out.fileno())
        from atomic_output import replace_file
        replace_file(temporary,path)
    return summary


def edit_summary(store):
    # A newly recovered stopped session may still be finishing coordinates,
    # before the first edit batch has been allowed to run.
    initialize_ledger(store.db)
    rows=store.db.execute('SELECT kind,COUNT(*) FROM edit_events GROUP BY kind').fetchall()
    # Aggregate inside SQLite: do not materialize a day of contact payloads.
    scopes=dict(store.db.execute("SELECT json_extract(payload,'$.location_evidence.scope') AS scope,COUNT(*) FROM edit_events GROUP BY scope HAVING scope IS NOT NULL"))
    hints=dict(store.db.execute("SELECT json_extract(payload,'$.context.eraser_mode_bracket_hint.mode') AS mode,COUNT(*) FROM edit_events GROUP BY mode HAVING mode IS NOT NULL"))
    return {'schema':VERSION,'counts':dict(rows),'processed_through_seq':store.get('edits_cursor',0),
            'final_contact_location_counts':scopes,'mode_hint_counts_not_confirmed':hints,
            'tool_observation_rows':store.db.execute('SELECT COALESCE(MAX(id),0) FROM edit_observations').fetchone()[0],
            'raw_unchanged':True,'native_document_edits_applied':False,
            'limits':['Input gestures and tool paths are candidates, not confirmed saved edits.',
                      'Undo may reverse erase/move/delete rather than the last stroke.',
                      'Stroke/point erasure needs observed mode; lasso move/delete needs selected object identities.']}

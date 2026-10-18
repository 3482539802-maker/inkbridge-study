"""Bounded provisional per-stroke page coordinates from a durable raw prefix.

Copies only a temporary complete prefix, deletes it immediately after analysis,
and atomically replaces numeric derived output. Never modifies canonical raw.
"""
from collections import Counter
from html import escape
import json
from pathlib import Path
import tempfile

from coordinate_alignment import align
from ink_protocol import PEN_TOOLS,validate_point


def render_report(strokes,locations,finalized=False):
    pages={}
    for p in locations.values():
        if p.get('pdf_xy') is not None:
            key=(p['source_pdf_sha256'],p['pdf_page_1based'],p['page_uuid'])
            pages.setdefault(key,[]).append(p)
    figures=[]
    for (_,number,uuid),points in sorted(pages.items()):
        size=points[0].get('pdf_size_pt') or [612,792]
        paths=[]
        for stroke in strokes:
            # Break at every unresolved/other-page/missing event. Never join
            # unknown sections into apparently known continuous handwriting.
            run=[]
            def flush():
                if not run:return
                xy=' '.join(f'{p[0]:.3f},{p[1]:.3f}' for p in run)
                paths.append(f'<polyline points="{xy}" fill="none" stroke="{color}" stroke-width="1.2"/>')
                if len(run)==1:paths.append(f'<circle cx="{run[0][0]}" cy="{run[0][1]}" r="1" fill="{color}"/>')
                run.clear()
            color='#1765a5';previous=None
            for seq in range(stroke['seq_range'][0],stroke['seq_range'][1]+1):
                p=locations.get(seq)
                if (not p or p.get('page_uuid')!=uuid or p.get('pdf_xy') is None or
                    any(p.get(k)!=stroke[k] for k in ('display_id','window_id','pointer_id'))):
                    flush();previous=None;continue
                new_color=('#8750b5' if p['status']=='motion_candidate' else
                           '#bd7416' if p['status']=='extrapolated_candidate' else '#1765a5')
                if previous is not None and (seq!=previous+1 or new_color!=color):flush()
                color=new_color;run.append(p['pdf_xy']);previous=seq
            flush()
        figures.append(f'<figure><figcaption>PDF 第 {number} 页 · {escape(uuid)}</figcaption>'
            f'<svg viewBox="0 0 {size[0]} {size[1]}" role="img" aria-label="页面坐标轨迹">'
            f'<rect width="{size[0]}" height="{size[1]}" fill="white" stroke="#bbc6d0"/>'+''.join(paths)+'</svg></figure>')
    table=[]
    for s in strokes:
        page=s.get('pdf_page_1based','未知')
        bounds=', '.join(f'{v:.2f}' for v in s.get('pdf_bounds',[])) or '未知'
        state='完整候选' if s['location_complete'] else ('部分／证据不足' if finalized else '部分／待后帧')
        if s['status_counts'].get('extrapolated_candidate'):state+=' · 含页边外推'
        if s['status_counts'].get('motion_candidate'):state+=' · 含运动估计'
        rejections=s.get('rejection_counts',{})
        if rejections.get('outside_bound_page')==s['raw_points']:
            state='已确认页面范围之外（无页面坐标）'
        elif finalized and rejections.get('no_bracketing_geometry'):
            state+=' · 缺少前后画面'
        table.append(f'<tr><td>{s["stroke_index"]+1}</td><td>{page}</td><td>{s["located_points"]}/{s["raw_points"]}</td>'
            f'<td>{bounds}</td><td>{s["pen_down_duration_ms"]:.1f} ms</td><td>{state}</td></tr>')
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="refresh" content="4">
<title>逐笔页面坐标</title><style>body{font:16px system-ui;background:#eef2f6;color:#183048;margin:32px}main{max-width:1150px;margin:auto}table{width:100%;border-collapse:collapse;background:white}td,th{padding:12px;text-align:left;border-bottom:1px solid #dce3ea}section{display:flex;flex-wrap:wrap;gap:20px}figure{width:430px;margin:20px 0}svg{width:100%;max-height:650px}figcaption{font-size:13px;overflow-wrap:anywhere;margin:10px 0}p{line-height:1.6}</style><main>
<h1>逐笔页面坐标</h1><p>蓝色：特征覆盖范围内的坐标候选；橙色：页边外推候选；紫色：相邻画面间的运动估计坐标。空白部分继续未知。图示只显示轨迹在页面上的位置，坐标单位为 PDF pt，不显示原题背景。下表边界依次为左、上、右、下。</p>
<p>有前后画面证据后才更新坐标，因此会延迟出现。候选尚未全部验证为保存笔迹真值；落笔时长不等于思考或解题时长。</p>
<table><thead><tr><th>笔画</th><th>PDF页</th><th>已定位点</th><th>页面边界（pt）</th><th>落笔时长</th><th>状态</th></tr></thead><tbody>'''+''.join(table)+'</tbody></table><section>'+''.join(figures)+'</section></main></html>'
    if finalized:
        html=html.replace('<meta http-equiv="refresh" content="4">','').replace('有前后画面证据后才更新坐标，因此会延迟出现。','本次观察已结束；未知部分缺少同期证据，不会用之后的画面补写。')
    return html


def strokes_from_raw(raw,locations):
    rows=[]
    active={}
    for line in raw.splitlines():
        p=validate_point(json.loads(line))
        if p['tool_type'] not in PEN_TOOLS:continue
        key=(p['display_id'],p['window_id'],p['pointer_id'])
        if p['action']==1 or key not in active:
            row={'stroke_index':len(rows),'session_id':p['session_id'],
                 'window_id':p['window_id'],'display_id':p['display_id'],'pointer_id':p['pointer_id'],
                 'start_action_time_us':p['action_time'],'down_observed':p['action']==1,'points':[]}
            rows.append(row);active[key]=row
        row=active[key]
        row['points'].append(p)
        if p['action'] in (0,3):active.pop(key,None)
    for row in rows:
        points=row.pop('points')
        selected=[locations[p['seq']] for p in points if p['seq'] in locations]
        found=[p for p in selected if p.get('pdf_xy') is not None]
        pages={(p['source_pdf_sha256'],p['pdf_page_1based'],p['page_uuid']) for p in found}
        row.update(seq_range=[points[0]['seq'],points[-1]['seq']],raw_points=len(points),
                   end_action_time_us=points[-1]['action_time'],
                   pen_down_duration_ms=(points[-1]['action_time']-points[0]['action_time'])/1000,
                   ended=points[-1]['action'] in (0,3),cancelled=points[-1]['action']==0,
                   located_points=len(found),unresolved_points=len(selected)-len(found),
                   status_counts=dict(Counter(p['status'] for p in selected)),
                   rejection_counts=dict(Counter(p['reason'] for p in selected if p.get('reason'))),
                   location_complete=len(found)==len(points) and row['down_observed'] and points[-1]['action']==3,
                   all_positions_are_candidates=True,model_reasoning_duration_measured=False)
        if found and len(pages)==1:
            digest,page,uuid=pages.pop()
            row.update(source_pdf_sha256=digest,pdf_page_1based=page,page_uuid=uuid,
                       pdf_bounds=[min(p['pdf_xy'][0] for p in found),min(p['pdf_xy'][1] for p in found),
                                   max(p['pdf_xy'][0] for p in found),max(p['pdf_xy'][1] for p in found)])
        elif len(pages)>1:
            row.update(location_complete=False,page_identity_conflict=True)
    return rows


def update(raw,sidecar,frames,output,stable_runs=False,allow_page_margins=False,finalized=False):
    raw,output=Path(raw),Path(output)
    if raw.stat().st_size>64*1024*1024:raise ValueError('Live prefix exceeds 64 MiB analysis bound')
    payload=raw.read_bytes()
    if len(payload)>64*1024*1024:raise ValueError('Live prefix grew beyond bound')
    payload=payload[:payload.rfind(b'\n')+1]
    output.mkdir(parents=True,exist_ok=True)
    # align's unchanged-raw check applies to this immutable prefix. The canonical
    # journal continues receiving new events, which belong to a later revision.
    with tempfile.TemporaryDirectory(prefix='coordinate-prefix-',dir=output) as temporary:
        prefix=Path(temporary)/'raw.jsonl'
        prefix.write_bytes(payload)
        target=Path(temporary)/'points.jsonl'
        report=align(prefix,sidecar,frames,target,stable_runs=stable_runs,allow_page_margins=allow_page_margins)
        locations={p['seq']:p for line in target.read_text(encoding='utf-8').splitlines() if (p:=json.loads(line))}
        strokes=strokes_from_raw(payload,locations)
        report.update(raw_prefix_bytes=len(payload),provisional_live_revision=True,
                      stroke_count=len(strokes),complete_located_strokes=sum(s['location_complete'] for s in strokes),
                      pending_future_frame_is_not_a_failure=not finalized,observation_finalized=finalized,tablet_ui_actions=[])
        # Replace numeric outputs only; readers use the summary written last.
        target.replace(output/'points.jsonl')
        for name,value in [('strokes.json',{'strokes':strokes,'raw_prefix_bytes':len(payload)}),('summary.json',report)]:
            scratch=output/(name+'.tmp')
            scratch.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
            scratch.replace(output/name)
        html=output/'report.html.tmp'
        html.write_text(render_report(strokes,locations,finalized=finalized),encoding='utf-8')
        html.replace(output/'report.html')
    return report

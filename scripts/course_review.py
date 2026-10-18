"""Human-readable local tutoring packets; no cloud, tablet frames or UI controls."""
from html import escape
import json
from pathlib import Path
from urllib.parse import quote

import fitz

from atomic_output import atomic_bytes
from course_learning import verified_package
from course_workbook import sha256
from recognition_display import recognition_html


STYLE = '''<style>body{font:16px/1.7 system-ui;max-width:1080px;margin:30px auto;padding:0 20px;background:#f4f6fa;color:#243249}section{background:white;border-radius:12px;padding:24px;margin:20px 0}img{max-width:100%;height:auto}table{width:100%;border-collapse:collapse}td,th{padding:9px;text-align:left;border-bottom:1px solid #ddd}button{font:inherit;border:0;background:#225ca1;color:white;padding:10px 18px;border-radius:7px;cursor:pointer}code,pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#225ca1}.muted{color:#63718a}</style>'''


def publish_request(request_path):
    path = Path(request_path); request = json.loads(path.read_bytes())
    page = path.with_suffix('.html')
    if page.exists(): return page
    _, manifest = verified_package(request['source']['binding_path'])
    if sha256(manifest['source_pdf']) != request['source']['original_pdf_sha256']:
        raise ValueError('Original source identity changed')
    pictures = []
    with fitz.open(manifest['source_pdf']) as document:
        for index, part in enumerate(request['question']['parts']):
            target = path.parent/(request['id']+f'.question-{index}.png')
            document[part['page']-1].get_pixmap(matrix=fitz.Matrix(1.6,1.6),clip=fitz.Rect(part['box'])).save(target)
            pictures.append(target.name)
    activity = request['activity']; rows = []
    for i, step in enumerate(activity['step_candidates'], 1):
        rows.append('<tr>'+''.join('<td>'+escape(str(v))+'</td>' for v in
                    (i, round(step['observed_writing_seconds'], 2), round(step['fragment_span_seconds'], 2),
                     round(step['unclassified_interval_seconds'], 2), step['contact_count']))+'</tr>')
    source_html = ''.join('<img src="'+escape(name,quote=True)+'" alt="原题">' for name in pictures)
    ink = []
    for asset in request.get('answer_images', []):
        image = Path(asset['path'])
        if image.parent != path.parent or sha256(image) != asset['sha256']: raise ValueError('Ink asset identity mismatch')
        ink.append('<img src="'+escape(image.name,quote=True)+'" alt="原始笔迹候选">')
    candidates = recognition_html(request.get('recognition_candidates', []))
    incoming = path.parent/'incoming'/(request['id']+'.json')
    prompt = (f'请读取本机辅导证据包 {path.resolve()} 及其引用的题目和笔迹图，为这门课程提供个性化辅导。'
              '区分实际书写时长、停顿和未确认的语义步骤；按可读答案分析，识别不清就保留未知，给渐进提示。'
              '用户需要完整知识库条目：另在 reference_solution 提供原题的完整参考推导、答案和易错点，不能当成用户作答。'
              f'按证据包 response_schema 保存 JSON 到 {incoming.resolve()}，并包含 request_id={request["id"]}、'
              f'evidence_hash={request["evidence_hash"]}。观察须引用已有 evidence_ids，不改原始笔迹。')
    html = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>按题辅导证据</title>'+STYLE
    library = '../knowledge/'+quote(request['course']['id'])+'/index.html#question='+quote(request['question']['id'])
    html += '<p><a href="'+escape(library,quote=True)+'">打开完整知识库条目：原题 · 笔迹 · 建议 · 解答 · 复练</a> · <a href="/">学习工作台</a></p><h1>'+escape(request['course']['title'])+' · '+escape(activity['label'])+'</h1>'
    html += '<section><h2>原题</h2>'+source_html+'</section><section><h2>本次过程</h2>'
    html += '<p>实际书写接触合计 '+str(round(activity['observed_writing_seconds'],2))+' 秒。'+('记录仍在进行，以下是过程候选。' if activity['provisional'] else '这份记录已处理。')+'</p>'
    html += '<table><tr><th>片段候选</th><th>实际书写秒</th><th>片段跨度秒</th><th>未分类间隔秒</th><th>笔接触</th></tr>'+''.join(rows)+'</table><p class="muted">片段不等于已确认的解题步骤；间隔可能包含阅读、思考、休息或离开。</p></section>'
    html += '<section><h2>笔迹与识别候选</h2><p>原始接触轨迹，尚未完整应用撤回、擦除和套索，不能当作最终保存答案。</p>'
    html += ''.join(ink) if ink else '<p>尚无合格的页面笔迹图；缺少证据不会自动判错。</p>'
    if candidates: html += candidates+'<p class="muted">识别保留文字、图形与标注；候选不是最终确认答案。</p>'
    review = request.get('chat_recognition_review')
    if review:
        packet = Path(review['path'])
        if packet.resolve().parent.parent != path.resolve().parent or sha256(packet) != review['sha256']:
            raise ValueError('Chat recognition packet identity mismatch')
        html += '<p><a href="'+escape(packet.parent.name+'/review.html',quote=True)+'">查看待聊天核图包</a></p>'
    html += '</section><section><h2>交给当前聊天辅导</h2><button id="copy">复制辅导请求</button><p id="copied"></p><details><summary>查看请求文字</summary><pre>'+escape(prompt)+'</pre></details><p><a href="'+escape(path.name)+'">查看完整来源证据 JSON</a></p></section>'
    html += '<script>document.getElementById("copy").onclick=async()=>{try{await navigator.clipboard.writeText('+json.dumps(prompt,ensure_ascii=False)+');document.getElementById("copied").textContent="已复制；发送到当前聊天即可。"}catch(e){document.getElementById("copied").textContent="请展开下方文字手动复制。"}}</script></html>'
    atomic_bytes(page, html.encode())
    return page


def publish_feedback(path):
    path = Path(path); result = json.loads(path.read_bytes()); response = result['response']
    html = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>学习反馈</title>'+STYLE
    library = '../knowledge/'+quote(result['course']['id'])+'/index.html#question='+quote(result['question_id'])
    html += '<p><a href="'+escape(library,quote=True)+'">打开完整知识库条目：原题 · 笔迹 · 建议 · 解答 · 复练</a> · <a href="/">学习工作台</a></p><h1>'+escape(result['course']['title'])+' · '+escape(result['question_id'])+'</h1>'
    for title,key in [('观察','observations'),('下一步提示','hints'),('后续练习','next_practice'),('需要确认','needs_clarification')]:
        values=response.get(key,[])
        if values:
            html += '<section><h2>'+title+'</h2><ul>'+''.join('<li>'+escape(v['text'] if isinstance(v,dict) else v)+'</li>' for v in values)+'</ul></section>'
    if result.get('step_timings'):
        html += '<section><h2>按解题步骤整理的计时</h2><p>步骤归类来自辅导判断，书写秒数由引用的设备事件重新计算。</p><ul>'
        html += ''.join('<li>'+escape(s['label'])+'：实际书写 '+str(round(s['observed_writing_seconds'],2))+' 秒</li>' for s in result['step_timings'])+'</ul></section>'
    html += '<p class="muted">反馈来自当前聊天或配置模型；观察保留来源，模型判断仍需实际答案佐证。</p></html>'
    atomic_bytes(path.with_suffix('.html'),html.encode())
    return path.with_suffix('.html')

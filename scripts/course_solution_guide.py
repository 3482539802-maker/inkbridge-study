"""Build a computer-only worked-solution guide, separate from Notes bindings.

Input solutions are authored/reviewed mathematical content, not model truth.
Requires latex2mathml for offline browser-native formulas. No network/tablet I/O.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path

import fitz

from course_workbook import save, sha256, validate


def build_guide(plan_path, solutions_path, output):
    from latex2mathml.converter import convert
    plan_path, solutions_path, output = (Path(p).resolve() for p in (plan_path, solutions_path, output))
    plan = json.loads(plan_path.read_bytes())
    validate(plan, plan['source_pdf'])
    solutions = json.loads(solutions_path.read_bytes())
    if solutions.get('schema') != 'inkbridge-worked-solutions-v1':
        raise ValueError('Unsupported worked-solutions schema')
    if solutions.get('source_pdf_sha256') != plan['source_pdf_sha256']:
        raise ValueError('Solutions source hash mismatch')
    ids = [q['id'] for q in plan['questions']]
    guide_title = escape(solutions.get('title', plan['course']['title']))
    guide_header = escape(solutions.get('header', plan['course']['id']))
    introduction = escape(solutions.get('introduction', f'共 {len(ids)} 题。每题保留原题截图，并给出入手理由、完整推导、结果与检查。'))
    review_note = escape(solutions.get('review_note', '按原卷逐题核对，并补充中间推导和适用条件；本册是解题参考，不是对用户笔迹或掌握程度的评判。'))
    entries = solutions.get('questions', [])
    if [q['id'] for q in entries] != ids:
        raise ValueError('Solutions must cover every question exactly once, in plan order')
    if plan.get('review') != 'confirmed' or any(q.get('review') != 'confirmed' for q in plan['questions']):
        raise ValueError('Question cuts must be reviewed first')
    if output.exists():
        raise FileExistsError('Use a new immutable guide directory')

    def blocks(items):
        rendered = []
        for item in items:
            if set(item) == {'text'}:
                rendered.append('<p>'+escape(item['text'])+'</p>')
            elif set(item) == {'math'}:
                rendered.append('<div class="formula">'+convert(item['math'], display='block')+'</div>')
            else:
                raise ValueError('Guide blocks must contain text or math')
        return ''.join(rendered)

    # Validate/formula-convert all authored content before creating the package.
    sections = []
    for q in entries:
        if not q.get('approach') or not q.get('steps') or not q.get('pitfalls') or not q.get('result'):
            raise ValueError('Each solution needs approach, detailed steps, result and pitfalls')
        sections.append((blocks(q['approach']), [(escape(s['title']), blocks(s['blocks'])) for s in q['steps']],
                         blocks(q['result']), blocks(q['pitfalls'])))
    output.mkdir(parents=True)
    cards, images = [], []
    with fitz.open(plan['source_pdf']) as source:
        for i, (question, entry, rendered) in enumerate(zip(plan['questions'], entries, sections), 1):
            prompts = []
            for j, part in enumerate(question['parts'], 1):
                filename = f'{question["id"]}-prompt-{j}.png'
                source[part['page']-1].get_pixmap(matrix=fitz.Matrix(2, 2), clip=fitz.Rect(part['box']), alpha=False).save(output/filename)
                prompts.append(f'<img class="prompt" src="{filename}" alt="{escape(question["id"])} 原题区域">')
                images.append({'question_id': question['id'], 'path': filename, 'sha256': sha256(output/filename),
                               'source_page_1based': part['page'], 'source_box_pdf_pt': part['box']})
            approach, steps, result, pitfalls = rendered
            chunks = [f'<section class="sheet" id="{question["id"]}"><header>{guide_header} · 电脑端详解 <span>{i:02d} / {len(ids)}</span></header>',
                      f'<h1>{escape(question["id"])} · {escape(entry["title"])}</h1>', ''.join(prompts),
                      '<h2>怎么想到</h2>'+approach, '<h2>详细步骤</h2>']
            for n, (title, body) in enumerate(steps, 1):
                if entry['steps'][n-1].get('new_page'):
                    chunks.append(f'</section><section class="sheet"><header>{escape(question["id"])} · 证明续页</header><h1>继续推导</h1>')
                chunks.append(f'<h3>{n}. {title}</h3>'+body)
            chunks.extend(['<div class="result"><h2>结果</h2>'+result+'</div>', '<h2>易错点与检查</h2>'+pitfalls, '</section>'])
            cards.append(''.join(chunks))
    nav = ''.join(f'<a href="#{escape(q["id"])}">{escape(q["id"])}</a>' for q in entries)
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>'''+guide_title+''' · 思路与详解（电脑端）</title>
<style>
@page{size:A4;margin:13mm 16mm}*{box-sizing:border-box}body{margin:0;background:#e9eef2;color:#202e3c;font:14px/1.6 "Microsoft YaHei",sans-serif}
nav{position:sticky;top:0;padding:12px;background:#142f46;color:white;display:flex;gap:18px;flex-wrap:wrap}nav a{color:white}
.sheet{width:210mm;margin:22px auto;background:white;padding:14mm 16mm;box-shadow:0 5px 25px #10203015}
header{color:#456279;font-size:11px;border-bottom:1px solid #c6d5df;padding-bottom:6px;letter-spacing:1px}header span{float:right}
h1{font-size:24px;line-height:1.35;margin:14px 0}h2{font-size:15px;color:#12616c;margin:13px 0 4px}h3{font-size:14px;margin:10px 0 3px}
p{margin:4px 0 7px;text-align:justify}math{font-family:"Cambria Math",serif;font-size:17px}.formula{margin:6px 0;overflow-x:auto;padding:3px 0}
.prompt{display:block;max-width:100%;max-height:135px;width:auto;margin:8px auto 12px}.result{background:#edf6f5;border-left:3px solid #287c80;padding:6px 12px;margin-top:12px}
.muted{color:#526979;font-size:12px}.cover{padding-top:30mm}.cover h1{font-size:36px}.cover p{font-size:16px;margin:20px 0}.cover code{font-size:11px;word-break:break-all}
@media(max-width:800px){.sheet{width:100%;padding:22px;margin:12px 0}.formula{max-width:100%}}
@media print{body{background:white;font-size:12px;line-height:1.5}nav{display:none}.sheet{width:auto;margin:0;padding:0;box-shadow:none;break-before:page}.sheet:first-of-type{break-before:auto}h1{font-size:22px}h2{font-size:14px}h3{font-size:12px}math{font-size:15px}.formula{overflow:visible;margin:5px 0}.prompt{max-height:180px}p{margin:3px 0 6px}.result{break-inside:avoid}h2,h3{break-after:avoid}.cover h1{font-size:32px}.cover{padding-top:25mm}}
</style><nav>题目导航 '''+nav+'''</nav><section class="sheet cover"><header>解题参考 · 独立电脑副本</header>
<h1>'''+guide_title+'''<br>思路与详细解答</h1><p>'''+introduction+'''</p>
<p>本册只保存在电脑，含完整答案。平板作答与 Notes 绑定继续使用现有无答案作答本。</p>
<p class="muted">'''+review_note+'''</p>
<p class="muted">来源 SHA256<br><code>'''+escape(plan['source_pdf_sha256'])+'''</code></p></section>'''+''.join(cards)+'</html>'
    (output/'guide.html').write_text(html, encoding='utf-8')
    save(output/'guide-manifest.json', {
        'schema': 'inkbridge-solution-guide-v1', 'artifact_role': 'worked_solutions',
        'delivery_scope': 'computer_only', 'answer_content_included': True,
        'notes_binding_eligible': False, 'tablet_ui_actions': [],
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'source_pdf_sha256': plan['source_pdf_sha256'], 'plan_path': str(plan_path),
        'plan_sha256': sha256(plan_path), 'solutions_path': str(solutions_path),
        'solutions_sha256': sha256(solutions_path), 'question_ids': ids,
        'html_sha256': sha256(output/'guide.html'), 'prompt_images': images,
    })
    return output/'guide.html'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--solutions', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(build_guide(args.plan, args.solutions, args.output))


if __name__ == '__main__':
    main()

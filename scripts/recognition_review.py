"""Build a local, source-checked review set; evaluate explicit human labels only.

No model invocation, tablet access, external assets or annotation inference.
The images are reconstructions of historical raw contacts, never screenshots.
"""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import re

from recognition_ink_input import render_contacts,render_screen_contacts

KINDS={'complete_formula','partial_formula','diagram','annotation','text','mixed','uncertain'}


def digest(path):
    with Path(path).open('rb') as source:return hashlib.file_digest(source,'sha256').hexdigest()


def read_json(path,budget=8*1024*1024):
    with Path(path).open('rb') as source:payload=source.read(budget+1)
    if len(payload)>budget:raise ValueError('Review input exceeds bounded size')
    return json.loads(payload)


def build(directory,raw,report_paths):
    from PIL import Image
    directory,raw=Path(directory),Path(raw)
    reports=[read_json(p) for p in report_paths]
    if not 1<=len(reports)<=3:raise ValueError('Use one to three explicit local model reports')
    job=read_json(directory/'job.json')
    progress=read_json(directory/'processing.json')
    session=job['session_id']
    if not job.get('geometry_closed') or not job.get('final_complete') or not progress.get('final_complete'):
        raise ValueError('Only finalized historical sessions can be reviewed')
    if any(r.get('proposals',{}).get('session_id')!=session or r.get('external_inference') is not False for r in reports):
        raise ValueError('Model report session or local provenance mismatch')
    records=[]
    for report in reports:
        rows=report.get('results',[])
        ids=[r.get('group',{}).get('id') for r in rows]
        if not 1<=len(rows)<=32 or None in ids or len(ids)!=len(set(ids)):
            raise ValueError('Use 1..32 unique reviewed groups per report')
        records.append({r['group']['id']:r for r in rows})
    if any(set(rows)!=set(records[0]) for rows in records):raise ValueError('Models used different group sets')
    raw_hash=digest(raw);samples=[]
    for number,(key,record) in enumerate(records[0].items(),1):
        group=record['group'];ids=group['start_sequences']
        if group.get('session_id')!=session or group.get('budget_rejection'):
            raise ValueError('Invalid or oversized review group')
        mode=group['input_mode_candidate']
        if mode not in ('page','screen'):raise ValueError('Unknown ink rendering mode')
        render=render_contacts if mode=='page' else render_screen_contacts
        image,evidence=render(directory,raw,session,ids)
        buffer=io.BytesIO();Image.fromarray(image).save(buffer,format='PNG');png=buffer.getvalue()
        del image
        predictions=[]
        for index,rows in enumerate(records):
            other=rows[key]
            if (other['group']!=group or other.get('input_evidence')!=evidence or
                    other.get('status') not in ('historical_candidate_requires_review','recognition_incomplete')):
                raise ValueError('Reconstructed source differs from the model input')
            recognition=other.get('recognition',{})
            text=recognition.get('latex_candidate','')
            if not isinstance(text,str) or len(text)>8192:raise ValueError('Invalid bounded model output')
            predictions.append({'model_index':index,'latex_candidate':text,
                'status':recognition.get('status'),'eos_observed':recognition.get('eos_observed',False)})
        samples.append({'id':key,'number':number,'contacts':group['contact_count'],
            'duration_seconds':(group['end_us']-group['start_us'])/1e6,
            'input_mode':mode,'source':evidence,'png_sha256':hashlib.sha256(png).hexdigest(),
            'predictions':predictions,'image_data':'data:image/png;base64,'+base64.b64encode(png).decode()})
    if digest(raw)!=raw_hash:raise ValueError('Canonical raw changed while building review')
    manifest={'schema':1,'session_id':session,'raw_sha256':raw_hash,
        'model_reports':[{'sha256':digest(path),'engine':report.get('engine')} for path,report in zip(report_paths,reports)],
        'samples':[{k:v for k,v in s.items() if k!='image_data'} for s in samples],
        'semantic_groups_verified':False,'saved_state_verified':False,'screenshots_acquired':0,
        'external_requests':False,'model_inference_run':False,'raw_modified':False}
    dataset_id=hashlib.sha256(json.dumps(manifest,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return {'dataset_id':dataset_id,'manifest':manifest,'samples':samples}


def normalize_latex(text):
    text=text.strip()
    for left,right in (('$$','$$'),('\\[','\\]'),('$','$')):
        if text.startswith(left) and text.endswith(right) and len(text)>=len(left)+len(right):
            text=text[len(left):-len(right)];break
    return re.sub(r'\s+','',text)


def evaluate(packet,labels):
    manifest=packet['manifest']
    expected_id=hashlib.sha256(json.dumps(manifest,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if packet.get('dataset_id')!=expected_id:raise ValueError('Review manifest was changed')
    if labels.get('schema')!=1 or labels.get('dataset_id')!=packet['dataset_id'] or labels.get('session_id')!=manifest['session_id']:
        raise ValueError('Labels belong to a different review set')
    samples={s['id']:s for s in manifest['samples']};rows=labels.get('annotations')
    if not isinstance(rows,list) or len(rows)>len(samples):raise ValueError('Invalid annotation count')
    seen=set();complete=[];counts={};missing_transcripts=0
    for row in rows:
        key=row.get('sample_id');kind=row.get('kind');latex=row.get('latex','');notes=row.get('notes','')
        if key not in samples or key in seen or kind not in KINDS:raise ValueError('Invalid or duplicate annotation identity/class')
        if not isinstance(latex,str) or len(latex)>8192 or not isinstance(notes,str) or len(notes)>4096:
            raise ValueError('Invalid annotation text')
        seen.add(key);counts[kind]=counts.get(kind,0)+1
        if kind=='complete_formula':
            if normalize_latex(latex):complete.append((samples[key],latex))
            else:missing_transcripts+=1
    scores=[]
    for index in range(len(manifest['model_reports'])):
        correct=0
        for sample,truth in complete:
            prediction=sample['predictions'][index]
            correct+=int(prediction['status']=='candidate' and prediction.get('eos_observed') is True and
                         normalize_latex(prediction['latex_candidate'])==normalize_latex(truth))
        scores.append({'model_index':index,'labeled_complete_formulas_with_transcription':len(complete),
            'normalized_exact_matches':correct,'normalized_exact_match_rate':correct/len(complete) if complete else None})
    return {'schema':1,'dataset_id':packet['dataset_id'],'session_id':manifest['session_id'],
        'annotation_source':'explicit_human_label_file_not_independently_verified',
        'annotated_samples':len(rows),'sample_count':len(samples),'unlabeled_samples':len(samples)-len(rows),
        'classes':counts,'scores':scores,'all_samples_labeled':len(rows)==len(samples),
        'complete_formula_transcriptions_missing':missing_transcripts,'accuracy_benchmark_complete':False,
        'metric':'Whitespace/display-delimiter normalized exact text; not mathematical equivalence.',
        'automatic_solution_steps':False,'raw_modified':False,'external_requests':False}


def write_review(packet,directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    template=Path(__file__).with_suffix('.html').read_text(encoding='utf-8')
    payload=json.dumps(packet,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    (directory/'review.html').write_text(template.replace('__REVIEW_PACKET__',payload),encoding='utf-8')
    (directory/'manifest.json').write_text(json.dumps({'dataset_id':packet['dataset_id'],'manifest':packet['manifest']},
        ensure_ascii=False,indent=2),encoding='utf-8')


def refresh_review(path):
    """Replace presentation only; retain the exact packet and browser draft key."""
    from atomic_output import atomic_bytes
    path=Path(path)
    old=path.read_text(encoding='utf-8')
    match=re.search(r'(<script id="packet" type="application/json">)(.*?)(</script>)',old,re.S)
    if not match:raise ValueError('Missing existing review packet')
    packet=json.loads(match[2]);manifest=packet['manifest']
    identity=hashlib.sha256(json.dumps(manifest,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if identity!=packet['dataset_id']:raise ValueError('Existing review identity mismatch')
    template=Path(__file__).with_suffix('.html').read_text(encoding='utf-8')
    atomic_bytes(path,template.replace('__REVIEW_PACKET__',match[2]).encode('utf-8'))
    return packet['dataset_id']


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory',type=Path);p.add_argument('--raw',type=Path)
    p.add_argument('--model-report',type=Path,action='append')
    p.add_argument('--manifest',type=Path);p.add_argument('--labels',type=Path)
    p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    if args.labels:
        if not args.manifest:p.error('--manifest required with --labels')
        report=evaluate(read_json(args.manifest),read_json(args.labels))
        with args.output.open('x',encoding='utf-8') as out:json.dump(report,out,ensure_ascii=False,indent=2)
        print(json.dumps(report,ensure_ascii=False,indent=2))
    else:
        if not (args.directory and args.raw and args.model_report):p.error('Provide --directory, --raw and --model-report')
        packet=build(args.directory,args.raw,args.model_report);write_review(packet,args.output)
        print(json.dumps({'samples':len(packet['samples']),'dataset_id':packet['dataset_id'],
            'page':str((args.output/'review.html').resolve()),'labels_received':False}))


if __name__=='__main__':main()

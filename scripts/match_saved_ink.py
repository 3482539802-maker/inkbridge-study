"""Bounded offline correspondence candidates using shape AND page position.

No native identity, deletion inference or edit application. Known translations
are explicit hypotheses; mutual uniqueness rejects ambiguous repeated strokes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

import numpy as np


def resample(points,count=48,min_length=4.):
    p=np.asarray(points,dtype=float)
    if p.ndim!=2 or p.shape[1]!=2 or len(p)<2 or len(p)>16384 or not np.isfinite(p).all():return None
    lengths=np.r_[0,np.cumsum(np.linalg.norm(np.diff(p,axis=0),axis=1))]
    keep=np.r_[True,np.diff(lengths)>1e-7];p=p[keep];lengths=lengths[keep]
    if len(p)<2 or lengths[-1]<min_length:return None
    return np.array([np.interp(np.linspace(0,lengths[-1],count),lengths,p[:,i]) for i in (0,1)]).T


def match_paths(raw,saved,translations=(),maximum_rms=1.5,uniqueness_margin=1.):
    """Translations carry the earliest possible editing seq; never apply to later ink."""
    if len(raw)>512 or len(saved)>512:raise ValueError('Offline page exceeds 512-path matching budget')
    scores=np.full((len(raw),len(saved)),np.inf);choices={}
    for i,a in enumerate(raw):
        hypotheses=[{'name':'unchanged_position','delta':[0.,0.]}]
        hypotheses.extend(t for t in translations if a['end_seq']<t['before_seq'])
        for j,b in enumerate(saved):
            candidates=sorted((float(np.sqrt(np.mean(np.sum((b['samples']-a['samples']-h['delta'])**2,axis=1)))),k)
                              for k,h in enumerate(hypotheses))
            error,k=candidates[0]
            if len(candidates)>1 and candidates[1][0]-error<uniqueness_margin:continue
            scores[i,j]=error;choices[i,j]=hypotheses[k]
    pairs=[]
    if raw and saved:
        for i,a in enumerate(raw):
            order=np.argsort(scores[i]);j=int(order[0]);error=scores[i,j]
            if error>maximum_rms:continue
            other_row=float(scores[i,order[1]]) if len(saved)>1 else float('inf')
            column=np.sort(scores[:,j]);other_column=float(column[1]) if len(raw)>1 else float('inf')
            if (int(np.argmin(scores[:,j]))!=i or other_row-error<uniqueness_margin or
                    other_column-error<uniqueness_margin):continue
            h=choices[i,j]
            pairs.append({'raw_start_seq':a['start_seq'],'raw_end_seq':a['end_seq'],
                          'saved_index':saved[j]['index'],'hypothesis':h['name'],
                          'translation_pdf_pt_candidate':h['delta'],'position_and_shape_rms_pdf_pt':float(error),
                          'native_identity_verified':False,'deletion_inferred':False})
    return pairs


def read_contacts(db,session,page,source_hash):
    raw=[];skipped=[];events=[]
    for payload, in db.execute('SELECT payload FROM edit_events ORDER BY rowid'):
        e=json.loads(payload)
        if e.get('session_id')!=session or e.get('provisional',True):raise ValueError('Final same-session ledger required')
        if e['kind']=='lasso_path_candidate' and e.get('page_id')==page:
            if len(events)>=512:raise ValueError('Offline page exceeds 512 lasso contacts')
            events.append(e)
        if e['kind']!='pen_contact_pen' or e.get('page_id')!=page:continue
        if len(raw)+len(skipped)>=512:raise ValueError('Offline page exceeds 512-contact budget')
        loc=e.get('location_evidence',{})
        if loc.get('scope')!='within_bound_page_candidate' or loc.get('source_pdf_sha256')!=source_hash:
            skipped.append({'start_seq':e['start_seq'],'reason':'incomplete_or_wrong_source'});continue
        rows=db.execute('''SELECT c.payload FROM raw_index r JOIN coordinates c ON r.seq=c.seq
            WHERE r.seq BETWEEN ? AND ? AND r.tool=? AND r.pointer=? AND r.win=? AND r.display=?
            ORDER BY r.seq LIMIT 8193''',(e['start_seq'],e['end_seq'],e['tool_type'],e['pointer_id'],e['window_id'],e['display_id'])).fetchall()
        coords=[json.loads(p) for p, in rows]
        if (len(coords)!=e['points'] or len(coords)>8192 or not coords or
            coords[0]['seq']!=e['start_seq'] or coords[-1]['seq']!=e['end_seq'] or
            any(p.get('provisional_live_revision',True) or p.get('session_id')!=session or p.get('page_uuid')!=page or
                p.get('source_pdf_sha256')!=source_hash or p.get('status') not in ('candidate','extrapolated_candidate') or
                not p.get('pdf_xy') for p in coords)):raise ValueError('Inconsistent final contact coordinates')
        samples=resample([p['pdf_xy'] for p in coords])
        if samples is None:skipped.append({'start_seq':e['start_seq'],'reason':'short_or_degenerate_path'});continue
        raw.append({'start_seq':e['start_seq'],'end_seq':e['end_seq'],'samples':samples})
    return raw,skipped,events


def seed_translation(seeds,raw,saved,events,page,source_hash):
    anchors=[p for p in seeds['pairs'] if p.get('translated_candidate')]
    if len(anchors)<3:return [],[]
    if len({p['raw_start_seq'] for p in anchors})!=len(anchors) or len({p['saved_index'] for p in anchors})!=len(anchors):
        raise ValueError('Ambiguous seed identities')
    by_raw={p['start_seq']:p for p in raw};by_saved={p['index']:p for p in saved}
    shifts=[]
    for a in anchors:
        p=by_raw.get(a['raw_start_seq']);q=by_saved.get(a['saved_index'])
        if p is None or q is None or p['end_seq']!=a['raw_end_seq']:raise ValueError('Seed source contact missing')
        delta=q['samples']-p['samples'];shift=delta.mean(axis=0)
        if np.sqrt(np.mean(np.sum((delta-shift)**2,axis=1)))>1.5 or np.linalg.norm(shift-a['translation_pdf_pt_candidate'])>1e-6:
            raise ValueError('Seed shape or translation changed')
        shifts.append(shift)
    center=np.median(shifts,axis=0)
    if max(np.linalg.norm(s-center) for s in shifts)>1.:raise ValueError('No single coherent translation seed group')
    last=max(a['raw_end_seq'] for a in anchors)
    lasso=[e for e in events if e['kind']=='lasso_path_candidate' and e['start_seq']>last and e.get('page_id')==page and
           e.get('location_evidence',{}).get('scope')=='within_bound_page_candidate' and
           e['location_evidence'].get('source_pdf_sha256')==source_hash]
    if not lasso:return [],anchors
    earliest=min(e['start_seq'] for e in lasso)
    return [{'name':'saved_seed_common_translation','delta':center.tolist(),'before_seq':earliest}],anchors


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('session-dir','raw','hinote','pdf','seeds','output'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--page',required=True)
    parser.add_argument('--fragments',action='store_true',help='Also check unmatched saved paths for partial geometry candidates')
    args=parser.parse_args()
    from coordinate_page_binding import load_binding
    from inspect_native_pen import native_pen
    from compare_native_coordinates import native_to_pdf
    import fitz
    job=json.loads((args.session_dir/'job.json').read_bytes());session=job['session_id']
    if not job.get('final_complete'):raise ValueError('Completed session required')
    binding=load_binding(args.hinote,args.pdf);native=native_pen(args.hinote)
    pages=[p for p in native['pages'] if p['page_uuid']==args.page]
    if len(pages)!=1 or args.page not in binding['pages']:raise ValueError('Unique original-bound page required')
    page=pages[0];number=binding['pages'][args.page]
    if page['background_index']!=number-1:raise ValueError('Page mapping mismatch')
    if len(page['strokes'])>512:raise ValueError('Offline page exceeds 512 saved paths')
    with args.raw.open('rb') as f:raw_hash=hashlib.file_digest(f,'sha256').hexdigest()
    seeds=json.loads(args.seeds.read_bytes())
    expected={'session_id':session,'page_uuid':args.page,'source_pdf_sha256':binding['source_pdf_sha256'],
              'source_hinote_sha256':binding['source_hinote_sha256'],'raw_sha256':raw_hash}
    if any(seeds.get(k)!=v for k,v in expected.items()):raise ValueError('Seed source identity mismatch')
    db=sqlite3.connect((args.session_dir/'pipeline.sqlite').resolve().as_uri()+'?mode=ro',uri=True)
    try:
        identity=json.loads(db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0])
        if Path(identity[0]).resolve()!=args.raw.resolve() or identity[1]!=session:raise ValueError('Raw file identity mismatch')
        raw,skipped,events=read_contacts(db,session,args.page,binding['source_pdf_sha256'])
    finally:db.close()
    with fitz.open(args.pdf) as pdf:size=[pdf[number-1].rect.width,pdf[number-1].rect.height]
    saved=[];saved_skipped=[]
    for s in page['strokes']:
        samples=resample(native_to_pdf(s['points_native'],page['native_size_candidate'],size))
        if samples is None:saved_skipped.append(s['index'])
        else:saved.append({'index':s['index'],'samples':samples})
    translations,anchors=seed_translation(seeds,raw,saved,events,args.page,binding['source_pdf_sha256'])
    pairs=match_paths(raw,saved,translations)
    anchor_keys={p['raw_start_seq'] for p in anchors}
    for p in pairs:p['used_to_estimate_translation']=p['raw_start_seq'] in anchor_keys
    result={**expected,'schema':1,'method':'48_arc_length_samples_position_gated_mutual_unique',
      'thresholds':{'maximum_rms_pdf_pt':1.5,'uniqueness_margin_pdf_pt':1.,'minimum_path_length_pdf_pt':4.},
      'placement_hypothesis':'native_1000_width_contain_center_original_pdf',
      'translations':translations,'raw_paths_considered':len(raw),'saved_paths_considered':len(saved),
      'skipped_raw':skipped,'skipped_saved_short_paths':saved_skipped,'pairs':pairs,
      'unmatched_raw_start_seqs':[p['start_seq'] for p in raw if p['start_seq'] not in {m['raw_start_seq'] for m in pairs}],
      'unmatched_saved_indices':[p['index'] for p in saved if p['index'] not in {m['saved_index'] for m in pairs}],
      'native_identity_verified':False,'edits_applied':False,'physical_accuracy_verified':False,
      'limits':['Position-gated shape candidates; unmatched is not deleted.',
                'Translation is fitted from saved seed paths, not independently observed lasso movement.',
                'Repeated shapes, short paths, missing geometry and splits remain ambiguous.',
                'A saved result does not reveal intermediate undo or erase history.']}
    if args.fragments:
        from match_ink_fragments import match_fragments
        result['partial_path_evidence']=match_fragments(raw,saved,translations,pairs)
    with args.output.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps({'raw':len(raw),'saved':len(saved),'pairs':len(pairs),
      'moved':[p['raw_start_seq'] for p in pairs if p['hypothesis']!='unchanged_position'],
      'unmatched_raw':len(result['unmatched_raw_start_seqs']),'unmatched_saved':len(result['unmatched_saved_indices'])}))


if __name__=='__main__':main()

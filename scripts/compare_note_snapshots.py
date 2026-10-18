"""Read-only native path differences, never a reconstruction of the edit stack."""
import argparse
from collections import defaultdict,deque
import hashlib
import json
from pathlib import Path
import zipfile

from inspect_hinote import inspect
from inspect_native_pen import native_pen


def signature(stroke,relative=False):
    points=stroke['points_native'];origin=points[0] if relative else (0,0)
    points=[[round(p[i]-origin[i],4) for i in (0,1)] for p in points] if relative else points
    return json.dumps([stroke['style_sha256'],stroke['layer_id'],points],separators=(',',':'))


def compare_paths(before,after):
    """Multiplicity-safe equality, then unique sampled-path translation candidates."""
    by_exact=defaultdict(deque)
    for j,s in enumerate(after):by_exact[signature(s)].append(j)
    unchanged=[];old=[];used=set()
    for i,s in enumerate(before):
        matches=by_exact[signature(s)]
        if matches:
            j=matches.popleft();unchanged.append([i,j]);used.add(j)
        else:old.append(i)
    new=[j for j in range(len(after)) if j not in used]
    a=defaultdict(list);b=defaultdict(list)
    for i in old:a[signature(before[i],True)].append(i)
    for j in new:b[signature(after[j],True)].append(j)
    moved=[];moved_old=set();moved_new=set()
    for key,indices in a.items():
        if len(indices)==1 and len(b.get(key,[]))==1:
            i=indices[0];j=b[key][0]
            # A single-position path is too weak to establish object identity.
            if before[i].get('degenerate_point_path') or after[j].get('degenerate_point_path'):continue
            delta=[after[j]['points_native'][0][k]-before[i]['points_native'][0][k] for k in (0,1)]
            moved.append({'before_index':i,'after_index':j,'translation_native_candidate':delta})
            moved_old.add(i);moved_new.add(j)
    return {'before_count':len(before),'after_count':len(after),'unchanged_path_pairs':unchanged,
            'translated_path_candidates':moved,
            'before_unmatched_indices':[i for i in old if i not in moved_old],
            'after_unmatched_indices':[j for j in new if j not in moved_new],
            'native_stroke_identity_verified':False}


def snapshot_diff(before_path,after_path):
    metadata=[inspect(path) for path in (before_path,after_path)]
    if any(m['warnings'] or len(m['documents'])!=1 for m in metadata):raise ValueError('Ambiguous or unverified export')
    if metadata[0]['documents'][0]['note_id']!=metadata[1]['documents'][0]['note_id']:raise ValueError('Different note identities')
    def pdf_hashes(path,meta):
        with zipfile.ZipFile(path) as archive:
            hashes=[]
            for item in meta['embedded_pdfs']:
                digest=hashlib.sha256()
                with archive.open(item['entry']) as source:
                    for chunk in iter(lambda:source.read(256*1024),b''):digest.update(chunk)
                hashes.append(digest.hexdigest())
            return sorted(hashes)
    hashes=[pdf_hashes(p,m) for p,m in zip((before_path,after_path),metadata)]
    if hashes[0]!=hashes[1]:raise ValueError('Original document changed')
    native=[native_pen(p) for p in (before_path,after_path)]
    pages=[{p['page_uuid']:p for p in n['pages']} for n in native]
    if any(len(p)!=len(n['pages']) for p,n in zip(pages,native)):raise ValueError('Duplicate page identity')
    changes=[]
    for identity in sorted(set(pages[0])&set(pages[1])):
        a,b=[p[identity] for p in pages]
        if a['background_index']!=b['background_index'] or a['native_size_candidate']!=b['native_size_candidate']:
            raise ValueError('Page background or dimensions changed; cannot compare in common coordinates')
        changes.append({'page_uuid':identity,'background_index':a['background_index'],**compare_paths(a['strokes'],b['strokes'])})
    return {'schema':1,'note_id':metadata[0]['documents'][0]['note_id'],
        'before_sha256':metadata[0]['sha256'],'after_sha256':metadata[1]['sha256'],
        'original_document_hashes':hashes[0],'pages':changes,
        'pages_added':sorted(set(pages[1])-set(pages[0])),'pages_removed':sorted(set(pages[0])-set(pages[1])),
        'edit_history_reconstructed':False,'limits':[
            'Snapshot differences do not prove the operation sequence or undo targets.',
            'Unmatched paths may be additions, removals, splits, or modified paths; not automatically deletions.',
            'No stable native stroke IDs are available; equal geometry is not proof of object identity.',
            'Binary point tables are decoded conservatively; saved appearance is a separate check.']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('before',type=Path);parser.add_argument('after',type=Path)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    result=snapshot_diff(args.before,args.after)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as out:json.dump(result,out,ensure_ascii=False,indent=2)
    print(json.dumps({'pages':[{'background_index':p['background_index'],'before':p['before_count'],
        'after':p['after_count'],'unchanged':len(p['unchanged_path_pairs']),
        'translated_candidates':len(p['translated_path_candidates']),
        'old_unmatched':len(p['before_unmatched_indices']),'new_unmatched':len(p['after_unmatched_indices'])}
        for p in result['pages']]},ensure_ascii=False))


if __name__=='__main__':main()

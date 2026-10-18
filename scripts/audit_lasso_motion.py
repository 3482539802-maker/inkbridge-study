"""Compare saved-path translation hypotheses with final lasso contact displacements.

Read-only evidence audit. It neither infers a selected set nor applies edits.
The shape match input must carry session, page and original-document identity.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path


def compare(events,matches):
    result={'schema':1,'comparisons':[],'rejected_pairs':[],
            'selected_objects_known':False,'operation_verified':False,
            'limits':['Contact endpoint displacement is compared without fitting it to saved paths.',
                      'A shape match is a hypothesis, not a native object ID.',
                      'Incomplete selection paths are never closed or filled in.',
                      'No unmatched path is inferred to be deleted.']}
    contacts={(e['start_seq'],e['end_seq']):e for e in events if e['kind']=='pen_contact_pen'}
    lasso=[e for e in events if e['kind']=='lasso_path_candidate']
    result['lasso_contacts']=[{'start_seq':e['start_seq'],'end_seq':e['end_seq'],
        'points':e['points'],'location_evidence':e.get('location_evidence',{}),
        'path_shape':e.get('path_shape'),'selected_objects_known':False} for e in lasso]
    pairs=[p for p in matches['pairs'] if p.get('translated_candidate')]
    raw_keys=[(p['raw_start_seq'],p['raw_end_seq']) for p in pairs]
    saved_keys=[p['saved_index'] for p in pairs]
    for pair,key in zip(pairs,raw_keys):
        source=contacts.get(key);reason=None
        location=source.get('location_evidence',{}) if source else {}
        if raw_keys.count(key)!=1 or saved_keys.count(pair['saved_index'])!=1:reason='ambiguous_match'
        elif not source or source.get('provisional',True):reason='missing_final_source_contact'
        elif location.get('scope')!='within_bound_page_candidate':reason='incomplete_source_coordinates'
        elif (source.get('session_id')!=matches.get('session_id') or
              not matches.get('page_uuid') or location.get('page_id')!=matches['page_uuid'] or
              not matches.get('source_pdf_sha256') or location.get('source_pdf_sha256')!=matches['source_pdf_sha256']):
            reason='source_identity_mismatch'
        shift=pair.get('translation_pdf_pt_candidate')
        if not isinstance(shift,list) or len(shift)!=2 or not all(isinstance(v,(float,int)) and math.isfinite(v) for v in shift):
            reason='invalid_translation'
        if reason:
            result['rejected_pairs'].append({'raw_start_seq':key[0],'saved_index':pair['saved_index'],'reason':reason})
            continue
        for drag in lasso:
            d=drag.get('location_evidence',{})
            if (drag.get('provisional',True) or drag.get('session_id')!=source['session_id'] or
                drag['start_seq']<=source['end_seq'] or drag['start_us']<=source['end_us'] or
                d.get('scope')!='within_bound_page_candidate' or
                any(d.get(k)!=location.get(k) for k in ('page_id','source_pdf_sha256','pdf_page_1based'))):continue
            delta=[b-a for a,b in zip(d['start_pdf_candidate'],d['end_pdf_candidate'])]
            if len(delta)!=2 or not all(math.isfinite(v) for v in delta):continue
            result['comparisons'].append({'raw_start_seq':key[0],'raw_end_seq':key[1],
                'saved_index':pair['saved_index'],'lasso_start_seq':drag['start_seq'],
                'lasso_end_seq':drag['end_seq'],'contact_delta_pdf_pt_candidate':delta,
                'saved_translation_pdf_pt_candidate':shift,'difference_pdf_pt':math.dist(delta,shift),
                'selection_membership_verified':False,'operation_verified':False})
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events',type=Path,required=True)
    parser.add_argument('--matches',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.resolve() in (args.events.resolve(),args.matches.resolve()):parser.error('Output must differ from inputs')
    events=[json.loads(line) for line in args.events.read_text(encoding='utf-8').splitlines() if line]
    matches=json.loads(args.matches.read_bytes());result=compare(events,matches)
    result['provenance']={str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest() for p in (args.events,args.matches)}
    result['session_id']=matches.get('session_id');result['page_uuid']=matches.get('page_uuid')
    result['source_pdf_sha256']=matches.get('source_pdf_sha256')
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'comparisons':len(result['comparisons']),'rejected_pairs':result['rejected_pairs']}))


if __name__=='__main__':main()

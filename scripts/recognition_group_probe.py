"""Offline local inference over automatic whole-contact grouping proposals."""
import argparse
import json
from pathlib import Path
import time

from recognition_groups import from_directory
from recognition_ink_input import render_contacts, render_screen_contacts


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--raw-directory',type=Path,default=Path('output/ink/sessions'))
    parser.add_argument('--engine',choices=('unimernet','texteller'),default='unimernet')
    parser.add_argument('--model',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():parser.error('Choose a new experiment output')
    proposals=from_directory(args.directory);session=proposals['session_id']
    raw=args.raw_directory/(session+'.jsonl')
    if args.engine=='unimernet':
        from recognition_unimernet_probe import UniMEREngine as Engine
        default_model='tools/formula-models/unimernet-base'
    else:
        from recognition_texteller_probe import TexTellerEngine as Engine
        default_model='tools/formula-models/texteller-3'
    engine=Engine(args.model or default_model,max_tokens=256,seconds=15)
    results=[];started=time.monotonic()
    for index,group in enumerate(proposals['groups']):
        record={'group':group};image=None
        if group['budget_rejection']:record['status']='input_budget_rejected'
        elif group['spatial_quality'].get('spatially_disconnected'):
            record['status']='spatially_disconnected_requires_review'
        elif index>=32 or time.monotonic()-started>180:record['status']='experiment_budget_exhausted'
        else:
            try:
                render=render_contacts if group['input_mode_candidate']=='page' else render_screen_contacts
                acquired=time.perf_counter()
                image,evidence=render(args.directory,raw,session,group['start_sequences'])
                record.update(input_evidence=evidence,input_acquisition_ms=(time.perf_counter()-acquired)*1000)
                record['recognition']=engine.recognize(image)
                record['status']=('historical_candidate_requires_review' if record['recognition']['status']=='candidate'
                                  else 'recognition_incomplete')
            except ValueError as error:record.update(status='input_or_model_rejected',reason=str(error))
            finally:del image
        results.append(record)
        print(json.dumps({'first_seq':group['first_seq'],'contacts':group['contact_count'],
            'status':record['status'],'latex':record.get('recognition',{}).get('latex_candidate')},ensure_ascii=False),flush=True)
    report={'schema':1,'proposals':proposals,'results':results,'engine':engine.identity,
        'model_init_ms':engine.init_ms,'experiment_seconds':time.monotonic()-started,
        'external_inference':False,'semantic_grouping_verified':False,'accuracy_benchmark':False,
        'automatic_solution_steps':False,'screenshots_acquired':0,'retained_images':0,
        'raw_modified':False,'tablet_ui_actions':[]}
    with args.output.open('x',encoding='utf-8') as out:json.dump(report,out,ensure_ascii=False,indent=2)


if __name__=='__main__':main()

"""Finite passive UI-tool survey, no screenshots, taps, app starts or raw edits."""
import argparse
import json
import re
import subprocess
from pathlib import Path
import time

from notes_diagnostics import extract_tree,notes_window,summarize_tree
from notes_edit_context import summarize_edit_tools
from tablet_files import Hdc


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target',required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--seconds',type=int,default=120)
    args=p.parse_args()
    if not 1<=args.seconds<=300:p.error('Use 1..300 seconds for the passive tool survey')
    args.output.mkdir(parents=True,exist_ok=False)
    hdc=Hdc(Path(__file__).resolve().parents[1]/'tools/hdc/hdc.exe',args.target)
    deadline=time.monotonic()+args.seconds;last=None;count=0
    while time.monotonic()<deadline:
        started=time.monotonic()
        try:
            before=notes_window(hdc.shell(['hidumper','-s','WindowManagerService','-a','-a']))
            if not before['focused']:raise ValueError('Notes not focused')
            raw=hdc.shell(['hidumper','-s','WindowManagerService','-a',f'-w {before["window_id"]} -allInfo'])
            tree=extract_tree(raw);summary=summarize_tree(tree)
            header_pid=re.search(r'^Pid:\s*(\d+)',raw,re.M)
            if header_pid is None or int(header_pid[1])!=before['pid']:raise ValueError('Tree PID changed')
            after=notes_window(hdc.shell(['hidumper','-s','WindowManagerService','-a','-a']))
            if before!=after or not summary['active_tab_marker_unique']:raise ValueError('Context changed')
            editing=summarize_edit_tools(tree)
            # Selected menu modes are not fully known yet. Retain only visible
            # control metadata under editing tool/menu areas for diagnosis.
            from notes_diagnostics import nodes
            details=[]
            for n,parents in nodes(tree):
                ids=[x.get('compid','').lower() for x in (*parents,n)]
                if any(any(word in i for word in ('eraser','lasso','select','delete','popup','menu')) for i in ids):
                    attrs=n.get('$attrs',{})
                    details.append({'id':n.get('compid'),'type':n.get('$type'),'rect':n.get('$rect'),
                        'attrs':{k:v for k,v in attrs.items() if k in ('content','selected','checked','enabled','accessibilityText')},
                        'backgroundColor':n.get('backgroundColor')})
            value={'editing':editing,'menu_diagnostics':details[:150]}
            key=json.dumps(value,sort_keys=True)
            if key!=last:
                count+=1
                row=dict(value,observed_at_pc=time.time(),window=after,
                         document_title=summary['document_title_candidate'],
                         historical_raw_mapping_allowed=False,tablet_ui_actions=[])
                with (args.output/'observations.jsonl').open('a',encoding='utf-8') as out:out.write(json.dumps(row,ensure_ascii=False)+'\n')
                last=key
            (args.output/'status.json').write_text(json.dumps({'stage':'observing','changes':count,
                'selected_tool':editing['selected_tool'],'eraser_mode':editing['eraser_mode'],'updated_at_pc':time.time()}),encoding='utf-8')
        except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired) as error:
            (args.output/'status.json').write_text(json.dumps({'stage':'waiting_context','error':str(error)[:120]}),encoding='utf-8')
        time.sleep(max(0,1-(time.monotonic()-started)))
    (args.output/'status.json').write_text(json.dumps({'stage':'completed','changes':count,'tablet_ui_actions':[]}),encoding='utf-8')


if __name__=='__main__':main()

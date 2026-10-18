"""Bounded numeric audit of independent simultaneous-page fits; no images/UI."""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from multipage_geometry import REASON, page_frames
from coordinate_alignment import valid_frame
from capture_clock_history import load_clock
from native_frame_timing import bind


def audit(directory,limit=256):
    if type(limit) is not int or not 1<=limit<=1024:raise ValueError('Bounded geometry sample required')
    directory=Path(directory)
    with closing(sqlite3.connect((directory/'pipeline.sqlite').resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        db.execute('BEGIN')
        rows=[json.loads(p) for p, in db.execute('SELECT payload FROM geometry ORDER BY id DESC LIMIT ?',(limit,))][::-1]
        coordinates=[{'page_uuid':page,'pdf_page_1based':number,'status':status,'points':count}
            for page,number,status,count in db.execute("""SELECT json_extract(payload,'$.page_uuid'),
                json_extract(payload,'$.pdf_page_1based'),status,COUNT(*) FROM coordinates
                WHERE status IN ('candidate','extrapolated_candidate') GROUP BY 1,2,3 ORDER BY 2,1,3 LIMIT 100""")]
        provisional=db.execute("SELECT COUNT(*) FROM coordinates WHERE json_extract(payload,'$.provisional_live_revision')=1").fetchone()[0]
    clock_path=directory/'clock.json'
    if clock_path.exists():rows,calibration=bind(rows,load_clock(clock_path))
    else:calibration={'usable_candidate':False,'reason':'clock_not_received'}
    multiplicity=Counter();pages=Counter();rejected=Counter();usable=0;examples=[]
    for row in rows:
        registration=row.get('registration',{})
        if registration.get('reason')!=REASON:continue
        children=page_frames(row)
        multiplicity[str(len(children))]+=1
        for child in children:
            page=child['registration']['pdf_page_1based']
            pages[page]+=1;usable+=int(valid_frame(child))
        for item in registration.get('rejected_pages',[]):rejected[item.get('reason')]+=1
        if len(examples)<5:
            examples.append({'index':row['index'],'pdf_pages':[c['registration']['pdf_page_1based'] for c in children],
                'independent_timed_children':sum(valid_frame(c) for c in children)})
    return {'schema':1,'sampled_geometry_records':len(rows),'limit':limit,
        'multipage_observations_by_accepted_page_count':dict(multiplicity),'independent_fits_by_pdf_page':dict(pages),
        'timed_usable_page_fits':usable,'page_rejections':dict(rejected),'examples':examples,
        'all_session_coordinate_counts_by_page':coordinates,'provisional_coordinate_points':provisional,
        'native_frame_clock_usable_candidate':calibration.get('usable_candidate'),
        'independent_page_accuracy_verified':False,'physical_mm_accuracy_verified':False,
        'screenshots_acquired':0,'tablet_actions':[],'raw_modified':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();result=audit(args.directory)
    with args.output.open('x',encoding='utf-8') as out:json.dump(result,out,indent=2)
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()

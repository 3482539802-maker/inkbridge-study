"""Fetch bounded public reference text for the page-motion experiment."""
import argparse,base64,hashlib,json,subprocess,time
from pathlib import Path

SOURCES={
    'opencv_lk_example':'https://raw.githubusercontent.com/opencv/opencv/4.12.0/samples/python/lk_track.py',
    'opencv_video_api':'https://raw.githubusercontent.com/opencv/opencv/4.12.0/modules/video/include/opencv2/video/tracking.hpp',
    'opencv_affine_api':'https://raw.githubusercontent.com/opencv/opencv/4.12.0/modules/calib3d/include/opencv2/calib3d.hpp',
    'opencv_license':'https://raw.githubusercontent.com/opencv/opencv/4.12.0/LICENSE',
}

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=True);report=[]
    for name,url in SOURCES.items():
        path=url.split('/4.12.0/',1)[1]
        result=subprocess.run(['gh','api','repos/opencv/opencv/contents/'+path+'?ref=4.12.0'],
            capture_output=True,check=True,timeout=30)
        content=json.loads(result.stdout)
        payload=base64.b64decode(content['content'],validate=False)
        if len(payload)>2*1024*1024:raise ValueError('Reference exceeds limit')
        (args.output/(name+'.txt')).write_bytes(payload)
        lines=payload.decode('utf-8').splitlines()
        terms=('calcOpticalFlowPyrLK','goodFeaturesToTrack','forward-backward','estimateAffinePartial2D','RANSAC','Lucas','License')
        report.append(dict(name=name,url=url,git_blob_sha=content['sha'],bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest(),
            excerpts=[dict(line=i+1,text=line[:200]) for i,line in enumerate(lines) if any(t in line for t in terms)][:25]))
    (args.output/'sources.json').write_text(json.dumps(dict(retrieved_at_pc=time.time(),sources=report),indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()

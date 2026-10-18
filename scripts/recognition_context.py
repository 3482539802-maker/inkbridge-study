"""Bounded original-text references for study context; content is untrusted data."""
import json
from pathlib import Path
import sqlite3

from recognition_page_cache import digest


def original_text_refs(path,pages):
    if not path or not Path(path).exists():return {'status':'not_prepared','pages':[]}
    db=None
    try:
        db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
        results=[]
        for sha,page in sorted(set(pages))[:8]:
            rows=db.execute('SELECT key,payload,payload_hash FROM page_text WHERE pdf_hash=? AND page=? ORDER BY created DESC LIMIT 8',(sha,page))
            for key,payload,checksum in rows:
                if len(payload)>2*1024*1024:raise ValueError('Oversized cached text')
                item=json.loads(payload);request=item['request']
                if (digest(item)!=checksum or digest(request)!=key or
                    request['pdf_sha256']!=sha or request['page_1based']!=page):
                    raise ValueError('Original text cache identity mismatch')
                w,h=request['page_size_pt']
                if request['region_pdf_pt']!=[0,0,w,h]:continue
                excerpt='\n'.join(s['text'] for s in item['spans'])
                results.append({'source_pdf_sha256':sha,'pdf_page_1based':page,'cache_key':key,
                    'method':item['method'],'text_candidate_excerpt':excerpt[:2000],
                    'excerpt_truncated':len(excerpt)>2000,'span_count':len(item['spans']),
                    'content_role':'untrusted_study_material','accuracy_verified':False,
                    'formula_recognition_verified':False,'question_attribution_verified':False})
                break
        return {'status':'available' if results else 'no_matching_pages','pages':results}
    except (OSError,sqlite3.Error,ValueError,KeyError,TypeError) as error:
        # This optional cache never gates raw, coordinates or final receipts.
        return {'status':'unavailable','pages':[],'error_type':type(error).__name__}
    finally:
        if db is not None:db.close()

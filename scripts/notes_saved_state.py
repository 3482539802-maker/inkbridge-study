"""Versioned local saved-note snapshots from user-exported hinote files.

Raw capture remains canonical input. This store records exported saved paths,
not live Notes objects or inferred undo/delete transactions.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from compare_note_snapshots import compare_paths
from coordinate_page_binding import load_binding
from inspect_native_pen import native_pen


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


class SavedNotes:
    def __init__(self,path):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS snapshots(hash TEXT PRIMARY KEY,note_id TEXT NOT NULL,
                source_path TEXT NOT NULL,imported_at_pc REAL NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pages(snapshot TEXT NOT NULL REFERENCES snapshots(hash),
                page_id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(snapshot,page_id));
            CREATE TABLE IF NOT EXISTS objects(snapshot TEXT NOT NULL,page_id TEXT NOT NULL,ordinal INTEGER NOT NULL,
                id TEXT UNIQUE NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(snapshot,page_id,ordinal),
                FOREIGN KEY(snapshot,page_id) REFERENCES pages(snapshot,page_id));
            CREATE TABLE IF NOT EXISTS comparisons(id TEXT PRIMARY KEY,before_hash TEXT NOT NULL REFERENCES snapshots(hash),
                after_hash TEXT NOT NULL REFERENCES snapshots(hash),session TEXT,payload TEXT NOT NULL);
        ''')

    def close(self):self.db.close()

    def import_decoded(self,binding,native,source_path):
        """Only public import() supplies verified decoder/binding output in production."""
        sha=binding['source_hinote_sha256']
        if native['source_hinote_sha256']!=sha:raise ValueError('Decoded snapshot hash mismatch')
        if self.db.execute('SELECT 1 FROM snapshots WHERE hash=?',(sha,)).fetchone():
            existing=self.metadata(sha)
            if any(existing[k]!=binding[k] for k in ('note_id','source_pdf_sha256')):raise ValueError('Existing snapshot binding differs')
            return sha
        pages=native['pages'];ids=[p['page_uuid'] for p in pages]
        if len(ids)!=len(set(ids)) or len(ids)>2000:raise ValueError('Ambiguous or oversized page set')
        if any(not isinstance(p,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',p) for p in ids):raise ValueError('Unsafe page identity')
        if sum(len(p['strokes']) for p in pages)>20000:raise ValueError('Saved object budget exceeded')
        payload={'schema':1,'snapshot_sha256':sha,'note_id':binding['note_id'],'title':binding['title'],
            'source_pdf_sha256':binding['source_pdf_sha256'],'page_count':len(pages),
            'saved_path_count':sum(len(p['strokes']) for p in pages),
            'authority':'user_exported_saved_snapshot','live_state_verified':False,
            'native_persistent_object_ids_available':False,'physical_accuracy_verified':False}
        with self.db:
            self.db.execute('INSERT INTO snapshots VALUES (?,?,?,?,?)',(sha,binding['note_id'],str(Path(source_path).resolve()),time.time(),json.dumps(payload)))
            for page in pages:
                meta={k:v for k,v in page.items() if k!='strokes'}
                meta['pdf_page_1based']=binding['pages'].get(page['page_uuid'])
                self.db.execute('INSERT INTO pages VALUES (?,?,?)',(sha,page['page_uuid'],json.dumps(meta)))
                for ordinal,stroke in enumerate(page['strokes']):
                    identity=digest([sha,page['page_uuid'],ordinal])
                    content={k:stroke[k] for k in ('points_native','width_native','pen_type','style_sha256','layer_id')}
                    obj=dict(stroke,local_snapshot_object_id=identity,content_sha256=digest(content),
                        snapshot_ordinal=ordinal,object_identity_scope='this_snapshot_only')
                    self.db.execute('INSERT INTO objects VALUES (?,?,?,?,?)',(sha,page['page_uuid'],ordinal,identity,json.dumps(obj)))
        return sha

    def import_file(self,hinote,pdf):
        binding=load_binding(hinote,pdf)
        return self.import_decoded(binding,native_pen(hinote),hinote)

    def metadata(self,sha):
        row=self.db.execute('SELECT payload FROM snapshots WHERE hash=?',(sha,)).fetchone()
        if not row:raise ValueError('Unknown saved snapshot')
        return json.loads(row[0])

    def page(self,sha,page_id):
        row=self.db.execute('SELECT payload FROM pages WHERE snapshot=? AND page_id=?',(sha,page_id)).fetchone()
        if not row:raise ValueError('Unknown snapshot page')
        meta=json.loads(row[0]);meta['strokes']=[json.loads(p) for p, in self.db.execute(
            'SELECT payload FROM objects WHERE snapshot=? AND page_id=? ORDER BY ordinal',(sha,page_id))]
        return meta

    def compare(self,before,after,session=None):
        a,b=self.metadata(before),self.metadata(after)
        if any(a[k]!=b[k] for k in ('note_id','source_pdf_sha256')):raise ValueError('Snapshot note or original changed')
        old={p for p, in self.db.execute('SELECT page_id FROM pages WHERE snapshot=?',(before,))}
        new={p for p, in self.db.execute('SELECT page_id FROM pages WHERE snapshot=?',(after,))}
        results=[]
        for identity in sorted(old&new):
            x,y=self.page(before,identity),self.page(after,identity)
            if any(x[k]!=y[k] for k in ('native_size_candidate','background_index')):raise ValueError('Page layout changed')
            results.append({'page_uuid':identity,**compare_paths(x['strokes'],y['strokes'])})
        payload={'schema':1,'note_id':a['note_id'],'source_pdf_sha256':a['source_pdf_sha256'],
            'before_sha256':before,'after_sha256':after,'associated_session':session,
            'association_basis':'explicit_user_experiment_pair' if session else 'explicit_snapshot_pair',
            'pages':results,'pages_added':sorted(new-old),'pages_removed':sorted(old-new),
            'saved_after_path_count':b['saved_path_count'],'operation_history_reconstructed':False,
            'live_document_state_available':False,'raw_modified':False}
        identity=digest([before,after,session])
        with self.db:self.db.execute('INSERT OR IGNORE INTO comparisons VALUES (?,?,?,?,?)',(identity,before,after,session,json.dumps(payload)))
        return payload


def export_pair(store,comparison,directory):
    from capture_clock_history import atomic_bytes
    from html import escape
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    a,b=[store.metadata(comparison[k]) for k in ('before_sha256','after_sha256')]
    pages=[]
    for page_id, in store.db.execute('SELECT page_id FROM pages WHERE snapshot=? ORDER BY page_id',(b['snapshot_sha256'],)):
        page=store.page(b['snapshot_sha256'],page_id);pages.append(page)
        atomic_bytes(directory/(page_id+'.json'),json.dumps(page,ensure_ascii=False).encode())
    manifest={'schema':1,'before':a,'after':b,'comparison':comparison,
        'pages':[{'page_uuid':p['page_uuid'],'pdf_page_1based':p['pdf_page_1based'],
                  'saved_paths':len(p['strokes']),'path':p['page_uuid']+'.json'} for p in pages]}
    atomic_bytes(directory/'saved-state.json',json.dumps(manifest,ensure_ascii=False,indent=2).encode())
    sections=[]
    for page in sorted(pages,key=lambda p:p['pdf_page_1based'] or 10**9):
        paths=[]
        for s in page['strokes']:
            xy=s['points_native']
            if s.get('degenerate_point_path'):
                paths.append(f'<circle cx="{xy[0][0]}" cy="{xy[0][1]}" r="1.5"/>')
            else:paths.append('<polyline points="'+' '.join(f'{x},{y}' for x,y in xy)+'"/>')
        width,height=page['native_size_candidate']
        sections.append('<section><h2>原件第'+escape(str(page['pdf_page_1based'] or '未知'))+'页 · '+str(len(page['strokes']))+'条保存路径</h2>'+
            '<p class="small">页面 '+escape(page['page_uuid'])+'</p><svg viewBox="0 0 '+str(width)+' '+str(height)+'"><g stroke="#253d85" fill="none" stroke-width="2">'+''.join(paths)+'</g></svg></section>')
    html='''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>已导出的保存字迹</title>
<style>body{font:16px/1.7 system-ui;margin:24px;background:#f3f6fa;color:#234}main{max-width:1000px;margin:auto}section{background:white;padding:20px;margin:16px 0}svg{width:100%;max-height:800px;border:1px solid #ddd}.small{font-size:13px;color:#667}</style><main><h1>已导出的保存字迹</h1><p>此视图来自用户导出的保存快照，包含该快照实际保存的路径；不会把原始擦除轨迹、套索轨迹叠成字迹。它不是当前平板实时状态。颜色/笔宽是简化显示，未重建原生渲染。</p><p>笔记：'''+escape(b['title'])+'；保存路径 '+str(b['saved_path_count'])+'。<a href="saved-state.json">来源和版本</a></p>'+''.join(sections)+'</main>'
    atomic_bytes(directory/'report.html',html.encode())
    return manifest


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('store','before','after','pdf','output'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--session');args=p.parse_args()
    if args.session:
        from ink_protocol import validate_session_id
        validate_session_id(args.session)
    store=SavedNotes(args.store)
    try:
        before=store.import_file(args.before,args.pdf);after=store.import_file(args.after,args.pdf)
        result=store.compare(before,after,args.session);manifest=export_pair(store,result,args.output)
        print(json.dumps({'note':result['note_id'],'saved_after_paths':manifest['after']['saved_path_count'],
                          'pages':manifest['pages'],'report':str(args.output/'report.html')}))
    finally:store.close()


if __name__=='__main__':main()

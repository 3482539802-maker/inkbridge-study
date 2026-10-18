"""Publish/inspect a course knowledge library without starting capture services."""
import argparse
import json
from pathlib import Path
import uuid
import threading
import time

from course_learning import LearningStore
from course_knowledge import action, process_incoming, publish_knowledge, read, setup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['publish', 'generate', 'inbox', 'status', 'serve'])
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--course'); parser.add_argument('--question')
    args = parser.parse_args(); config = read(args.config); store = LearningStore(config['root'])
    try:
        setup(store)
        if args.command == 'serve':
            # A separate workspace needs only a manifest/catalog, not HDC,
            # recording configuration, Notes authorization or model workers.
            from course_runtime import make_server, single_instance
            from html import escape
            from atomic_output import atomic_bytes
            with single_instance(store.root):
                server=make_server(store.root,config.get('port',18772),args.config.resolve())
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                print(json.dumps({'url':f'http://127.0.0.1:{server.server_port}/', 'capture_services_started':False}),flush=True)
                try:
                    while True:
                        config=read(args.config);process_incoming(store,config['courses'])
                        links=publish_knowledge(store,config['courses'])
                        hub='<!doctype html><meta charset="utf-8"><title>我的知识库</title><h1>我的知识库</h1>'+''.join(
                            '<p><a href="'+escape(k['url'],quote=True)+'">'+escape(k['title'])+'</a></p>' for k in links)
                        atomic_bytes(store.root/'index.html',hub.encode())
                        time.sleep(5)
                except KeyboardInterrupt: pass
                finally:server.shutdown();server.server_close()
            return
        elif args.command == 'generate':
            result = action(store, config['courses'], {'id':str(uuid.uuid4()), 'kind':'generate',
                            'course_id':args.course, 'question_id':args.question})
        elif args.command == 'inbox':
            process_incoming(store, config['courses']); result = publish_knowledge(store, config['courses'])
        elif args.command == 'publish': result = publish_knowledge(store, config['courses'])
        else:
            result = {'tutor_jobs': dict(store.db.execute('SELECT status,count(*) FROM tutor_jobs GROUP BY status')),
                      'knowledge_events': store.db.execute('SELECT count(*) FROM knowledge_events').fetchone()[0],
                      'practice_items': store.db.execute('SELECT count(*) FROM practice_items').fetchone()[0],
                      'generations': dict(store.db.execute('SELECT status,count(*) FROM practice_generations GROUP BY status'))}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally: store.close()


if __name__ == '__main__': main()

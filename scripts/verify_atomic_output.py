"""Publication lock failures must preserve evidence and leave workers alive."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import atomic_output as output
from edit_observations import EditObservationWorker
from session_pipeline import NumericProcessor, dashboard


class PublicationChecks(unittest.TestCase):
    def test_transient_lock_preserves_old_file_until_replace(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.json'
            path.write_bytes(b'old')
            replace = os.replace
            attempts = []
            def blocked(src, dst):
                self.assertEqual(path.read_bytes(), b'old')
                attempts.append(src)
                if len(attempts) < 3:
                    raise PermissionError('sharing violation')
                replace(src, dst)
            with patch.object(output.os, 'replace', side_effect=blocked), patch.object(output.time, 'sleep'):
                output.atomic_bytes(path, b'new')
            self.assertEqual(path.read_bytes(), b'new')
            self.assertEqual(len(attempts), 3)
            self.assertEqual(list(path.parent.glob('*.tmp')), [])

    def test_persistent_lock_raises_and_cleans_unpublished_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'clock.json'
            path.write_bytes(b'old')
            with patch.object(output.os, 'replace', side_effect=PermissionError('locked')) as replace, \
                 patch.object(output.time, 'sleep'):
                with self.assertRaises(PermissionError):output.atomic_bytes(path, b'new')
            self.assertEqual(replace.call_count, 21)
            self.assertEqual(path.read_bytes(), b'old')
            self.assertEqual(list(path.parent.glob('*.tmp')), [])

    def test_concurrent_publishers_never_share_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.json'
            barrier = threading.Barrier(2)
            names = []
            replace = os.replace
            def synchronized(src, dst):
                if src not in names:
                    names.append(src)
                    barrier.wait(timeout=5)
                replace(src, dst)
            values = [b'a'*10000, b'b'*12000]
            with patch.object(output.os, 'replace', side_effect=synchronized), ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(output.atomic_bytes, path, value) for value in values]
                for future in futures:future.result(timeout=10)
            self.assertEqual(len(set(names)), 2)
            self.assertIn(path.read_bytes(), values)
            self.assertEqual(list(path.parent.glob('*.tmp')), [])

    def test_write_failure_never_replaces_previous_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'clock.json';path.write_bytes(b'old')
            with patch.object(output.os, 'fsync', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):output.atomic_bytes(path, b'new')
            self.assertEqual(path.read_bytes(), b'old')
            self.assertEqual(list(path.parent.glob('*.tmp')), [])

    def test_numeric_worker_retries_even_when_error_status_is_unwritable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('a', 'b'):
                (root/('session-'+name)).mkdir()
                (root/('session-'+name)/'job.json').write_text('{}')
            worker = NumericProcessor(root, [], root)
            calls = []
            def process(path):
                calls.append(path.parent.name)
                if len(calls) >= 3:worker.stop.set()
                if path.parent.name == 'session-a':raise OSError('disk busy')
            with patch.object(worker, 'process', side_effect=process), \
                 patch.object(output, 'atomic_bytes', side_effect=PermissionError('status locked')), \
                 patch.object(output.logging, 'warning'):
                worker.run()
            self.assertEqual(calls, ['session-a', 'session-b', 'session-a'])

    def test_status_page_outage_does_not_escape_into_supervisor(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch('session_pipeline.atomic_bytes', side_effect=PermissionError('view locked')), \
             patch.object(output, 'atomic_bytes', side_effect=PermissionError('status locked')), \
             patch.object(output.logging, 'warning'):
            dashboard(Path(folder), {'stage':'observing'})

    def test_tool_observer_keeps_durable_samples_when_status_is_unwritable(self):
        with tempfile.TemporaryDirectory() as folder:
            # Run the real loop synchronously to inspect all durable barriers.
            worker = EditObservationWorker.__new__(EditObservationWorker)
            worker.directory = Path(folder);worker.session = 'synthetic'
            worker.stop = threading.Event();worker.interval = 0
            worker.is_active = lambda session: True
            count = [0]
            def read():
                count[0] += 1
                if count[0] == 3:worker.stop.set()
                return {'valid':False, 'reason':'sample'}
            from types import SimpleNamespace
            worker.reader = SimpleNamespace(read=read)
            with patch.object(output, 'atomic_bytes', side_effect=PermissionError('view locked')), \
                 patch.object(output.logging, 'warning'):
                worker.run()
            with closing(sqlite3.connect(worker.directory/'pipeline.sqlite')) as db:
                rows = [json.loads(r[0]) for r in db.execute('SELECT payload FROM edit_observations ORDER BY id')]
            self.assertEqual([r['reason'] for r in rows],
                             ['tool_observer_started','sample','sample','tool_observer_stopped'])


if __name__ == '__main__':unittest.main(verbosity=2)

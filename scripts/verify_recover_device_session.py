"""Recovery must preserve raw delivery when numeric clock transport fails."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import recover_device_session as recovery
from verify_coordinate_alignment import point


class RecoveryChecks(unittest.TestCase):
    def exercise(self, clock_error=False):
        session='de23b504-4d12-4ab9-a6db-2454e6277e5d'
        raw=(json.dumps(dict(point(1,1000),session_id=session))+'\n').encode()
        metadata=dict(session_id=session,state='completed',byte_length=len(raw),point_count=1,
                      synced=True,dropped_count=0,loss_accounting_complete=True,started_at=1,
                      ended_at=2,stop_reason='user_stop')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);output=root/'recovered'
            canonical=root/'output/ink/sessions'/f'{session}.jsonl'
            canonical.parent.mkdir(parents=True);canonical.write_bytes(raw)
            retrieved=[]
            def receive(command,**kwargs):
                name=Path(command[-1]);retrieved.append(name.name)
                name.write_bytes(raw if name.suffix=='.jsonl' else json.dumps(metadata).encode())
                return SimpleNamespace(returncode=0,stdout=b'OK')
            def clock(prefix,sid,path):
                self.assertEqual(sid,session)
                if clock_error:raise ValueError('incomplete checkpoint')
                path.write_text('{}')
                return True
            argv=['recover','--target','test','--session',session,'--output',str(output),'--replay']
            with patch('sys.argv',argv),patch.object(recovery,'ROOT',root), \
                 patch.object(recovery.subprocess,'run',side_effect=receive), \
                 patch.object(recovery,'backfill_clock',side_effect=clock), \
                 patch.object(recovery,'load_clock',return_value={'history_complete':True,'history_window_count':4}), \
                 patch.object(recovery,'replay',return_value={'complete':True}) as replay:
                recovery.main()
            replay.assert_called_once()
            self.assertEqual(canonical.read_bytes(),raw)
            self.assertEqual(retrieved,['device.session.json','device.jsonl'])
            return json.loads((output/'recovery.json').read_bytes())

    def test_clock_failure_does_not_block_raw_replay(self):
        report=self.exercise(clock_error=True)
        self.assertFalse(report['clock_recovery_complete'])
        self.assertTrue(report['canonical_raw_bytes_equal'])

    def test_recovery_reports_checkpoint_history_separately(self):
        report=self.exercise()
        self.assertTrue(report['clock_recovery_complete'])
        self.assertEqual(report['clock_history']['history_window_count'],4)
        self.assertFalse(report['synchronous_geometry_assessed'])


if __name__=='__main__':unittest.main(verbosity=2)

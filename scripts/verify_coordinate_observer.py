"""Finite waiting, context failure and cleanup checks without tablet control."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock,patch

import observe_coordinate_session as observer
from verify_coordinate_layout_check import scene, training
from coordinate_layout_check import fit


SESSION='5494f517-426f-4157-aa29-eadb39f41d39'


class ObserverChecks(unittest.TestCase):
    def test_finite_end_finalizes_display_without_stopping_capture(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            raw=root/'output/ink/sessions'/f'{SESSION}.jsonl'
            raw.parent.mkdir(parents=True);raw.write_text('raw remains recording')
            (root/'clock.json').write_text('{}')
            args=SimpleNamespace(output=root,live_coordinates=True,stable_runs=True,page_margins=True)
            with patch.object(observer,'ROOT',root),patch.object(observer,'load_clock',return_value={}), \
                 patch.object(observer,'update_live',return_value={'observation_finalized':True}) as update:
                observer.close_live_coordinates(args,SESSION,[])
            self.assertTrue(update.call_args.kwargs['finalized'])
            self.assertTrue(update.call_args.kwargs['allow_page_margins'])
            self.assertEqual(raw.read_text(),'raw remains recording')
            self.assertTrue(json.loads((root/'coordinate-progress.json').read_bytes())['observation_finalized'])

    def test_layout_contradiction_vetoes_a_low_residual_visual_fit(self):
        bad=scene(0,-800,1.3,bad=True)
        frames=Mock()
        frames.capture.return_value=object(),bad['observation']
        registration=Mock()
        registration.match.return_value=bad['registration']
        _,result,_=observer.capture_geometry(frames,registration,'note',fit(training()))
        self.assertFalse(result['valid'])
        self.assertTrue(result['visual_fit_valid_before_layout_check'])
        self.assertEqual(result['layout_consistency']['status'],'contradiction')

    def test_layout_never_revives_invalid_registration(self):
        frame=scene(0,-800)
        frames=Mock()
        frames.capture.return_value=object(),frame['observation']
        registration=Mock()
        registration.match.return_value={'valid':False,'reason':'no_supported_pdf_geometry'}
        _,result,_=observer.capture_geometry(frames,registration,'note',fit(training()))
        self.assertEqual(result,{'valid':False,'reason':'no_supported_pdf_geometry'})

    def test_context_failure_does_not_reuse_registration(self):
        frames=Mock()
        frames.capture.side_effect=[ValueError('PID changed'),(object(),{'valid':True})]
        registration=Mock()
        registration.match.return_value={'valid':True}
        observation,result,_=observer.capture_geometry(frames,registration,'test')
        self.assertFalse(observation['valid'])
        self.assertFalse(result['valid'])
        registration.match.assert_not_called()
        observation,result,_=observer.capture_geometry(frames,registration,'test')
        self.assertTrue(result['valid'])
        registration.match.assert_called_once()

    def exercise(self,begin):
        with tempfile.TemporaryDirectory(prefix='coordinate-observer-check-') as folder:
            output=Path(folder)/'run'
            now=SimpleNamespace(value=0.)
            captures=[]
            frames=Mock(prefix=[])
            def capture(title):
                captures.append(now.value)
                now.value+=1.5
                return object(),{'valid':True}
            frames.capture.side_effect=capture
            registration=Mock()
            registration.match.return_value={'valid':True}
            def receive():
                if begin is None or now.value<begin:return []
                return [{'session_id':SESSION,'started_at':1,
                         'state':'running' if now.value<begin+3 else 'completed'}]
            def sleep(seconds):now.value+=seconds
            argv=['observe','--target','test','--pdf','dummy.pdf','--title','test',
                  '--output',str(output),'--seconds','10','--wait-seconds','60']
            guard=Mock()
            with patch('sys.argv',argv),patch.object(observer,'receiver',side_effect=receive), \
                 patch.object(observer,'TabletFrames',return_value=frames), \
                 patch.object(observer,'PDFRegistration',return_value=registration), \
                 patch.object(observer,'RawGuardPoller',return_value=guard), \
                 patch.object(observer,'pull_clock',return_value=False), \
                 patch.object(observer,'backfill_clock',return_value=False), \
                 patch.object(observer.time,'monotonic',side_effect=lambda:now.value), \
                 patch.object(observer.time,'sleep',side_effect=sleep):
                observer.main()
            frames.close.assert_called_once()
            registration.close.assert_called_once()
            guard.close.assert_called_once()
            return captures,json.loads((output/'status.json').read_bytes()),now.value

    def test_waiting_does_not_spend_capture_budget_or_take_screenshots(self):
        captures,status,elapsed=self.exercise(40)
        self.assertTrue(captures)
        self.assertGreaterEqual(min(captures),40)
        self.assertLess(max(captures),43)
        self.assertEqual(status['status'],'completed')
        self.assertEqual(status['tablet_ui_actions'],[])

    def test_wait_timeout_finishes_without_images(self):
        captures,status,elapsed=self.exercise(None)
        self.assertEqual(captures,[])
        self.assertLessEqual(elapsed,60.25)
        self.assertEqual(status['samples'],0)
        self.assertEqual(status['retained_images'],0)


if __name__=='__main__':unittest.main(verbosity=2)

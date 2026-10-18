"""Verify routing and stale-status barriers without changing production config."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('inkbridge_skill_bridge',ROOT/'skills/inkbridge-study/scripts/bridge.py')
bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)


class BridgeChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='inkbridge-skill-')
        self.addCleanup(self.tmp.cleanup)
        self.directory=Path(self.tmp.name)
        self.home=self.directory/'codex'
        self.environment=patch.dict(os.environ,{'CODEX_HOME':str(self.home)})
        self.environment.start();self.addCleanup(self.environment.stop)
        self.remove_env=patch.dict(os.environ)
        self.remove_env.start();self.addCleanup(self.remove_env.stop)
        os.environ.pop('INKBRIDGE_HOME',None)
        self.a=self.checkout('production');self.b=self.checkout('other')

    def checkout(self,name):
        root=self.directory/name
        for name in ('AGENTS.md','docs/WORKFLOW.md','scripts/course_runtime.py','start-study.ps1'):
            p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('placeholder',encoding='utf-8')
        cfg=root/'output/learning/runtime.json';cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({'port':18771,'supervise_capture':True}),encoding='utf-8')
        return root.resolve()

    def test_configure_then_locate_from_unrelated_directory(self):
        bridge.configure(self.a)
        self.assertEqual(bridge.locate(),(self.a,'local_config'))
        self.assertFalse(list(bridge.config_path().parent.glob('*.tmp')))

    def test_second_checkout_cannot_silently_redirect_existing_toolbox(self):
        bridge.configure(self.a);before=bridge.config_path().read_bytes()
        with self.assertRaisesRegex(ValueError,'already configured'):bridge.configure(self.b)
        self.assertEqual(before,bridge.config_path().read_bytes())
        self.assertTrue(bridge.configure(self.b,keep_existing=True)['preserved_existing'])
        self.assertEqual(before,bridge.config_path().read_bytes())
        bridge.configure(self.b,replace=True)
        self.assertEqual(bridge.locate()[0],self.b)

    def test_bad_config_requires_explicit_replacement_and_invalid_root_never_writes(self):
        bridge.configure(self.a);bridge.config_path().write_text('{broken',encoding='utf-8')
        with self.assertRaises(ValueError):bridge.configure(self.b,keep_existing=True)
        bridge.configure(self.a,replace=True)
        before=bridge.config_path().read_bytes()
        with self.assertRaises(ValueError):bridge.configure(self.directory/'absent',replace=True)
        self.assertEqual(before,bridge.config_path().read_bytes())

    def test_explicit_environment_overrides_saved_default_and_does_not_fallback_if_invalid(self):
        bridge.configure(self.a)
        with patch.dict(os.environ,{'INKBRIDGE_HOME':str(self.b)}):
            self.assertEqual(bridge.locate(),(self.b,'environment'))
        with patch.dict(os.environ,{'INKBRIDGE_HOME':str(self.directory/'missing')}):
            with self.assertRaises(ValueError):bridge.locate()

    def status(self,payload):
        with patch.object(bridge,'build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value=json.dumps(payload).encode()
            result=bridge.status(self.a)
            self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'],5)
            return result

    def healthy(self):
        return {'updated_at_pc':time.time(),'capture':{'checked_at_pc':time.time(),
                'pipeline_status_fresh':True,'recording':False}}

    def test_fresh_status_read_has_a_finite_timeout(self):
        value=self.healthy();self.assertEqual(self.status(value),value)

    def test_stale_or_missing_status_cannot_claim_ready(self):
        for stamp in (time.time()-46,time.time()+60,None,True,float('nan')):
            value=self.healthy();value['updated_at_pc']=stamp
            with self.subTest(stamp=stamp),self.assertRaises(ValueError):self.status(value)

    def test_fresh_dashboard_does_not_mask_stale_capture_or_missing_health(self):
        for capture in ({},{'attention_required':True},
                        {'checked_at_pc':time.time()-100,'pipeline_status_fresh':True},
                        {'checked_at_pc':time.time(),'pipeline_status_fresh':False}):
            value=self.healthy();value['capture']=capture
            with self.subTest(capture=capture),self.assertRaises(ValueError):self.status(value)

    def test_redirects_are_refused(self):
        with self.assertRaises(ValueError):bridge.NoRedirect().redirect_request(None,None,302,'',{},'https://example.invalid')


if __name__=='__main__':unittest.main(verbosity=2)

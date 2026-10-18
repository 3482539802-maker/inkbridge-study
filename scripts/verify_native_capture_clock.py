"""Compile/run production C++ history and read it through production PC gates."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from capture_clock_history import load_clock, history_directory
from coordinate_alignment import assess_clock

ROOT = Path(__file__).resolve().parents[1]


class NativeClockChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT/'output/test-runs').mkdir(parents=True, exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix='native-clock-check-', dir=ROOT/'output/test-runs')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        native = ROOT/'app/entry/src/main/cpp'
        sources = [str(ROOT/'scripts/verify_native_capture_clock.cpp'), str(native/'capture_clock.cpp')]
        binary = cls.root/('verify.exe' if os.name == 'nt' else 'verify')
        if os.name == 'nt':
            vswhere = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)'))/'Microsoft Visual Studio/Installer/vswhere.exe'
            if not vswhere.exists(): raise unittest.SkipTest('Host C++ compiler unavailable')
            install = subprocess.check_output([str(vswhere), '-latest', '-products', '*', '-requires',
                'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property', 'installationPath'], text=True).strip()
            if not install: raise unittest.SkipTest('Host C++ compiler unavailable')
            setup = str(Path(install)/'VC/Auxiliary/Build/vcvars64.bat')
            # Environment is parsed in memory and never printed (it may contain credentials).
            environment_script = cls.root/'compiler-env.cmd'
            environment_script.write_text(f'@echo off\ncall "{setup}" >nul\nif errorlevel 1 exit /b 1\nset\n', encoding='ascii')
            result = subprocess.run(['cmd.exe','/d','/c',str(environment_script)], capture_output=True, check=True)
            env = dict(os.environ)
            for line in result.stdout.decode('mbcs', errors='replace').splitlines():
                if '=' in line and not line.startswith('='):
                    key, value = line.split('=', 1); env[key] = value
            # MSVC creates linker response files in TEMP. Use the existing,
            # isolated ASCII workspace path, not a user-profile temp path.
            env['TEMP'] = env['TMP'] = str(cls.root)
            compiler = shutil.which('cl.exe', path=env.get('Path', env.get('PATH')))
            command = [compiler, '/nologo','/std:c++17','/EHsc','/O2',f'/I{native}',f'/Fe:{binary}'] + sources
        else:
            compiler = shutil.which('c++')
            if not compiler: raise unittest.SkipTest('Host C++ compiler unavailable')
            env = None
            command = [compiler,'-std=c++17','-O2','-pthread','-I',str(native),'-o',str(binary)] + sources
        compiled = subprocess.run(command, cwd=cls.root, env=env, capture_output=True, text=True, errors='replace', timeout=90)
        if compiled.returncode: raise AssertionError(compiled.stdout + compiled.stderr)
        subprocess.run([str(binary), str(cls.root)], check=True, capture_output=True, timeout=30)
        for name in ('two-hour','failed','worker','retry'):
            folder = cls.root/name
            destination = history_directory(folder/'clock.json')
            destination.mkdir()
            latest = json.loads((folder/'clock.clock.json').read_bytes())
            (folder/'clock.json').write_text(json.dumps(latest), encoding='utf-8')
            for index in range(latest['history_segments']):
                (destination/f'{index}.json').write_bytes((folder/f'clock.clock-{index}.json').read_bytes())

    def test_two_hour_native_history_roundtrips_through_pc(self):
        state = load_clock(self.root/'two-hour/clock.json')
        self.assertTrue(state['history_complete'])
        self.assertEqual(len(state['observations']),1440)
        self.assertEqual(len(state['anchors']),1441)
        self.assertTrue(assess_clock(state)['usable_candidate'])
        self.assertTrue(all(o['sampling_stage']=='native_input_callback' for o in state['observations']))

    def test_evicted_uncommitted_history_is_rejected_after_disk_recovers(self):
        state = load_clock(self.root/'failed/clock.json')
        self.assertTrue(state['history_lost'])
        self.assertFalse(state['history_complete'])
        self.assertFalse(assess_clock(state)['usable_candidate'])

    def test_native_worker_runs_and_commits_without_arkts(self):
        state = load_clock(self.root/'worker/clock.json')
        self.assertTrue(state['history_complete'])
        self.assertEqual(len(state['observations']),8)
        self.assertTrue(assess_clock(state)['usable_candidate'])
        self.assertTrue(state['stopped'],'Only the durable final latest sidecar completes numeric jobs')

    def test_checkpoint_retry_preserves_original_bytes_and_stop_tail(self):
        state = load_clock(self.root/'retry/clock.json')
        self.assertTrue(state['history_complete'])
        self.assertEqual(len(state['observations']),17)
        self.assertTrue(assess_clock(state)['usable_candidate'])


if __name__ == '__main__': unittest.main(verbosity=2)

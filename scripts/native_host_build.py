"""Compile small production-native fault harnesses without exposing the SDK env."""
import os
from pathlib import Path
import shutil
import subprocess


def compile_native(directory, sources, name, libraries=()):
    directory = Path(directory).resolve()
    native = Path(__file__).resolve().parents[1]/'app/entry/src/main/cpp'
    binary = directory/(name+('.exe' if os.name == 'nt' else ''))
    env = dict(os.environ)
    if os.name == 'nt':
        vswhere = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)'))/'Microsoft Visual Studio/Installer/vswhere.exe'
        if not vswhere.exists():raise RuntimeError('Host C++ compiler unavailable')
        install = subprocess.check_output([str(vswhere), '-latest', '-products', '*', '-requires',
            'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property', 'installationPath'], text=True).strip()
        if not install:raise RuntimeError('Host C++ compiler unavailable')
        setup = Path(install)/'VC/Auxiliary/Build/vcvars64.bat'
        script = directory/'compiler-env.cmd'
        script.write_text(f'@echo off\ncall "{setup}" >nul\nif errorlevel 1 exit /b 1\nset\n',encoding='ascii')
        result = subprocess.run(['cmd.exe','/d','/c',str(script)],capture_output=True,check=True)
        for line in result.stdout.decode('mbcs',errors='replace').splitlines():
            if '=' in line and not line.startswith('='):
                key,value = line.split('=',1);env[key]=value
        env['TEMP']=env['TMP']=str(directory)
        compiler=shutil.which('cl.exe',path=env.get('Path',env.get('PATH')))
        command=[compiler,'/nologo','/std:c++17','/EHsc','/O2',f'/I{native}',f'/Fe:{binary}']
        command += [str(p) for p in sources] + list(libraries)
    else:
        compiler=shutil.which('c++')
        if not compiler:raise RuntimeError('Host C++ compiler unavailable')
        command=[compiler,'-std=c++17','-O2','-pthread','-I',str(native),'-o',str(binary)]
        command += [str(p) for p in sources]
    result=subprocess.run(command,cwd=directory,env=env,capture_output=True,text=True,errors='replace',timeout=90)
    if result.returncode:raise AssertionError(result.stdout+result.stderr)
    return binary

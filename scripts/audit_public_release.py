"""Audit Git-tracked public distribution paths and common credential patterns.

Run after staging the intended release. This complements a full secret scanner;
it is not proof that arbitrary user content is safe to publish.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


def audit(root):
    names=subprocess.check_output(['git','ls-files','-z'],cwd=root).decode('utf-8').split('\0')
    findings=[];files=[]
    patterns={
        'private_key':r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----',
        'github_token':r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b',
        'api_token':r'\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}\b',
        'aws_access_key':r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    }
    denied_prefixes=('output/','materials/','tools/','workspace/','.playwright-cli/','docs/history/','examples/verified-capture/')
    denied_extensions={'.p12','.p7b','.cer','.csr','.jks','.keystore','.pem','.key','.hap','.har','.sqlite','.db','.jsonl','.pdf','.hinote','.zip','.exe','.dll','.safetensors','.pth','.pt'}
    for name in filter(None,names):
        p=root/name
        forbidden=(name.startswith(denied_prefixes) or p.suffix.lower() in denied_extensions or
                   p.name=='.env' or p.name.startswith('.env.') or name=='app/build-profile.json5' or
                   any(part in ('__pycache__','.auth','node_modules','.venv') for part in p.parts))
        if forbidden:findings.append({'path':name,'reason':'private_or_generated_path'})
        if p.is_symlink():
            findings.append({'path':name,'reason':'symlink_needs_manual_review'});continue
        if not p.is_file():
            findings.append({'path':name,'reason':'missing_tracked_file'});continue
        data=p.read_bytes()
        files.append({'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        try:text=data.decode('utf-8-sig')
        except UnicodeError:
            findings.append({'path':name,'reason':'binary_needs_manual_review'});continue
        for kind,pattern in patterns.items():
            for match in re.finditer(pattern,text):
                findings.append({'path':name,'reason':kind,'line':text[:match.start()].count('\n')+1})
    return {'passed':not findings,'tracked_files':len(files),'findings':findings,'files':files,
            'scope':'tracked path policy and selected credential signatures; also run full secret/history scan'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--report',type=Path)
    args=parser.parse_args();result=audit(args.root.resolve())
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        with args.report.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='files'},ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())

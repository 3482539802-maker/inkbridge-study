"""Fetch public upstream documentation for the recognition latency investigation.

No user documents, tablet access, credentials or external inference requests.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import urllib.request

DEST = Path(__file__).resolve().parents[1] / 'output/test-runs/recognition-research-20261016/sources'
REPOS = ['RapidAI/RapidOCR', 'PaddlePaddle/PaddleOCR', 'breezedeus/Pix2Text',
         'lukas-blecher/LaTeX-OCR', 'opendatalab/UniMERNet',
         'Green-Wood/BTTR', 'Green-Wood/CoMER', 'RapidAI/RapidLaTeXOCR']
FILES = {
    'pp-formulanet-module.md': ('PaddlePaddle/PaddleOCR', 'docs/version3.x/module_usage/formula_recognition.en.md'),
    'pp-formulanet-algorithm.md': ('PaddlePaddle/PaddleOCR', 'docs/version2.x/algorithm/formula_recognition/algorithm_rec_ppformulanet_en.md'),
    'mathwriting.md': ('google-research/google-research', 'mathwriting/README.md'),
    'pp-ocrv6.md': ('PaddlePaddle/PaddleOCR', 'docs/version3.x/algorithm/PP-OCRv6/PP-OCRv6.en.md'),
}


def fetch(url):
    if url.startswith('https://api.github.com/'):
        result = subprocess.run(['gh', 'api', url.removeprefix('https://api.github.com/')],
                                capture_output=True, timeout=40)
        if result.returncode:
            raise RuntimeError(result.stderr.decode('utf-8', errors='replace').strip())
        return result.stdout
    req = urllib.request.Request(url, headers={'User-Agent': 'InkBridge-public-research',
                                               'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=35) as response:
        return response.read()


def one(repo):
    name = repo.replace('/', '__')
    result = {'repo': repo, 'url': 'https://github.com/' + repo,
              'retrieved_at': datetime.now(timezone.utc).isoformat()}
    try:
        meta = json.loads(fetch('https://api.github.com/repos/' + repo))
        result.update({k: meta.get(k) for k in ('default_branch', 'pushed_at', 'archived', 'stargazers_count', 'license')})
        (DEST / (name + '.metadata.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        readme = fetch('https://api.github.com/repos/' + repo + '/readme')
        import base64
        content = base64.b64decode(json.loads(readme)['content'])
        (DEST / (name + '.README.md')).write_bytes(content)
        result['readme_bytes'] = len(content)
    except Exception as exc:
        result['error'] = str(exc)
    return result


if __name__ == '__main__':
    DEST.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(one, REPOS))
    (DEST / 'index.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    for row in results:
        print(json.dumps(row, ensure_ascii=False))
    import base64
    for name, (repo, path) in FILES.items():
        try:
            data = json.loads(fetch('https://api.github.com/repos/' + repo + '/contents/' + path))
            (DEST / name).write_bytes(base64.b64decode(data['content']))
            print(name, data.get('html_url'))
        except Exception as exc:
            print(name, str(exc))

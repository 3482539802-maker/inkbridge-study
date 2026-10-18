"""Compare the bounded OCR experiments without claiming recognition accuracy."""
from difflib import SequenceMatcher
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1] / 'output/test-runs/recognition-research-20261016'
baseline = json.loads((root / 'ocr-benchmark.json').read_text(encoding='utf-8'))
optimized = json.loads((root / 'ocr-no-upscale.json').read_text(encoding='utf-8'))
comparisons = []
for candidate in optimized['runs']:
    original = next(row for row in baseline['runs'] if row['threads'] == 0 and row['input'] == candidate['input'])
    a = [row['text'] for row in original['recognized_text']]
    b = [row['text'] for row in candidate['recognized_text']]
    matcher = SequenceMatcher(a=a, b=b, autojunk=False)
    row = {'input': candidate['input'], 'baseline_ms': original['warm_median_ms'],
           'optimized_ms': candidate['warm_median_ms'],
           'speed_ratio': original['warm_median_ms'] / candidate['warm_median_ms'],
           'text_equal': a == b, 'baseline_boxes': len(a), 'optimized_boxes': len(b),
           'changes': [{'baseline': a[i:j], 'optimized': b[k:l]}
                       for op, i, j, k, l in matcher.get_opcodes() if op != 'equal'],
           'ground_truth_accuracy_verified': False}
    comparisons.append(row)
(root / 'comparison.json').write_text(json.dumps(comparisons, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(comparisons, ensure_ascii=False, indent=2))

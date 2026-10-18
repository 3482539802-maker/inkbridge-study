"""Reject diagnostic cache/UUID confusion; these checks do not prove live mapping."""
import copy
import json
from pathlib import Path
import unittest

from notes_diagnostics import bounds, extract_tree, notes_window, summarize_events, summarize_tree

PAGE = 'a'*32
OTHER = 'b'*32


def tree():
    return {'$type': 'root', '$children': [
        {'$type': 'Row', 'compid': 'FirstToolBarNoteTitleOuterLayer', '$children': [
            {'$type': 'SymbolGlyph'}, {'compid': 'FirstToolBarNoteTitleText', '$attrs': {'content': 'Test'}}]},
        {'$type': 'Stack', 'compid': 'pageAllLayer_'+PAGE, '$rect': '[10.25, -40.50],[810.25,1000.50]',
         '$children': [{'$type': 'XComponent', 'compid': 'allStylus'+OTHER+'_9_123', '$attrs': {'$SurfaceId': '20'}}]}]}


class DiagnosticSafety(unittest.TestCase):
    def test_nonmatching_surface_uuid_is_not_page_binding(self):
        result = summarize_tree(tree())
        self.assertEqual(result['document_title_candidate'], 'Test')
        self.assertEqual(result['page_layers'][0]['unclipped_window_bounds'], [10.25, -40.5, 810.25, 1000.5])
        self.assertTrue(result['surfaces'][0]['uuid_conflicts_with_ancestor'])
        self.assertFalse(result['surfaces'][0]['verified_page_binding'])
        self.assertFalse(result['native_ink_points_available'])

    def test_hidden_and_cached_pages_excluded(self):
        sample = tree()
        cached = copy.deepcopy(sample['$children'][1])
        sample['$CachedItems'] = [cached]
        sample['$children'][1]['visible'] = 'false'
        self.assertEqual(summarize_tree(sample)['page_layers'], [])

    def test_duplicate_title_marker_rejected(self):
        sample = tree()
        sample['$children'].append(copy.deepcopy(sample['$children'][0]))
        self.assertIsNone(summarize_tree(sample)['document_title_candidate'])

    def test_structural_wrapper_preserves_active_marker(self):
        sample = tree()
        row = sample['$children'][0]
        row['$children'][0] = {'$type': 'IfElse', '$children': [row['$children'][0]]}
        # A close button's SymbolGlyph must never become an active marker.
        row['$children'].append({'$type': 'Button', '$children': [{'$type': 'SymbolGlyph'}]})
        self.assertEqual(summarize_tree(sample)['document_title_candidate'], 'Test')

    def test_wrong_bundle_or_missing_tree_rejected(self):
        for sample in ('bundleName:other\n'+json.dumps(tree()), 'bundleName:com.huawei.hmos.hinote\n{}'):
            with self.assertRaises(ValueError): extract_tree(sample)

    def test_window_focus_and_multiple_windows(self):
        sample = 'hinote0 0 123 45 1\nFocus window: 46\n'
        self.assertFalse(notes_window(sample)['focused'])
        with self.assertRaises(ValueError): notes_window(sample+'hinote0 0 124 47 1\n')

    def test_gesture_acceptance_is_not_complete_pen_capture(self):
        sample = ('bundleName:com.huawei.hmos.hinote\n0: event tree =>\n'
                  ' id: 1, type: TouchDown, timestamp: 2026-10-16 19:05:01.262, isInjected: 0\n'
                  ' frameNodeId: 20, type: PinchRecognizer\n'
                  ' procedure: HandleTouchMove, state: FAIL, disposal: REJECT\n'
                  ' frameNodeId: 21, type: PanRecognizer\n'
                  ' procedure: HandleTouchMove, state: SUCCEED, disposal: ACCEPT\n')
        result = summarize_events(sample)
        decisions = result['recent_event_trees'][0]['recognizers']
        self.assertEqual([r['accepted'] for r in decisions], [False, True])
        self.assertFalse(result['complete_input_stream'])
        self.assertFalse(result['clock_aligned_to_raw'])

    def test_degenerate_and_unrecognised_bounds_rejected(self):
        for sample in ('[1,1],[1,2]', '[NaN,1],[2,3]', '[1,1][2,3]', '[4,1],[2,3]'):
            self.assertIsNone(bounds(sample))

    def test_optional_real_diagnostic_fixture(self):
        path = Path(__file__).resolve().parents[1]/'output/test-runs/20261016-local-interfaces-001/notes-allInfo.txt'
        if not path.exists(): self.skipTest('Private local evidence not distributed')
        result = summarize_tree(extract_tree(path.read_text(encoding='utf-8')))
        self.assertGreaterEqual(len(result['page_layers']), 2)
        self.assertTrue(any(s['uuid_conflicts_with_ancestor'] for s in result['surfaces']))


if __name__ == '__main__':
    unittest.main(verbosity=2)

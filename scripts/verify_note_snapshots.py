import copy
import unittest
from compare_note_snapshots import compare_paths


def stroke(points):return {'points_native':points,'style_sha256':'style','layer_id':'0','degenerate_point_path':len({tuple(p) for p in points})==1}


class SnapshotChecks(unittest.TestCase):
    def test_duplicate_paths_preserve_multiplicity(self):
        a=stroke([[0,0],[1,1]])
        result=compare_paths([a,a],[a])
        self.assertEqual(len(result['unchanged_path_pairs']),1)
        self.assertEqual(result['before_unmatched_indices'],[1])

    def test_unique_translation_candidate_is_not_native_identity(self):
        a=stroke([[0,0],[1,1],[2,1]])
        b=stroke([[10,20],[11,21],[12,21]])
        result=compare_paths([a],[b])
        self.assertEqual(result['translated_path_candidates'][0]['translation_native_candidate'],[10,20])
        self.assertFalse(result['native_stroke_identity_verified'])

    def test_ambiguous_translation_and_degenerate_dots_remain_unmatched(self):
        a=stroke([[0,0],[1,1]])
        result=compare_paths([a,a],[stroke([[5,5],[6,6]])])
        self.assertFalse(result['translated_path_candidates'])
        result=compare_paths([stroke([[0,0],[0,0]])],[stroke([[1,1],[1,1]])])
        self.assertFalse(result['translated_path_candidates'])

    def test_style_changes_and_split_paths_not_labeled_translations(self):
        a=stroke([[0,0],[1,1],[2,2]])
        b=copy.deepcopy(a);b['style_sha256']='different'
        self.assertFalse(compare_paths([a],[b])['translated_path_candidates'])
        result=compare_paths([a],[stroke([[0,0],[1,1]]),stroke([[1,1],[2,2]])])
        self.assertEqual(result['before_unmatched_indices'],[0])
        self.assertEqual(result['after_unmatched_indices'],[0,1])


if __name__=='__main__':unittest.main(verbosity=2)

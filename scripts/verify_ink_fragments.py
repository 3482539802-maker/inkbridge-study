import unittest
import numpy as np
from match_ink_fragments import fit_fragment,match_fragments


class FragmentChecks(unittest.TestCase):
    def raw(self,points=None,start=1,end=2):
        return {'start_seq':start,'end_seq':end,'samples':np.array(points if points is not None else [[0,0],[30,0]],float)}

    def saved(self,points,index=0):return {'index':index,'samples':np.array(points,float)}

    def test_two_disjoint_retained_pieces_do_not_become_deletions(self):
        result=match_fragments([self.raw()],[self.saved([[0,0],[8,0]]),self.saved([[20,0],[30,0]],1)])
        self.assertEqual(len(result['candidates']),2)
        self.assertEqual(result['candidates'][0]['raw_arc_interval_pdf_pt'],[0.,8.])
        self.assertTrue(all(not p['erase_operation_verified'] for p in result['candidates']))
        self.assertFalse(result['edits_applied'])

    def test_nearby_parallel_strokes_and_duplicate_sources_rejected(self):
        r=[self.raw(),self.raw([[0,.3],[30,.3]],3,4)]
        result=match_fragments(r,[self.saved([[4,0],[15,0]])])
        self.assertEqual(result['candidates'],[])
        self.assertEqual(result['ambiguities'][0]['reason'],'competing_path_or_translation')
        with self.assertRaises(ValueError):match_fragments(r+r,[])

    def test_order_and_shortcuts_are_not_fragments(self):
        self.assertIsNone(fit_fragment([[0,0],[30,0]],[[20,0],[10,0]]))
        self.assertIsNone(fit_fragment([[0,0],[10,10],[20,0]],[[0,0],[20,0]]))
        self.assertIsNone(fit_fragment([[0,0],[30,0]],[[5,0],[15,0],[8,0],[20,0]]))

    def test_overlapping_saved_fragments_both_rejected(self):
        result=match_fragments([self.raw()],[self.saved([[0,0],[15,0]]),self.saved([[10,0],[20,0]],1)])
        self.assertEqual(result['candidates'],[]);self.assertEqual(len(result['ambiguities']),2)

    def test_whole_short_nonfinite_oversized_paths(self):
        for p in ([[0,0],[30,0]],[[0,0],[2,0]],[[0,0],[0,0]]):
            self.assertEqual(match_fragments([self.raw()],[self.saved(p)])['candidates'],[])
        for p in ([[0,0],[float('nan'),3]],[[0,0]]*1025):
            with self.assertRaises(ValueError):match_fragments([self.raw()],[self.saved(p)])
        with self.assertRaises(ValueError):match_fragments([self.raw()]*513,[])

    def test_translation_not_applied_to_later_ink_or_fitted_freely(self):
        s=[self.saved([[100,10],[110,10]])]
        h={'name':'seed','delta':[100,10],'before_seq':5}
        self.assertEqual(len(match_fragments([self.raw()],s,[h])['candidates']),1)
        self.assertEqual(match_fragments([self.raw(end=6)],s,[h])['candidates'],[])
        self.assertEqual(match_fragments([self.raw()],s)['candidates'],[])

    def test_known_whole_match_excluded_and_input_unmodified(self):
        a=self.raw();b=self.saved([[0,0],[8,0]]);prior=a['samples'].copy()
        result=match_fragments([a],[b],whole_pairs=[{'saved_index':0}])
        self.assertEqual(result['candidates'],[]);np.testing.assert_array_equal(a['samples'],prior)
        result=match_fragments([a],[b],whole_pairs=[{'saved_index':99,'raw_start_seq':1}])
        self.assertEqual(result['candidates'],[])
        self.assertEqual(result['ambiguities'][0]['reason'],'raw_already_has_whole_saved_match')


if __name__=='__main__':unittest.main(verbosity=2)

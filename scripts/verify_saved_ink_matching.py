import copy
import unittest
import numpy as np
from match_saved_ink import match_paths,resample,seed_translation


class MatchChecks(unittest.TestCase):
    def path(self,offset=(0,0)):
        return resample(np.array([[0,0],[3,5],[10,2]],float)+offset)

    def test_position_disambiguates_repeated_shapes(self):
        raw=[{'start_seq':i+1,'end_seq':i+1,'samples':self.path((20*i,0))} for i in range(2)]
        saved=[{'index':i,'samples':self.path((20*i,0))} for i in range(2)]
        pairs=match_paths(raw,saved)
        self.assertEqual([(p['raw_start_seq'],p['saved_index']) for p in pairs],[(1,0),(2,1)])
        self.assertTrue(all(not p['native_identity_verified'] for p in pairs))

    def test_duplicates_and_near_competitors_stay_unmatched(self):
        raw=[{'start_seq':1,'end_seq':2,'samples':self.path()}]
        for offset in ((0,0),(.5,0)):
            saved=[{'index':0,'samples':self.path()},{'index':1,'samples':self.path(offset)}]
            self.assertEqual(match_paths(raw,saved),[])
        self.assertEqual(match_paths(raw*2,[{'index':0,'samples':self.path()}]),[])

    def test_translation_does_not_move_ink_written_after_operation(self):
        raw=[{'start_seq':1,'end_seq':2,'samples':self.path()}]
        saved=[{'index':0,'samples':self.path((20,10))}]
        h={'name':'test_candidate','delta':[20,10],'before_seq':5}
        self.assertEqual(len(match_paths(raw,saved,[h])),1)
        raw[0]['end_seq']=6
        self.assertEqual(match_paths(raw,saved,[h]),[])

    def test_empty_short_nonfinite_and_path_budgets(self):
        for p in ([],[[1,1],[1,1]],[[0,0],[1,1]],[[0,0],[float('nan'),3]]):self.assertIsNone(resample(p))
        self.assertEqual(match_paths([],[]),[])
        with self.assertRaises(ValueError):match_paths([{}]*513,[])

    def test_seed_change_and_repeated_identity_rejected(self):
        raw=[{'start_seq':i,'end_seq':i,'samples':self.path((i*20,0))} for i in (1,2,3)]
        saved=[{'index':i,'samples':self.path((i*20+100,10))} for i in (1,2,3)]
        seeds={'pairs':[{'raw_start_seq':i,'raw_end_seq':i,'saved_index':i,'translated_candidate':True,
                        'translation_pdf_pt_candidate':[100,10]} for i in (1,2,3)]}
        lasso=[{'kind':'lasso_path_candidate','start_seq':5,'page_id':'p','location_evidence':{
            'scope':'within_bound_page_candidate','source_pdf_sha256':'h'}}]
        h,_=seed_translation(seeds,raw,saved,lasso,'p','h')
        self.assertEqual(h[0]['delta'],[100,10])
        for key,value in (('saved_index',1),('translation_pdf_pt_candidate',[90,0])):
            bad=copy.deepcopy(seeds);bad['pairs'][1][key]=value
            with self.assertRaises(ValueError):seed_translation(bad,raw,saved,lasso,'p','h')
        self.assertEqual(seed_translation(seeds,raw,saved,lasso,'other','h')[0],[])


if __name__=='__main__':unittest.main(verbosity=2)

import copy
import unittest
from audit_lasso_motion import compare


class MotionChecks(unittest.TestCase):
    def fixture(self):
        loc={'scope':'within_bound_page_candidate','page_id':'page','source_pdf_sha256':'hash',
             'pdf_page_1based':1,'start_pdf_candidate':[10,20],'end_pdf_candidate':[6,19]}
        source={'kind':'pen_contact_pen','session_id':'s','provisional':False,'start_seq':1,'end_seq':3,
                'start_us':10,'end_us':30,'location_evidence':loc}
        drag=dict(source,kind='lasso_path_candidate',start_seq=5,end_seq=10,start_us=50,end_us=100,points=6)
        pair={'raw_start_seq':1,'raw_end_seq':3,'saved_index':1,'translated_candidate':True,
              'translation_pdf_pt_candidate':[-3,-2]}
        return [source,drag],{'session_id':'s','page_uuid':'page','source_pdf_sha256':'hash','pairs':[pair]}

    def test_independent_difference_is_not_fitted_away(self):
        events,matches=self.fixture();result=compare(events,matches)
        self.assertAlmostEqual(result['comparisons'][0]['difference_pdf_pt'],2**.5)
        self.assertFalse(result['operation_verified'])

    def test_cross_page_session_time_and_incomplete_drag_rejected(self):
        for change in (lambda e:e.update(session_id='other'),lambda e:e.update(start_us=20),
                       lambda e:e.update(provisional=True),
                       lambda e:e['location_evidence'].update(page_id='other'),
                       lambda e:e['location_evidence'].update(scope='partial_or_unknown')):
            events,matches=self.fixture();events=copy.deepcopy(events);change(events[1])
            self.assertEqual(compare(events,matches)['comparisons'],[])

    def test_match_identity_and_duplicate_shapes_are_not_accepted(self):
        for change in (lambda m:m.update(source_pdf_sha256='other'),
                       lambda m:m['pairs'].append(copy.deepcopy(m['pairs'][0]))):
            events,matches=self.fixture();change(matches)
            result=compare(events,matches)
            self.assertEqual(result['comparisons'],[]);self.assertTrue(result['rejected_pairs'])


if __name__=='__main__':unittest.main(verbosity=2)

"""Whole contacts, exclusion barriers and explicit oversized-run rejection."""
import unittest
from recognition_groups import propose


def contact(start=1,**kwargs):
    return dict({'session_id':'s','start_seq':start,'end_seq':start+1,'start_us':start*10000,
        'end_us':(start+1)*10000,'points':2,'kind':'pen_contact_pen','provisional':False,
        'tool_type':1,'pointer_id':0,'window_id':1,'display_id':0,
        'location_evidence':{'scope':'within_bound_page_candidate','page_id':'p',
            'source_pdf_sha256':'a'*64,'pdf_page_1based':1}},**kwargs)


class GroupChecks(unittest.TestCase):
    def test_partial_first_character_is_kept_whole(self):
        groups=propose([contact(location_evidence={'scope':'partial_or_unknown'}),contact(3)],'s')
        self.assertEqual(groups[0]['start_sequences'],[1,3])
        self.assertEqual(groups[0]['input_mode_candidate'],'screen')
        self.assertEqual(groups[0]['page_incomplete_contacts'],1)
        self.assertFalse(groups[0]['semantic_grouping_verified'])

    def test_nonwriting_and_provisional_contacts_cannot_be_silently_skipped(self):
        for change in ({'kind':'lasso_path_candidate'},{'kind':'erase_candidate_mode_unknown'},
                       {'kind':'pen_contact_tool_unknown'},{'provisional':True}):
            groups=propose([contact(),contact(3,**change),contact(5)],'s')
            self.assertEqual([g['start_sequences'] for g in groups],[[1],[5]])
            self.assertEqual(groups[0]['right_boundary'],'unqualified_contact_or_input_boundary')

    def test_gaps_context_page_and_time_are_boundaries(self):
        variants=[contact(4),contact(3,window_id=9),contact(3,start_us=1,end_us=2),
                  contact(3,start_us=3_000_000,end_us=3_100_000),
                  contact(3,location_evidence={'scope':'within_bound_page_candidate','page_id':'q'})]
        for other in variants:self.assertEqual(len(propose([contact(),other],'s')),2)

    def test_oversized_run_is_rejected_whole_not_truncated_into_partial_formula(self):
        g=propose([contact(i*2+1) for i in range(65)],'s')[0]
        self.assertEqual(g['contact_count'],65);self.assertEqual(g['budget_rejection'],'contact_budget')
        self.assertEqual(len(g['start_sequences']),65)

    def test_invalid_ledger_and_order_rejected(self):
        for events in ([contact(session_id='other')],[contact(points=1)],
                       [contact(end_seq=0)],[contact(3),contact()], [contact(tool_type=0)]):
            with self.assertRaises(ValueError):propose(events,'s')

    def test_distant_regions_flagged_without_discarding_or_splitting_contacts(self):
        g=propose([contact(bounds=[0,0,10,20]),contact(3,bounds=[0,600,20,620])],'s')[0]
        self.assertEqual(g['start_sequences'],[1,3])
        self.assertTrue(g['spatial_quality']['spatially_disconnected'])
        self.assertEqual(g['spatial_quality']['screen_bbox_components'],2)
        g=propose([contact(bounds=[0,0,10,20]),contact(3,bounds=[0,40,20,60])],'s')[0]
        self.assertFalse(g['spatial_quality']['spatially_disconnected'])


if __name__=='__main__':unittest.main(verbosity=2)

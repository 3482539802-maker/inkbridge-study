"""Selected page context tests; no tablet commands or pixels."""
from copy import deepcopy
import unittest
from coordinate_context_check import reconcile
from verify_coordinate_alignment import frame


def scene():
    f=frame(1000)
    o=f['observation']
    o['context']['document_title_candidate']='note'
    o.update(valid=False,reason='window_title_or_page_geometry_changed',
             context_before=deepcopy(o['context']),window_before=deepcopy(o['window']))
    o['context']['page_layers'][0]['unclipped_window_bounds']=[.17,0,1000.17,800]
    f['registration']['support_hull_screen_px']=[[100,100],[900,100],[900,700],[100,700]]
    binding={'title':'note','note_id':'note','source_pdf_sha256':'a'*64,
             'source_hinote_sha256':'b'*64,'pages':{'page':1}}
    return f,binding


class ContextChecks(unittest.TestCase):
    def test_subpixel_rounding_has_explicit_two_context_native_evidence(self):
        f,b=scene();result=reconcile(f,b)
        self.assertTrue(result['observation']['valid'])
        self.assertTrue(result['registration']['valid'])
        self.assertFalse(f['observation']['valid'])
        self.assertFalse(result['observation']['context_validation']['rectangle_used_for_transform'])

    def test_missing_original_context_or_wrong_focus_title_and_page_fail(self):
        for failure in ('missing','focus','title','page','motion','visual_fit','window','reason'):
            f,b=scene();o=f['observation']
            if failure=='missing':o.pop('context_before')
            elif failure=='focus':o['window']['focused']=False
            elif failure=='title':o['context_before']['document_title_candidate']='other'
            elif failure=='page':o['context_before']['page_layers'][0]['page_id']='other'
            elif failure=='motion':o['context']['page_layers'][0]['unclipped_window_bounds'][0]=.26
            elif failure=='visual_fit':f['registration']['valid']=False
            elif failure=='window':o['window']['window_id']+=1
            else:o['reason']='notes_not_focused'
            self.assertFalse(reconcile(f,b)['observation']['valid'],failure)


if __name__=='__main__':unittest.main(verbosity=2)

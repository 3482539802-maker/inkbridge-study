"""Native snapshot page selection must never invent geometry or cross identities."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from coordinate_page_binding import binding_from_inspection,bind_frame as select_page
from verify_coordinate_alignment import frame


def binding():
    return {'source_hinote_sha256':'b'*64,'note_id':'note',
        'title':'test','source_pdf_sha256':'a'*64,'pages':{'page':1,'second':2},'live_database_subscription':False}


class PageBindingChecks(unittest.TestCase):
    def test_multiple_visible_pages_selects_only_exact_pdf_background(self):
        row=frame(11000)
        pages=row['observation']['context']['page_layers']
        pages.append({'page_id':'second','unclipped_window_bounds':[0,800,1000,1600]})
        before=deepcopy(row)
        selected=select_page(row,binding())
        self.assertEqual(row,before)
        context=selected['observation']['context']
        self.assertEqual(len(context['visible_page_layers']),2)
        self.assertEqual(context['page_layers'][0]['page_id'],'page')
        self.assertEqual(len(context['page_layers']),1)
        self.assertEqual(selected['registration']['pdf_to_screen'],row['registration']['pdf_to_screen'])
        self.assertFalse(selected['registration']['native_background_binding']['live_database_subscription'])

    def test_unknown_duplicate_wrong_pdf_or_title_rejected(self):
        for mode in ('unknown','duplicate','pdf','title','page'):
            row=frame(11000)
            if mode=='unknown':row['observation']['context']['page_layers'][0]['page_id']='unknown'
            elif mode=='duplicate':row['observation']['context']['page_layers']*=2
            elif mode=='pdf':row['registration']['source_pdf_sha256']='c'*64
            elif mode=='title':row['observation']['context']['document_title_candidate']='other'
            else:row['registration']['pdf_page_1based']=2
            self.assertFalse(select_page(row,binding())['registration']['valid'])

    def test_invalid_visual_fit_is_never_revived(self):
        row=frame(11000)
        row['registration'].update(valid=False,reason='wrong_geometry')
        self.assertEqual(select_page(row,binding()),row)

    def test_export_requires_single_note_verified_files_and_original_pdf(self):
        record={'sha256':'b'*64,'documents':[{'note_id':'note','title':'test',
            'attachment':[{'id':'pdf','filePath':'test.pdf'}]}],
            'attachment_verification':[{'verified':True,'name':'test.pdf','sha256':'a'*64}],
            'pages':[{'note_id':'note','page_id':'page','background_attachment_id':'pdf','background_attachment_index':0}]}
        self.assertEqual(binding_from_inspection(record,'a'*64,7)['pages'],{'page':1})
        with self.assertRaises(ValueError):binding_from_inspection(record,'c'*64,7)
        for mode in ('note','attachment','duplicate'):
            bad=deepcopy(record)
            if mode=='note':bad['documents']*=2
            elif mode=='attachment':bad['attachment_verification'][0]['verified']=False
            else:bad['pages']*=2
            with self.assertRaises(ValueError):binding_from_inspection(bad,'a'*64,7)


if __name__=='__main__':unittest.main(verbosity=2)

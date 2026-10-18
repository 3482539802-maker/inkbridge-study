"""Document changes may select originals, never bypass identity/visual gates."""
import unittest
from unittest.mock import Mock,patch
from pipeline_documents import AdditionalDocuments,configuration_extends
from screen_frame_probe import NativeProbeFrames


class DocumentChecks(unittest.TestCase):
    def test_moving_exposure_selects_additional_original_before_fitting(self):
        from verify_motion_exposure import payload
        captured, _ = payload()
        captured.observation['independent_page_exposure_requested'] = True
        for key in ('context', 'context_before'):
            captured.observation[key]['document_title_candidate'] = 'tutorial'
            captured.observation[key]['page_layers'][0]['page_id'] = 'live-page'
        registry = self.registry()
        secondary = Mock()
        with patch('pipeline_documents.PDFRegistration', return_value=secondary), patch('pipeline_documents.capture_geometry') as capture:
            registry.process(captured, Mock(), Mock(), 'original', None, {})
            self.assertIs(capture.call_args.args[1], secondary)
            self.assertEqual(capture.call_args.args[2], 'tutorial')
        registry.close()

    def registry(self):
        binding={'title':'tutorial','pages':{'live-page':280,'next-page':281},
                 'source_pdf_sha256':'a'*64,'source_hinote_sha256':'b'*64}
        with patch('pipeline_documents.load_binding',return_value=binding):
            return AdditionalDocuments([('tutorial.pdf','tutorial.hinote')],'original',True)

    def test_visible_uuid_selects_page_280_and_updates_without_full_book_cache(self):
        registry=self.registry();registration=Mock()
        context={'document_title_candidate':'tutorial','active_tab_marker_unique':True,
                 'page_layers':[{'page_id':'live-page'}]}
        with patch('pipeline_documents.PDFRegistration',return_value=registration) as factory:
            selected,binding=registry.select(context)
            self.assertIs(selected,registration)
            self.assertEqual(factory.call_args.args[1],[279])
            context['page_layers']=[{'page_id':'next-page'}]
            registry.select(context)
            factory.assert_called_once()
            self.assertEqual(registration.pages,[280])
        registry.close();registration.close.assert_called_once()

    def test_unknown_page_or_ambiguous_title_never_builds_reference(self):
        registry=self.registry()
        for title,unique,ids in [('other',True,['live-page']),('tutorial',False,['live-page']),
                                  ('tutorial',True,['unknown']),('tutorial',True,['live-page']*2)]:
            with patch('pipeline_documents.PDFRegistration') as factory:
                with self.assertRaises(ValueError):registry.select({'document_title_candidate':title,
                    'active_tab_marker_unique':unique,'page_layers':[{'page_id':i} for i in ids]})
                factory.assert_not_called()

    def test_configuration_can_add_but_never_replace_or_remove_sources(self):
        base={'pdf_sha256':'a','page_margins':True}
        extended=dict(base,additional_documents=[{'pdf_sha256':'b','hinote_sha256':'c'}])
        self.assertTrue(configuration_extends(base,extended))
        self.assertFalse(configuration_extends(extended,base))
        self.assertFalse(configuration_extends(base,dict(extended,pdf_sha256='other')))
        self.assertFalse(configuration_extends(extended,dict(base,additional_documents=[{'pdf_sha256':'d'}])))

    def test_unlisted_title_does_not_request_a_frame(self):
        frames=NativeProbeFrames.__new__(NativeProbeFrames)
        frames.window_context=Mock(return_value=({'focused':True},
            {'active_tab_marker_unique':True,'document_title_candidate':'private-other'}))
        frames.send_text=Mock()
        image,observation=frames.capture(('original','tutorial'))
        self.assertIsNone(image)
        self.assertEqual(observation['reason'],'different_or_ambiguous_document')
        frames.send_text.assert_not_called()


if __name__=='__main__':unittest.main(verbosity=2)

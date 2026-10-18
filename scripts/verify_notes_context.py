"""Check that context/PDF errors cannot silently select the wrong problem page."""
import copy
from datetime import datetime, timedelta, timezone
import json
import gzip
import hashlib
from pathlib import Path
import tempfile
import unittest
import zipfile

import fitz

from notes_context import parse_layout, apply_native_mapping
from notes_pdf import index_pdf, resolve_context, resolve_native
from inspect_hinote import inspect

ROOT = Path(__file__).resolve().parents[1]


def node(identifier='', text='', children=(), kind='Text', **attrs):
    return {'attributes': {'id': identifier, 'text': text, 'type': kind, **attrs},
            'children': list(children)}


def layout(page='2', total='3'):
    return node(children=[
        node('FirstToolBarNoteTitleOuterLayer', children=[
            node(kind='SymbolGlyph'), node('FirstToolBarNoteTitleText', 'Book')]),
        node(kind='Row', children=[node('paginationInput', page), node(text='/'), node(text=total)]),
        node('pageAllLayer_abc', bounds='[10,20][100,200]', origBounds='[10,-40][100,200]'),
        node('stylusStage_abc_999_1700000000000')])


class PageSafety(unittest.TestCase):
    def test_cache_slot_is_never_a_page_number(self):
        result = parse_layout(node(children=[node('stylusStage_abc_999_1700000000000')]))
        self.assertIsNone(result['current_page'])
        self.assertIsNone(result['document_title'])

    def test_visible_counter_and_bounds(self):
        result = parse_layout(layout())
        self.assertEqual((result['document_title'], result['current_page'], result['total_pages']), ('Book', 2, 3))
        self.assertEqual(result['visible_pages'][0]['original_bounds'], [10, -40, 100, 200])
        self.assertIsNone(result['captured_at_pc'])

    def test_hidden_ancestor_cannot_supply_stale_counter(self):
        tree = layout()
        tree['children'][1]['attributes']['visible'] = False
        self.assertIsNone(parse_layout(tree)['current_page'])

    def test_ambiguous_tab_and_page_rejected(self):
        tree = layout()
        tree['children'].append(copy.deepcopy(tree['children'][0]))
        tree['children'].append(node(text='当前页 3'))
        result = parse_layout(tree)
        self.assertIsNone(result['document_title'])
        self.assertIsNone(result['current_page'])

    def test_invalid_and_conflicting_pagination(self):
        for page, total in [('0', '3'), ('4', '3'), ('1', '0')]:
            self.assertIsNone(parse_layout(layout(page, total))['current_page'])
        tree = layout()
        tree['children'].append(node(children=[node('paginationInput', '1'), node(text='/'), node(text='3')]))
        self.assertIsNone(parse_layout(tree)['current_page'])

    def test_observed_snapshots_when_available(self):
        # Local private evidence is optional, never needed by a clean clone.
        checks = [('pdf-layout.json', 280, 322), ('notes-more.json', 108, None),
                  ('pdf-clean.json', None, None)]
        for name, page, total in checks:
            path = ROOT / 'output/notes-research' / name
            if path.exists():
                context = parse_layout(json.loads(path.read_bytes()))
                self.assertEqual((context['current_page'], context['total_pages']), (page, total))


class PDFSafety(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='inkbridge-pdf-test-')
        self.pdf = Path(self.temporary.name) / 'book.pdf'
        with fitz.open() as document:
            for number in range(1, 4):
                page = document.new_page(width=300, height=200)
                page.insert_text((20, 40), f'Problem page {number}')
            document.save(self.pdf)
        self.catalog = index_pdf(self.pdf, 'Book', 3, True)
        self.context = parse_layout(layout())
        self.context['captured_at_pc'] = datetime.now(timezone.utc).isoformat()

    def tearDown(self):
        self.temporary.cleanup()

    def test_exact_page_and_explicit_question(self):
        result = resolve_context(self.catalog, self.context, region=[10, 10, 250, 60], question='Q2')
        self.assertEqual(result['status'], 'page_resolved_question_explicitly_selected')
        self.assertIn('Problem page 2', result['page_text'])
        self.assertIn('Problem page 2', result['region_text'])
        self.assertEqual(result['question_evidence'], 'explicit_selection')

    def test_candidate_requires_page_order_confirmation(self):
        catalog = index_pdf(self.pdf, 'Book')
        self.assertEqual(resolve_context(catalog, self.context)['status'], 'candidate_page_alignment_unconfirmed')
        with self.assertRaises(ValueError):
            index_pdf(self.pdf, 'Book', 2, True)

    def test_wrong_document_total_page_or_stale_context_no_text(self):
        changes = [('document_title', 'Other', 'document_not_bound'),
                   ('total_pages', 4, 'notes_pdf_page_count_mismatch'),
                   ('current_page', None, 'page_missing_or_out_of_range'),
                   ('current_page', 4, 'page_missing_or_out_of_range'),
                   ('captured_at_pc', None, 'context_has_no_observation_time'),
                   ('captured_at_pc', (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), 'context_stale_or_clock_invalid')]
        for field, value, reason in changes:
            context = dict(self.context, **{field: value})
            result = resolve_context(self.catalog, context)
            self.assertIn(reason, result['reasons'])
            self.assertNotIn('page_text', result)

    def test_duplicate_titles_not_a_unique_file_binding(self):
        context = copy.deepcopy(self.context)
        context['open_tabs'].append(context['open_tabs'][0])
        self.assertIn('duplicate_document_titles', resolve_context(self.catalog, context)['reasons'])

    def test_source_changed_after_index_no_stale_text(self):
        self.pdf.write_bytes(self.pdf.read_bytes() + b'\n% changed\n')
        result = resolve_context(self.catalog, self.context)
        self.assertIn('source_pdf_changed_reindex_required', result['reasons'])
        self.assertNotIn('page_text', result)

    def test_outside_page_region_is_rejected(self):
        for region in ([0, 0, 400, 100], [20, 30, 10, 40], [0, 0, float('nan'), 10]):
            with self.assertRaises(ValueError):
                resolve_context(self.catalog, self.context, region=region)

    def native_fixture(self):
        native_path = Path(self.temporary.name) / 'book.hinote'
        pdf_data = self.pdf.read_bytes()
        book = {'customNoteContent': {'id': 'book-id', 'noteTitle': 'Book', 'attachment': [
            {'id': 'pdf-attachment', 'filePath': '/private/files/original_pdf'}]},
            'fileList': [{'name': 'original_pdf', 'hash': hashlib.sha256(pdf_data).hexdigest().upper()}]}
        # Notes page 2 deliberately refers to PDF page 3, proving pageNumber is not used.
        page = {'customNotePageContent': {'notesId': 'book-id', 'id': 'abc', 'pageNumber': 2,
                                          'bkgAttachmentId': 'pdf-attachment', 'bkgAttachmentIndex': 2,
                                          'pageElement': []}, 'fileList': []}
        with zipfile.ZipFile(native_path, 'w') as archive:
            archive.writestr('book.jhinote', gzip.compress(json.dumps(book).encode()))
            archive.writestr('pages/abc.jhinote', gzip.compress(json.dumps(page).encode()))
            archive.writestr('files/original_pdf', pdf_data)
        return native_path

    def test_native_original_hash_and_background_index(self):
        native = inspect(self.native_fixture())
        self.assertTrue(native['attachment_verification'][0]['verified'])
        self.assertEqual(native['embedded_pdfs'][0]['entry'], 'files/original_pdf')
        context = copy.deepcopy(self.context)
        context['current_page'] = None
        result = resolve_native(self.catalog, context, native)
        self.assertEqual(result['status'], 'visible_pdf_pages_resolved_from_native_snapshot')
        page = result['visible_pdf_pages'][0]
        self.assertEqual((page['notes_page_at_export'], page['pdf_page']), (2, 3))
        self.assertIn('Problem page 3', page['page_text'])

    def test_native_snapshot_does_not_replace_live_counter(self):
        context = copy.deepcopy(self.context)
        context['current_page'] = None
        result = apply_native_mapping(context, inspect(self.native_fixture()))
        self.assertIsNone(result['current_page'])
        self.assertEqual(result['snapshot_page_candidate'], 2)

    def test_native_unmatched_uuid_or_attachment_hash_has_no_text(self):
        native = inspect(self.native_fixture())
        context = copy.deepcopy(self.context)
        context['visible_pages'][0]['page_id'] = 'other'
        result = resolve_native(self.catalog, context, native)
        self.assertEqual(result['visible_pdf_pages'], [])
        self.assertIn('visible_canvas_uuid_not_in_native_note', result['reasons'])
        native['attachment_verification'][0]['sha256'] = 'wrong'
        result = resolve_native(self.catalog, self.context, native)
        self.assertEqual(result['visible_pdf_pages'], [])
        self.assertIn('embedded_original_pdf_hash_not_matched', result['reasons'])


if __name__ == '__main__':
    unittest.main(verbosity=2)

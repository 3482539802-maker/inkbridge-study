"""Verified extra Notes documents; visible UUIDs select bounded PDF references."""
from pathlib import Path

from coordinate_page_binding import load_binding
from observe_coordinate_session import capture_geometry
from visual_registration import PDFRegistration


def configuration_extends(previous, current):
    extendable = ('additional_documents', 'visual_discovery_titles')
    old = {k:v for k,v in previous.items() if k not in extendable}
    new = {k:v for k,v in current.items() if k not in extendable}
    if old != new:
        return False
    return (all(item in current.get('additional_documents', []) for item in previous.get('additional_documents', [])) and
            all(title in current.get('visual_discovery_titles', []) for title in previous.get('visual_discovery_titles', [])))


class AdditionalDocuments:
    def __init__(self, pairs, primary_title, local_refinement=False, discovery_titles=(), discovery_root=None):
        if len(pairs) > 3:
            raise ValueError('At most three additional verified documents')
        self.documents = {}
        self.configuration = []
        self.registration = None
        self.active_title = None
        self.local_refinement = local_refinement
        self.discovery = {}
        for pdf, hinote in pairs:
            pdf, hinote = Path(pdf).resolve(), Path(hinote).resolve()
            binding = load_binding(hinote, pdf)
            title = binding['title']
            if title == primary_title or title in self.documents:
                raise ValueError('Duplicate document title; refusing ambiguous identity')
            self.documents[title] = (pdf, binding)
            self.configuration.append({'pdf':str(pdf), 'hinote':str(hinote),
                'pdf_sha256':binding['source_pdf_sha256'],
                'hinote_sha256':binding['source_hinote_sha256'], 'title':title})
        for title in discovery_titles:
            if title not in self.documents or discovery_root is None:
                raise ValueError('Discovery requires an explicitly registered generated workbook')
            import fitz, hashlib
            from visual_page_discovery import VisualPageDiscovery
            pdf,binding=self.documents[title]
            with fitz.open(pdf) as document: count=len(document)
            name=hashlib.sha256(title.encode()).hexdigest()[:16]+'.json'
            self.discovery[title]=VisualPageDiscovery(binding,count,Path(discovery_root)/name)

    def select(self, context):
        title = context.get('document_title_candidate')
        if not context.get('active_tab_marker_unique') or title not in self.documents:
            raise ValueError('No unique registered document')
        pdf, binding = self.documents[title]
        discovery=self.discovery.get(title)
        if discovery is not None: binding=discovery.binding()
        ids = [p.get('page_id') for p in context.get('page_layers', [])]
        if (not 1 <= len(ids) <= 12 or None in ids or len(set(ids)) != len(ids)
                or (discovery is None and any(i not in binding['pages'] for i in ids))):
            raise ValueError('Visible page UUID is not in the verified export')
        pages = (list(range(discovery.page_count)) if discovery is not None else
                 sorted({binding['pages'][i]-1 for i in ids}))
        if self.active_title != title:
            self.close()
            self.registration = PDFRegistration(pdf, pages, local_refinement=self.local_refinement)
            self.active_title = title
        # Cache eviction remains bounded to 12 pages; the UUID selects which
        # originals to compare, never supplies a coordinate transform.
        self.registration.pages = pages
        return self.registration, binding

    def process(self, captured, frames, primary_registration, primary_title, layout, primary_binding):
        context = captured.observation.get('context') or {}
        title = context.get('document_title_candidate')
        from page_exposure import eligible
        if (not captured.observation.get('valid') and not eligible(captured.observation)) or title == primary_title:
            return capture_geometry(frames, primary_registration, primary_title, layout, primary_binding, False, captured)
        try:
            registration, binding = self.select(context)
        except ValueError as error:
            captured.image = None
            observation = dict(captured.observation, valid=False, reason='unbound_document_or_page')
            return observation, {'valid':False, 'reason':'unbound_document_or_page', 'detail':str(error)}, captured.acquired
        if title in self.discovery:
            return self.discovery[title].process(captured,registration)
        return capture_geometry(frames, registration, title, None, binding, False, captured)

    def close(self):
        if self.registration is not None:
            self.registration.close()
        self.registration = None
        self.active_title = None

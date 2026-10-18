"""Identify remapped native page UUIDs from two independent original-PDF fits.

Only opt-in generated workbooks of at most twelve pages. UI boxes select/veto
feature support, never derive transforms. No screenshots requested or retained.
"""
from copy import deepcopy
import hashlib
from pathlib import Path

from coordinate_page_binding import bind_frame, rectangle
from geometry_pixel_mapping import lift_registration
from multipage_geometry import REASON
from visual_calibration import write_bounded_json


class VisualPageDiscovery:
    def __init__(self, binding, page_count, output):
        if not 1 <= page_count <= 12: raise ValueError('Discovery requires 1..12 source pages')
        self.source = deepcopy(binding); self.page_count = page_count; self.output = Path(output)
        self.pages = {}; self.pending = {}; self.blocked = set(); self.evidence = {}
        self.dirty = False
        # The app may also rebuild the note ID. This is explicitly a local
        # association identity, never a claimed observed private database ID.
        self.local_id = 'visual-'+hashlib.sha256((binding['source_pdf_sha256']+binding['title']).encode()).hexdigest()[:32]

    def binding(self, pages=None):
        return dict(self.source, pages=dict(self.pages if pages is None else pages), note_id=self.local_id,
                    binding_origin='two_original_visual_fits', live_note_id_verified=False)

    def publish(self):
        self.output.parent.mkdir(parents=True,exist_ok=True)
        write_bounded_json(self.output, {'schema':'inkbridge-visual-page-binding-v1',
            'source_pdf_sha256':self.source['source_pdf_sha256'],
            'source_hinote_sha256':self.source['source_hinote_sha256'], 'title':self.source['title'],
            'local_association_id':self.local_id, 'pages':self.pages, 'evidence':self.evidence,
            'blocked_page_ids':sorted(self.blocked), 'live_note_id_verified':False,
            'tablet_ui_actions':[], 'image_files_retained':0})

    def process(self, captured, registration):
        image, observation = captured.image, captured.observation
        captured.image = None
        failure = {'valid':False,'reason':'visual_page_identity_not_yet_confirmed'}
        context = observation.get('context') or {}; layers = context.get('page_layers', [])
        if (image is None or not observation.get('valid') or not observation.get('window',{}).get('focused') or
            not context.get('active_tab_marker_unique') or context.get('document_title_candidate') != self.source['title'] or
            registration.sha256 != self.source['source_pdf_sha256'] or not 1 <= len(layers) <= 12):
            self.pending.clear()
            return observation, failure, captured.acquired
        ids = [layer.get('page_id') for layer in layers]
        if None in ids or len(ids) != len(set(ids)): return observation, failure, captured.acquired
        try:
            regions = [rectangle(layer) for layer in layers]
            for i, region in enumerate(regions):
                for other in regions[i+1:]:
                    if max(region[0],other[0]) < min(region[2],other[2]) and max(region[1],other[1]) < min(region[3],other[3]):
                        raise ValueError('Overlapping live page regions')
        except ValueError: return observation, failure, captured.acquired
        native = observation.get('native_frame'); reduction = native.get('pixel_reduction',1) if native else 1
        if reduction not in (1,2): raise ValueError('Invalid native reduction')
        offset = (reduction-1)/2; candidates=[]
        window=observation.get('window',{})
        identity = (window.get('pid'),window.get('window_id'),window.get('display_id'),context.get('document_title_candidate'))
        fits=[]
        for page_id, region in zip(ids, regions):
            if page_id in self.blocked: continue
            search_pages = [self.pages[page_id]-1] if page_id in self.pages else list(range(self.page_count))
            fitted = registration.match(image, pages=search_pages,
                                        screen_region=[(v-offset)/reduction for v in region])
            if native: fitted = lift_registration(fitted,native,image.shape)
            number=fitted.get('pdf_page_1based')
            if not fitted.get('valid') or type(number) is not int or not 1 <= number <= self.page_count:
                self.pending.pop(page_id,None); continue
            temporary = self.binding({page_id:number})
            bound = bind_frame({'observation':deepcopy(observation),'registration':fitted},temporary)
            if not bound['registration'].get('valid'):
                self.pending.pop(page_id,None); continue
            fits.append((page_id,number,bound))
        # Repeated page templates or multiple UUIDs mapping to the same page
        # cannot establish a unique workbook identity.
        numbers=[n for _,n,_ in fits]
        if len(numbers) != len(set(numbers)): return observation, dict(failure,reason='duplicate_visual_page_assignment'), captured.acquired
        changed=False
        for page_id,number,bound in fits:
            if page_id in self.pages and self.pages[page_id] != number:
                self.blocked.add(page_id);self.pages.pop(page_id,None);self.evidence.pop(page_id,None);changed=True;continue
            if any(other != page_id and n == number for other,n in self.pages.items()):
                # Reimported copy: never reuse an old note mapping by title.
                continue
            previous=self.pending.get(page_id)
            sample={'page':number,'time':captured.acquired,'identity':identity,
                    'heldout_median_error_screen_px':bound['registration'].get('heldout_median_error_screen_px'),
                    'heldout_inlier_p90_error_screen_px':bound['registration'].get('heldout_inlier_p90_error_screen_px')}
            self.pending[page_id]=sample
            agreed=(previous and previous['page']==number and previous['identity']==identity and
                    .1 <= captured.acquired-previous['time'] <= 5)
            if page_id not in self.pages and not agreed: continue
            if page_id not in self.pages:
                self.pages[page_id]=number;self.evidence[page_id]={'first':previous,'second':sample};changed=True
            evidence=bound['registration']['native_background_binding']
            evidence.update(method='two_independent_original_pdf_fits_and_live_canvas_uuid',
                            live_note_id_verified=False,exported_page_uuid_verified=False,
                            identification_evidence=self.evidence[page_id])
            candidates.append({'context':bound['observation']['context'],'registration':bound['registration']})
        self.dirty = self.dirty or changed
        if self.dirty:
            try:
                self.publish()
                self.dirty = False
            except OSError:
                # A local derived-file failure must not revoke an authorized
                # capture. Revalidate and retry on the next already acquired frame.
                return observation,dict(failure,reason='visual_identity_storage_unavailable'),captured.acquired
        if len(layers)==1 and candidates:
            result=deepcopy(observation);result['context']=candidates[0]['context']
            return result,candidates[0]['registration'],captured.acquired
        if len(layers)==1: return observation,failure,captured.acquired
        return observation, {'valid':False,'reason':REASON,'source_pdf_sha256':self.source['source_pdf_sha256'],
                             'page_candidates':candidates,'rejected_pages':[],
                             'rectangle_used_for_transform':False,'physical_pen_accuracy_verified':False}, captured.acquired

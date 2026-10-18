"""Recognize PDF page geometry from pixels. No OCR, UI actions, or image files."""
from collections import OrderedDict
from copy import deepcopy
import hashlib
from pathlib import Path

import cv2
import fitz
import numpy as np


def gray_scaled(image, max_side=1600):
    if image is None or image.size == 0:
        raise ValueError('Empty image')
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    scale = min(1., max_side/max(gray.shape))
    if scale < 1:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return np.ascontiguousarray(gray), scale


def project(matrix, points):
    points = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    return points @ np.asarray(matrix)[:, :2].T + np.asarray(matrix)[:, 2]


def compose(a,b):
    return (np.vstack([a,[0,0,1]]) @ np.vstack([b,[0,0,1]]))[:2]


def resize_to_original(work_shape,image_shape):
    # OpenCV maps pixel centres, and rounds each resized dimension separately.
    sx,sy=image_shape[1]/work_shape[1],image_shape[0]/work_shape[0]
    return np.array([[sx,0,(sx-1)/2],[0,sy,(sy-1)/2]],np.float64)


def robust_refit(source,target,initial):
    """Refine only the training inliers, without leaking held-out observations."""
    source,target=np.asarray(source,np.float64),np.asarray(target,np.float64)
    keep=np.linalg.norm(project(initial,source)-target,axis=1)<=3
    source,target=source[keep],target[keep]
    if len(source)<9:return initial
    centre=np.mean(source,axis=0)
    spread=np.std(source,axis=0)
    if min(spread)<1:return initial
    design=np.column_stack([(source-centre)/spread,np.ones(len(source))])
    weight=np.ones(len(source))
    for _ in range(5):
        solution=np.linalg.lstsq(design*np.sqrt(weight[:,None]),target*np.sqrt(weight[:,None]),rcond=None)[0]
        error=np.linalg.norm(design@solution-target,axis=1)
        sigma=max(.15,float(np.median(error))*1.4826)
        weight=np.minimum(1,1.5*sigma/np.maximum(error,1e-9))
    matrix=solution.T
    matrix[:,:2]/=spread
    matrix[:,2]-=matrix[:,:2]@centre
    return matrix


def refine_pixels(reference,screen,matrix,hull):
    """Independent local patches refine the affine; hold-out patches veto it.

    Images live only in this call. Repeated text cannot authorize a large jump.
    Residuals describe image alignment, not input digitizer or saved ink accuracy.
    """
    gray=cv2.cvtColor(screen,cv2.COLOR_BGR2GRAY) if screen.ndim==3 else screen
    predicted=cv2.warpAffine(reference,matrix,(gray.shape[1],gray.shape[0]),borderValue=255)
    hull=np.float32(hull)
    rows=[]
    half=64
    # At most 48 small patches, including spatially separate validation patches.
    minimum=np.maximum(np.min(hull,axis=0),[0,0])+half+2
    maximum=np.minimum(np.max(hull,axis=0),[gray.shape[1],gray.shape[0]])-half-2
    if np.any(maximum<=minimum):return None
    for y in np.linspace(minimum[1],maximum[1],6):
        for x in np.linspace(minimum[0],maximum[0],8):
            x,y=int(x),int(y)
            if any(cv2.pointPolygonTest(hull,(float(xx),float(yy)),False)<0
                   for xx,yy in ((x-half,y-half),(x+half,y-half),(x-half,y+half),(x+half,y+half))):continue
            a=predicted[y-half:y+half,x-half:x+half]
            b=gray[y-half:y+half,x-half:x+half]
            if a.std()<18 or b.std()<18:continue
            correction=np.eye(2,3,dtype=np.float32)
            try:
                correlation,correction=cv2.findTransformECC(a,b,correction,cv2.MOTION_TRANSLATION,
                    (cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_COUNT,35,1e-4),None,3)
            except cv2.error:continue
            delta=correction[:,2].astype(np.float64)
            if correlation<.92 or np.linalg.norm(delta)>2:continue
            rows.append([x,y,*delta,correlation])
    if len(rows)<12:return None
    rows=np.array(rows)
    held=np.arange(len(rows))%4==0
    source=rows[:,:2]
    target=source+rows[:,2:4]
    correction=robust_refit(source[~held],target[~held],np.eye(2,3))
    before=np.linalg.norm(rows[held,2:4],axis=1)
    after=np.linalg.norm(project(correction,source[held])-target[held],axis=1)
    span=np.ptp(source[held],axis=0)
    if (min(span)<200 or np.percentile(after,90)>.6 or
        np.median(after)>np.median(before)+.03):return None
    corners=[[0,0],[gray.shape[1],0],[0,gray.shape[0]],[gray.shape[1],gray.shape[0]]]
    if max(np.linalg.norm(project(correction,corners)-corners,axis=1))>2:return None
    return compose(correction,matrix),{'method':'local_translation_patches_then_affine',
        'patches':len(rows),'heldout_patches':int(sum(held)),
        'heldout_before_median_screen_px':float(np.median(before)),
        'heldout_after_median_screen_px':float(np.median(after)),
        'heldout_after_p90_screen_px':float(np.percentile(after,90)),
        'median_patch_correlation':float(np.median(rows[:,4])),
        'native_pen_accuracy_verified':False}


def fit_geometry(source, target):
    """Split matches before RANSAC; reject local/repeated-text/implausible fits."""
    source, target = np.float32(source), np.float32(target)
    if len(source) < 30:
        return None
    held = np.arange(len(source)) % 5 == 0
    matrix, mask = cv2.estimateAffine2D(source[~held], target[~held],
        method=cv2.RANSAC, ransacReprojThreshold=3., maxIters=3000, confidence=.995)
    if matrix is None:
        return None
    matrix=robust_refit(source[~held],target[~held],matrix)
    sx, sy = matrix[0, 0], matrix[1, 1]
    if not (.12 < sx < 12 and .12 < sy < 12 and .45 < sx/sy < 2.2):
        return None
    if abs(matrix[0, 1]) > .03*sy or abs(matrix[1, 0]) > .03*sx:
        return None  # No unvalidated rotation/perspective inversion.
    errors = np.linalg.norm(project(matrix, source) - target, axis=1)
    inliers = errors <= 3
    test = errors[held]
    if sum(inliers) < 24 or np.mean(inliers) < .45 or sum(test <= 3) < 5:
        return None
    good = target[inliers]
    area = float(cv2.contourArea(cv2.convexHull(good)))
    span = np.ptp(good, axis=0)
    if area < 12000 or min(span) < 100:
        return None
    # The independent subset must agree; never report a training residual as accuracy.
    held_good = test[test <= 3]
    if np.median(test) > 3 or np.percentile(held_good, 90) > 2.5:
        return None
    return {'matrix': matrix, 'inliers': int(sum(inliers)), 'matches': len(source),
            'heldout_matches': int(sum(held)), 'heldout_inliers': int(sum(test <= 3)),
            'heldout_median_error_work_px': float(np.median(test)),
            'heldout_inlier_p90_error_work_px':float(np.percentile(held_good,90)),
            'heldout_inlier_fraction':float(np.mean(test<=3)),
            'support_area_work_px2': area,
            'support_hull_work_px': cv2.convexHull(good).reshape(-1, 2)}


class PDFRegistration:
    """At most twelve page feature tables, one live image, no history of pixels."""
    def __init__(self, path, pages=None, precision=False, local_refinement=False):
        if precision and local_refinement:
            raise ValueError('Choose precision or local refinement mode')
        self.path = Path(path).resolve()
        self.signature = (self.path.stat().st_size, self.path.stat().st_mtime_ns)
        with self.path.open('rb') as source:
            self.sha256 = hashlib.file_digest(source, 'sha256').hexdigest()
        self.document = fitz.open(self.path)
        self.pages = list(pages if pages is not None else range(min(12, len(self.document))))
        if not self.pages or len(self.pages) > 12 or any(p < 0 or p >= len(self.document) for p in self.pages):
            raise ValueError('Choose 1..12 valid PDF pages per recognition cycle')
        # Dense repeated formula text needs more than the strongest few glyphs.
        # Keep the table bounded; reciprocal matching then removes one-way aliases.
        self.precision=precision
        self.local_refinement=local_refinement
        self.reference_max_side=2200 if precision or local_refinement else 1400
        self.feature_max_side=2200 if precision else 1400
        self.screen_max_side=2800 if precision else 1600
        self.sift = cv2.SIFT_create(nfeatures=10000 if precision else 6400)
        self.cache = OrderedDict()
        self.render_transforms={}
        # Cache only immutable original-PDF rasters, never screen frames.
        self.pixel_cache=OrderedDict()
        self.pixel_cache_bytes=0
        self.pixel_cache_limit=32*1024*1024

    def close(self):
        self.cache.clear()
        self.render_transforms.clear()
        self.pixel_cache.clear()
        self.pixel_cache_bytes=0
        self.document.close()

    def reference(self, page):
        if page in self.cache:
            self.cache.move_to_end(page)
            return self.cache[page]
        image,pdf_to_raster,kind=self.reference_pixels(page)
        features,_=gray_scaled(image,self.feature_max_side)
        key, desc = self.sift.detectAndCompute(features, None)
        pdf_to_raster=compose(cv2.invertAffineTransform(resize_to_original(features.shape,image.shape)),pdf_to_raster)
        value = (key, desc, (pdf_to_raster[0,0],pdf_to_raster[1,1]))
        self.render_transforms[page]=pdf_to_raster
        self.cache[page] = value
        while len(self.cache) > 12:
            removed,_=self.cache.popitem(last=False)
            self.render_transforms.pop(removed,None)
        return value

    def reference_pixels(self,page):
        if self.signature != (self.path.stat().st_size,self.path.stat().st_mtime_ns):
            raise ValueError('Source PDF changed; recreate registration')
        if page in self.pixel_cache:
            self.pixel_cache.move_to_end(page)
            return self.pixel_cache[page]
        result=self._reference_pixels(page)
        result[0].setflags(write=False)
        result[1].setflags(write=False)
        self.pixel_cache[page]=result
        self.pixel_cache_bytes+=result[0].nbytes
        while self.pixel_cache_bytes>self.pixel_cache_limit:
            _,removed=self.pixel_cache.popitem(last=False)
            self.pixel_cache_bytes-=removed[0].nbytes
        return result

    def _reference_pixels(self,page):
        p=self.document[page]
        infos=p.get_image_info(xrefs=True)
        # Single full-page scans can use the embedded pixels directly, avoiding
        # a second PDF rasterization and its scale-dependent sampling phase.
        if len(infos)==1 and infos[0]['xref'] and not p.rotation and not p.get_text().strip() and not p.get_drawings():
            info=infos[0]
            a,b,c,d,e,f=info['transform']
            rect=info['bbox']
            if (abs(b)+abs(c)<1e-6 and a>0 and d>0 and
                max(abs(x-y) for x,y in zip(rect,p.rect))<.01):
                pix=fitz.Pixmap(self.document,info['xref'])
                if pix.colorspace and pix.colorspace.n!=3:pix=fitz.Pixmap(fitz.csRGB,pix)
                array=np.frombuffer(pix.samples,np.uint8).reshape(pix.height,pix.width,pix.n)
                image=cv2.cvtColor(array[:,:,:3],cv2.COLOR_RGB2GRAY)
                # get_pixmap supplies a soft mask only through a separate xref.
                mask=next((i[1] for i in p.get_images() if i[0]==info['xref']),0)
                if mask:
                    alpha=fitz.Pixmap(self.document,mask)
                    alpha=np.frombuffer(alpha.samples,np.uint8).reshape(alpha.height,alpha.width)
                    image=np.rint(image.astype(np.float32)*(alpha/255.)+255-alpha).astype(np.uint8)
                matrix=np.array([[pix.width/a,0,-e*pix.width/a-.5],
                                 [0,pix.height/d,-f*pix.height/d-.5]],np.float64)
                work,_=gray_scaled(image,self.reference_max_side)
                return work,compose(cv2.invertAffineTransform(resize_to_original(work.shape,image.shape)),matrix),'embedded_full_page_scan'
        render_scale=self.reference_max_side/max(p.rect.width,p.rect.height)
        pix=p.get_pixmap(matrix=fitz.Matrix(render_scale,render_scale),colorspace=fitz.csGRAY)
        image=np.frombuffer(pix.samples,np.uint8).reshape(pix.height,pix.width)
        matrix=np.array([[render_scale,0,-pix.x-.5],[0,render_scale,-pix.y-.5]],np.float64)
        return image,matrix,'pdf_render'

    def match(self, image, pages=None, screen_region=None):
        if self.signature != (self.path.stat().st_size, self.path.stat().st_mtime_ns):
            raise ValueError('Source PDF changed; recreate registration')
        work, scale = gray_scaled(image,self.screen_max_side)
        original=resize_to_original(work.shape,image.shape)
        mask = None
        if screen_region is not None:
            region = np.asarray(screen_region, dtype=np.float64)
            if (region.shape != (4,) or not np.isfinite(region).all() or
                    region[0] >= region[2] or region[1] >= region[3]):
                raise ValueError('Invalid screen feature selection rectangle')
            # The rectangle selects eligible feature centres only. Translation
            # and scale still come from independent original-PDF matches.
            inverse = cv2.invertAffineTransform(original)
            corners = project(inverse, [region[:2], region[2:]])
            x0,y0 = np.maximum(0,np.ceil(corners[0])).astype(int)
            x1,y1 = np.minimum([work.shape[1],work.shape[0]],np.floor(corners[1])+1).astype(int)
            if x1 <= x0 or y1 <= y0:
                return {'valid':False,'reason':'page_feature_region_outside_screen'}
            mask = np.zeros(work.shape, dtype=np.uint8)
            mask[y0:y1,x0:x1] = 255
        key, desc = self.sift.detectAndCompute(work, mask)
        if desc is None or len(key) < 30:
            return {'valid': False, 'reason': 'insufficient_screen_features'}
        candidates = []
        matcher = cv2.BFMatcher(cv2.NORM_L2)
        for page in (pages if pages is not None else self.pages):
            rkey, rdesc, reference_scale = self.reference(page)
            if rdesc is None:
                continue
            pairs = matcher.knnMatch(rdesc, desc, k=2)
            reverse = matcher.knnMatch(desc, rdesc, k=2)
            best = {}
            for pair in pairs:
                if len(pair) != 2:
                    continue
                a, b = pair
                back = reverse[a.trainIdx]
                reciprocal = (len(back) == 2 and back[0].trainIdx == a.queryIdx
                              and back[0].distance < .66*back[1].distance)
                if reciprocal and a.distance < .66*b.distance and (a.trainIdx not in best or a.distance < best[a.trainIdx].distance):
                    best[a.trainIdx] = a
            matches = list(best.values())
            fit = fit_geometry([rkey[m.queryIdx].pt for m in matches], [key[m.trainIdx].pt for m in matches])
            if fit is None:
                continue
            matrix = fit.pop('matrix')
            # Render pixels -> PDF points, then work image -> original screen image.
            matrix=compose(original,compose(matrix,self.render_transforms[page]))
            hull = project(original,fit.pop('support_hull_work_px'))
            error_scale=max(original[0,0],original[1,1])
            candidates.append({'pdf_page_1based': page+1, 'pdf_to_screen': matrix.tolist(),
                'screen_to_pdf': cv2.invertAffineTransform(matrix).tolist(),
                'support_hull_screen_px': hull.tolist(), 'screen_size': [image.shape[1], image.shape[0]],
                'heldout_median_error_screen_px': fit['heldout_median_error_work_px']*error_scale,
                'heldout_inlier_p90_error_screen_px':fit['heldout_inlier_p90_error_work_px']*error_scale,
                'pdf_size_pt':[self.document[page].rect.width,self.document[page].rect.height],
                'pixel_geometry_version':2,'registration_mode':'local' if self.local_refinement else 'precision' if self.precision else 'balanced',
                'precision_is_empirical_not_pen_accuracy':True, **fit})
        candidates.sort(key=lambda c:c['inliers'], reverse=True)
        if not candidates:
            return {'valid': False, 'reason': 'no_supported_pdf_geometry'}
        if len(candidates) > 1 and candidates[1]['inliers'] >= .70*candidates[0]['inliers']:
            return {'valid': False, 'reason': 'ambiguous_multiple_pdf_pages',
                    'candidate_pages': [c['pdf_page_1based'] for c in candidates]}
        winner=candidates[0]
        if self.local_refinement:
            return self.refine_local(image,{'valid':True,'source_pdf_sha256':self.sha256,
                'source':'original_pdf_feature_registration','semantic_handwriting_recognition':False,**winner})
        if self.precision:
            page=winner['pdf_page_1based']-1
            reference,pdf_to_raster,kind=self.reference_pixels(page)
            pdf_to_screen=np.array(winner['pdf_to_screen'])
            raster_to_screen=compose(pdf_to_screen,cv2.invertAffineTransform(pdf_to_raster))
            refined=refine_pixels(reference,image,raster_to_screen,winner['support_hull_screen_px'])
            winner['reference_pixel_source']=kind
            winner['pixel_refinement']={'accepted':False,'reason':'insufficient_independent_pixel_support'}
            if refined:
                raster_to_screen,metrics=refined
                pdf_to_screen=compose(raster_to_screen,pdf_to_raster)
                winner.update(pdf_to_screen=pdf_to_screen.tolist(),
                    screen_to_pdf=cv2.invertAffineTransform(pdf_to_screen).tolist(),
                    pixel_refinement={'accepted':True,**metrics})
        return {'valid': True, 'source_pdf_sha256': self.sha256, 'source': 'original_pdf_feature_registration',
                'semantic_handwriting_recognition': False, **winner}

    def refine_local(self,image,coarse):
        """Correct a caller-supplied coarse fit on this image, without SIFT.

        Caller must establish current page/clock/context before coordinate use.
        This method retains no live images and never tracks across frames.
        """
        from local_geometry_refinement import refine_local_pixels
        if not coarse.get('valid'):
            return deepcopy(coarse)
        result=deepcopy(coarse)
        page=coarse.get('pdf_page_1based')
        if (coarse.get('source_pdf_sha256')!=self.sha256 or type(page) is not int or page-1 not in self.pages or
                coarse.get('screen_size')!=[image.shape[1],image.shape[0]] or coarse.get('pixel_geometry_version')!=2):
            return {'valid':False,'reason':'local_refinement_source_or_geometry_mismatch'}
        matrix=np.asarray(coarse.get('pdf_to_screen'),np.float64)
        inverse=np.asarray(coarse.get('screen_to_pdf'),np.float64)
        hull=np.asarray(coarse.get('support_hull_screen_px'),np.float64)
        if (matrix.shape!=(2,3) or inverse.shape!=(2,3) or hull.ndim!=2 or hull.shape[1]!=2 or
                not 3<=len(hull)<=512 or not all(np.isfinite(v).all() for v in (matrix,inverse,hull)) or
                max(np.linalg.norm(project(matrix,project(inverse,hull))-hull,axis=1))>.1):
            return {'valid':False,'reason':'local_refinement_invalid_matrix_or_support'}
        reference,pdf_to_raster,kind=self.reference_pixels(page-1)
        raster_to_screen=compose(matrix,cv2.invertAffineTransform(pdf_to_raster))
        diagnostics={}
        refined=refine_local_pixels(reference,image,raster_to_screen,hull,diagnostics)
        result.update(registration_mode='local',reference_pixel_source=kind,
            pixel_refinement={'accepted':False,'reason':'insufficient_independent_local_support',**diagnostics})
        if refined:
            transform,metrics=refined
            transform=compose(transform,pdf_to_raster)
            result.update(pdf_to_screen=transform.tolist(),screen_to_pdf=cv2.invertAffineTransform(transform).tolist(),
                          pixel_refinement={'accepted':True,**metrics})
        return result


def screen_point_to_pdf(registration, x, y):
    if not registration.get('valid'):
        raise ValueError('No valid visual calibration')
    hull = np.float32(registration['support_hull_screen_px'])
    if cv2.pointPolygonTest(hull, (float(x), float(y)), True) < -30:
        raise ValueError('Point outside verified image feature support')
    return project(registration['screen_to_pdf'], [[x, y]])[0].tolist()

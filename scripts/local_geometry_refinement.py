"""Bounded subpixel correction around a supplied coarse image transform.

No temporal tracking, screenshots, OCR, page selection or raw point mapping.
Only small reference patches are warped; live screen pixels are never cached.
"""
import cv2
import numpy as np

from visual_registration import compose, project, robust_refit


def refine_local_pixels(reference, screen, matrix, hull, diagnostics=None):
    diagnostics = diagnostics if diagnostics is not None else {}
    gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY) if screen.ndim == 3 else screen
    matrix = np.asarray(matrix, np.float64)
    hull = np.asarray(hull, np.float32).reshape(-1, 2)
    if (matrix.shape != (2, 3) or not np.isfinite(matrix).all() or
            not 3 <= len(hull) <= 512 or not np.isfinite(hull).all()):
        return None
    half = 64
    lower = np.maximum(np.min(hull, axis=0), [0, 0])+half+2
    upper = np.minimum(np.max(hull, axis=0), [gray.shape[1], gray.shape[0]])-half-2
    if np.any(upper <= lower):
        return None
    # Keep fit and validation image areas disjoint even on small supports.
    nx=min(10,int((upper[0]-lower[0])//(2*half+4))+1)
    ny=min(8,int((upper[1]-lower[1])//(2*half+4))+1)
    patches = []
    # Validation membership is fixed by position before measuring corrections.
    for iy, y in enumerate(np.linspace(lower[1], upper[1], ny)):
        for ix, x in enumerate(np.linspace(lower[0], upper[0], nx)):
            x, y = int(x), int(y)
            if any(cv2.pointPolygonTest(hull, (float(u), float(v)), False) < 0
                   for u, v in ((x-half,y-half),(x+half,y-half),(x-half,y+half),(x+half,y+half))):
                continue
            local = matrix.copy()
            local[:, 2] -= [x-half, y-half]
            a = cv2.warpAffine(reference, local, (2*half, 2*half), borderValue=255)
            b = gray[y-half:y+half, x-half:x+half]
            texture = min(float(a.std()), float(b.std()))
            if texture < 18:
                continue
            # Reject one-direction patterns that cannot measure both axes.
            gx = cv2.Sobel(a, cv2.CV_32F, 1, 0)
            gy = cv2.Sobel(a, cv2.CV_32F, 0, 1)
            tensor = np.array([[np.mean(gx*gx), np.mean(gx*gy)], [np.mean(gx*gy), np.mean(gy*gy)]])
            eigen = np.linalg.eigvalsh(tensor)
            if eigen[0] < .02*max(float(eigen[1]), 1.):
                continue
            patches.append((texture, (iy*10+ix)%7 < 2, x, y, a, b))
    # Bounded work, with validation reserved independently of fit residuals.
    chosen = []
    for held, limit in ((False, 24), (True, 12)):
        chosen.extend(sorted((p for p in patches if p[1] == held), key=lambda p:p[0], reverse=True)[:limit])
    diagnostics.update(candidate_patches=len(patches),attempted_patches=len(chosen))
    rows = []
    window=cv2.createHanningWindow((2*half,2*half),cv2.CV_32F)
    for _, held, x, y, a, b in chosen:
        correction = np.eye(2, 3, dtype=np.float32)
        # Phase correlation supplies a cheap local starting point; ECC and
        # held-out patches must still validate the result independently.
        shift,response=cv2.phaseCorrelate(a.astype(np.float32),b.astype(np.float32),window)
        if response>.15 and np.isfinite(shift).all() and np.linalg.norm(shift)<=2:
            correction[:,2]=shift
        try:
            correlation, correction = cv2.findTransformECC(a, b, correction, cv2.MOTION_TRANSLATION,
                (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 1e-4), None, 3)
        except cv2.error:
            continue
        delta = correction[:, 2].astype(np.float64)
        if not np.isfinite(delta).all() or correlation < .94 or np.linalg.norm(delta) > 2:
            continue
        rows.append([x, y, *delta, correlation, held])
    diagnostics.update(measured_patches=len(rows),heldout_patches=sum(bool(r[5]) for r in rows))
    if len(rows) < 12:
        diagnostics['reason']='too_few_local_measurements'
        return None
    rows = np.array(rows)
    held = rows[:, 5].astype(bool)
    if sum(held) < 4 or sum(~held) < 9:
        diagnostics['reason']='too_few_independent_measurements'
        return None
    source, target = rows[:, :2], rows[:, :2]+rows[:, 2:4]
    if any(min(np.ptp(source[subset], axis=0)) < 200 for subset in (held, ~held)):
        diagnostics['reason']='insufficient_spatial_spread'
        return None
    correction = robust_refit(source[~held], target[~held], np.eye(2, 3))
    before = np.linalg.norm(rows[held, 2:4], axis=1)
    after = np.linalg.norm(project(correction, source[held])-target[held], axis=1)
    if np.percentile(after, 90) > .35 or np.median(after) > np.median(before)+.02:
        diagnostics['reason']='heldout_local_fit_failed'
        return None
    corners = np.array([[0,0],[gray.shape[1],0],[0,gray.shape[0]],[gray.shape[1],gray.shape[0]]])
    max_correction = float(max(np.linalg.norm(project(correction, corners)-corners, axis=1)))
    if max_correction > 2:
        diagnostics['reason']='local_correction_exceeds_two_pixels'
        return None
    return compose(correction, matrix), {
        'method': 'bounded_local_reference_patches', 'patch_size': 2*half,
        'patches': len(rows), 'attempted_patches': len(chosen), 'heldout_patches': int(sum(held)),
        'heldout_before_median_screen_px': float(np.median(before)),
        'heldout_after_median_screen_px': float(np.median(after)),
        'heldout_after_p90_screen_px': float(np.percentile(after, 90)),
        'max_correction_screen_px': max_correction,
        'median_patch_correlation': float(np.median(rows[:,4])),
        'native_pen_accuracy_verified': False}

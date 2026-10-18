"""Lift a fit on deterministic reduced pixels to the original screen grid."""
from copy import deepcopy
import math


def lift_registration(result, metadata, image_shape):
    reduction=metadata.get('pixel_reduction',1)
    if type(reduction) is not int or reduction not in (1,2):
        raise ValueError('Unsupported deterministic pixel reduction')
    if reduction==1:return result
    width,height=metadata['width'],metadata['height']
    if width%2 or height%2 or (image_shape[1]*2,image_shape[0]*2)!=(width,height):
        raise ValueError('Pixel reduction dimensions disagree')
    if not result.get('valid'):return result
    output=deepcopy(result)
    if output['screen_size']!=[image_shape[1],image_shape[0]]:
        raise ValueError('Registration uses a different raster')
    import cv2
    import numpy as np
    from visual_registration import compose
    pixel_to_screen=np.array([[2.,0.,.5],[0.,2.,.5]],dtype=np.float64)
    matrix=compose(pixel_to_screen,np.array(output['pdf_to_screen'],dtype=np.float64))
    output.update(pdf_to_screen=matrix.tolist(),screen_to_pdf=cv2.invertAffineTransform(matrix).tolist(),
                  screen_size=[width,height])
    output['support_hull_screen_px']=[[x*2+.5,y*2+.5] for x,y in output['support_hull_screen_px']]
    for key in ('heldout_median_error_screen_px','heldout_inlier_p90_error_screen_px'):
        if key in output:output[key]*=2
    refinement=output.get('pixel_refinement',{})
    for key,value in refinement.items():
        if key.endswith('_screen_px') and isinstance(value,(float,int)) and not isinstance(value,bool):refinement[key]=value*2
    if refinement.get('accepted') and refinement.get('max_correction_screen_px',0)>2:
        output.update(valid=False,reason='reduced_refinement_exceeds_screen_correction_limit')
    # Apply the original physical-screen quality threshold after scaling.
    if not math.isfinite(output['heldout_median_error_screen_px']) or output['heldout_median_error_screen_px']>2:
        output.update(valid=False,reason='reduced_frame_exceeds_screen_error_threshold')
    output['pixel_mapping']={'method':'native_exact_2x2_box_pixel_centres',
        'raster_size':[image_shape[1],image_shape[0]],'screen_size':[width,height],
        'raster_to_screen':pixel_to_screen.tolist(),'physical_pen_accuracy_verified':False}
    return output

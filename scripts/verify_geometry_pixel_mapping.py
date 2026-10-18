"""Reduced-pixel fits preserve original screen units and quality gates."""
from copy import deepcopy
import unittest

from geometry_pixel_mapping import lift_registration
from coordinate_alignment import affine
from verify_coordinate_alignment import frame


class PixelMappingChecks(unittest.TestCase):
    def test_exact_box_pixel_centres_and_inverse_restore_screen_coordinates(self):
        source=frame(1000)['registration']
        source['screen_size']=[100,100]
        original=deepcopy(source)
        lifted=lift_registration(source,{'width':200,'height':200,'pixel_reduction':2},(100,100,3))
        for pdf in ((0,0),(10,20),(49,33)):
            raster=affine(source['pdf_to_screen'],*pdf)
            screen=affine(lifted['pdf_to_screen'],*pdf)
            self.assertEqual(screen,[2*x+.5 for x in raster])
            restored=affine(lifted['screen_to_pdf'],*screen)
            for x,y in zip(restored,pdf):self.assertAlmostEqual(x,y)
        self.assertEqual(lifted['heldout_median_error_screen_px'],source['heldout_median_error_screen_px']*2)
        self.assertEqual(lifted['screen_size'],[200,200])
        self.assertEqual(source,original)

    def test_quality_checked_in_physical_screen_units_and_bad_metadata_refused(self):
        source=frame(1000)['registration']
        source.update(screen_size=[100,100],heldout_median_error_screen_px=1.1)
        self.assertFalse(lift_registration(source,{'width':200,'height':200,'pixel_reduction':2},(100,100,3))['valid'])
        source['heldout_median_error_screen_px']=.1
        source['pixel_refinement']={'accepted':True,'max_correction_screen_px':1.1}
        self.assertFalse(lift_registration(source,{'width':200,'height':200,'pixel_reduction':2},(100,100,3))['valid'])
        for metadata in ({'width':201,'height':200,'pixel_reduction':2},
                         {'width':200,'height':200,'pixel_reduction':True},
                         {'width':400,'height':200,'pixel_reduction':2}):
            with self.assertRaises(ValueError):lift_registration(source,metadata,(100,100,3))


if __name__=='__main__':unittest.main(verbosity=2)

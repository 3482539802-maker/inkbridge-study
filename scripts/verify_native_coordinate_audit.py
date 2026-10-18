"""Reject ambiguous export decoding and verify independent distance arithmetic."""
import struct
import unittest

from inspect_native_pen import decode
from compare_native_coordinates import distance_to_polyline, native_to_pdf


def payload():
    data = bytearray(320+16+3*36)
    data[:12] = b'PENCILENGINE'
    struct.pack_into('>I', data, 112, 1)
    struct.pack_into('>I', data, 320-52, 2)
    struct.pack_into('>f', data, 320-52+28, 2.)
    struct.pack_into('>4I', data, 320, 0, 3, 36, 0)
    for index, (x,y) in enumerate(((10.,20.), (11.,21.), (12.,20.))):
        struct.pack_into('>2f', data, 340+index*36, x,y)
    return data


class NativeAuditChecks(unittest.TestCase):
    def test_supported_native_path_without_transform_fitting(self):
        strokes = decode(payload())
        self.assertEqual(strokes[0]['points_native'], [[10.,20.],[11.,21.],[12.,20.]])
        self.assertEqual(strokes[0]['width_native'], 2.)

    def test_count_nan_truncation_or_style_failure_rejected(self):
        for where, fmt, value in ((112, '>I', 2), (340, '>f', float('nan')), (268, '>I', 999)):
            bad = payload(); struct.pack_into(fmt,bad,where,value)
            with self.assertRaises(ValueError): decode(bad)
        with self.assertRaises(ValueError): decode(payload()[:-4])

    def test_degenerate_point_is_preserved_without_weakening_count_check(self):
        data=payload()
        for i in range(3):struct.pack_into('>2f',data,340+i*36,10.,20.)
        result=decode(data)
        self.assertEqual(len(result),1);self.assertTrue(result[0]['degenerate_point_path'])
        struct.pack_into('>I',data,112,2)
        with self.assertRaises(ValueError):decode(data)

    def test_observed_empty_envelope_and_false_empty_rejection(self):
        empty=bytearray(208);empty[:12]=b'PENCILENGINE'
        for offset,value in ((12,76),(16,65537),(24,1),(76,48),(80,2),(84,65536),(88,84),(108,12),(112,0),(116,12)):
            struct.pack_into('>I',empty,offset,value)
        self.assertEqual(decode(empty),[])
        with self.assertRaises(ValueError):decode(empty[:-4])
        data=payload();struct.pack_into('>I',data,112,0)
        with self.assertRaises(ValueError):decode(data)

    def test_polyline_distance_includes_segments_and_duplicate_vertices(self):
        distances = distance_to_polyline([[1,1],[5,0],[-1,0]],[[0,0],[0,0],[4,0]])
        self.assertEqual(list(distances), [1.,1.,1.])

    def test_pdf_placement_preserves_aspect_ratio_with_horizontal_margin(self):
        result = native_to_pdf([[50,0],[950,1200]], [1000,1200], [600,800])
        self.assertEqual(result, [[0.,0.],[600.,800.]])


if __name__ == '__main__':
    unittest.main(verbosity=2)

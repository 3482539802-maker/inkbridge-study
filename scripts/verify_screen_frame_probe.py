"""Frame packet integrity and clock-ambiguity checks; no device access."""
import json
import unittest
import cv2
import numpy as np
from screen_frame_probe import decode_packet, assess_timestamps


class FrameChecks(unittest.TestCase):
    def packet(self):
        image = np.full((480, 640, 3), 255, dtype=np.uint8)
        ok, jpeg = cv2.imencode('.jpg', image)
        self.assertTrue(ok)
        metadata = dict(schema=1, request_id='a' * 32, error='', timestamp_raw=100000000000,
                        callback_monotonic_us=100010000, callback_boottime_us=900010000,
                        callback_wall_us=1700000000000000, copy_finished_monotonic_us=100010500,
                        encoded_monotonic_us=100020000, width=640, height=480, stride=2560,
                        native_format=12, jpeg_bytes=len(jpeg))
        return metadata, jpeg.tobytes()

    def test_atomic_packet_identity_dimensions_and_length(self):
        metadata, jpeg = self.packet()
        def payload(m=metadata, data=jpeg):
            return json.dumps(m).encode() + b'\n' + data
        image, decoded = decode_packet(payload(), 'a' * 32, (640, 480))
        self.assertEqual(image.shape, (480, 640, 3))
        self.assertEqual(decoded, metadata)
        for value, nonce, size in [(payload(), 'b' * 32, (640, 480)),
                                    (payload(), 'a' * 32, (800, 600)),
                                    (payload(data=jpeg[:-1]), 'a' * 32, (640, 480)),
                                    (payload(dict(metadata, error='buffer_map_failed')), 'a' * 32, (640, 480)),
                                    (payload(dict(metadata, width=True)), 'a' * 32, (640, 480)),
                                    (payload(dict(metadata, copy_finished_monotonic_us=1)), 'a' * 32, (640, 480))]:
            with self.assertRaises(ValueError):
                decode_packet(value, nonce, size)

    def test_clock_units_and_ambiguity_never_become_verified(self):
        m, _ = self.packet()
        result = assess_timestamps([m, dict(m, timestamp_raw=m['timestamp_raw'] + 10000000000,
                                            callback_monotonic_us=m['callback_monotonic_us'] + 10000000,
                                            callback_boottime_us=m['callback_boottime_us'] + 10000000)])
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(result['candidates'][0]['unit'], 'ns')
        self.assertEqual(result['candidates'][0]['domain'], 'monotonic')
        self.assertFalse(result['verified'])
        ambiguous = assess_timestamps([dict(m, callback_boottime_us=m['callback_monotonic_us'])])
        self.assertEqual(len(ambiguous['candidates']), 2)
        self.assertFalse(ambiguous['used_for_pen_page_coordinates'])
        self.assertEqual(assess_timestamps([dict(m, timestamp_raw=0)])['candidates'], [])

    def test_reduced_packet_keeps_full_canvas_identity_and_validates_raster(self):
        metadata,jpeg=self.packet()
        metadata.update(width=1280,height=960,stride=5120,pixel_reduction=2,encoded_width=640,encoded_height=480)
        packet=json.dumps(metadata).encode()+b'\n'+jpeg
        image,decoded=decode_packet(packet,'a'*32,(1280,960))
        self.assertEqual(image.shape,(480,640,3))
        self.assertEqual(decoded['width'],1280)
        for key,value in [('pixel_reduction',3),('encoded_width',1280),('width',1279)]:
            with self.assertRaises(ValueError):
                decode_packet(json.dumps(dict(metadata,**{key:value})).encode()+b'\n'+jpeg,'a'*32,(1280,960))

    def test_optional_video_telemetry_keeps_unknown_result_and_rejects_malformed_counts(self):
        metadata,jpeg=self.packet()
        metadata.update(video_callback_count=123,max_video_fps_requested=5,max_video_fps_result=-1)
        def read(m):return decode_packet(json.dumps(m).encode()+b'\n'+jpeg,'a'*32,(640,480))[1]
        self.assertEqual(read(metadata)['max_video_fps_result'],-1)
        for key,value in [('video_callback_count',True),('video_callback_count',0),
                          ('max_video_fps_requested',0),('max_video_fps_result',None)]:
            with self.assertRaises(ValueError):read(dict(metadata,**{key:value}))


if __name__ == '__main__':
    unittest.main(verbosity=2)

"""Live revisions preserve raw and correctly separate strokes/page candidates."""
import json
from pathlib import Path
import tempfile
import unittest
from coordinate_live import update,strokes_from_raw
from verify_coordinate_alignment import point,frame,clocks


class LiveChecks(unittest.TestCase):
    def test_prefix_revisions_map_completed_stroke_after_future_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw=root/'canonical.jsonl'
            points=[point(i+1,t-10) for i,t in enumerate((1000,6000,11000))]
            points+=[dict(point(4,12000),action=1),dict(point(5,12100),action=3)]
            payload=('\n'.join(json.dumps(p) for p in points)+'\n').encode()
            raw.write_bytes(payload+b'{"partial":')
            first=update(raw,clocks(),[frame(11000)],root/'live',stable_runs=True)
            self.assertEqual(first['complete_located_strokes'],0)
            second=update(raw,clocks(),[frame(11000),frame(13000)],root/'live',stable_runs=True)
            self.assertEqual(second['complete_located_strokes'],1)
            stroke=json.loads((root/'live/strokes.json').read_bytes())['strokes'][-1]
            self.assertEqual(stroke['pdf_page_1based'],1)
            self.assertEqual(stroke['located_points'],2)
            self.assertEqual(stroke['pen_down_duration_ms'],100)
            self.assertEqual(raw.read_bytes(),payload+b'{"partial":')
            self.assertEqual(list((root/'live').glob('coordinate-prefix-*')),[])
            final=update(raw,clocks(),[frame(11000),frame(13000)],root/'live',finalized=True)
            self.assertTrue(final['observation_finalized'])
            html=(root/'live/report.html').read_text(encoding='utf-8')
            self.assertNotIn('http-equiv="refresh"',html)
            self.assertNotIn('部分／待后帧',html)

    def test_interleaved_pointer_and_cancel_never_count_as_complete(self):
        points=[dict(point(1,12000),action=1),dict(point(2,12010),action=1,pointer_id=2),
                dict(point(3,12020),action=0),dict(point(4,12030),action=3,pointer_id=2)]
        rows=strokes_from_raw(('\n'.join(json.dumps(p) for p in points)+'\n').encode(),{})
        self.assertEqual(len(rows),2)
        self.assertTrue(rows[0]['cancelled'])
        self.assertFalse(any(r['location_complete'] for r in rows))


if __name__=='__main__':unittest.main(verbosity=2)

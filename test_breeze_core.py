"""Safety and coordinate tests for saved-capture rendering and pagination."""
import hashlib
import math
import os
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageChops
import breeze_core as core


class CaptureRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured = os.environ.get('TMUX_SHOTS_TEST_FONTS')
        cls.fonts = core.find_fonts(Path(configured) if configured else None)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root/'capture.ansi'

    def run_capture(self, data, *, width=1130, columns=80, **kwargs):
        if isinstance(data,str):
            data = data.encode('utf-8')
        self.source.write_bytes(data)
        result = core.render_capture(self.source,self.root/f'images-{len(list(self.root.iterdir()))}',columns,
                                     core.Renderer(self.fonts,width=width),**kwargs)
        self.assertEqual(self.source.read_bytes(),data)
        self.assertEqual(result['capture_sha256'],hashlib.sha256(data).hexdigest())
        return result

    def test_trim_only_tail_preserves_internal_blank_and_one_padding_row(self):
        lines = [f'row {n}' if n!=5 else '' for n in range(1,16)]+['']*21
        result = self.run_capture('\n'.join(lines)+'\n',trim_empty_bottom=True,padding_rows=1)
        self.assertEqual(result['total_rows'],36)
        self.assertEqual(result['trim']['trimmed_bottom_rows'],20)
        self.assertEqual(result['selections'][0]['trimmed_row_ranges'],[[17,36]])
        self.assertEqual([(x['first_row'],x['last_row']) for x in result['images']],[(1,16)])
        image = result['images'][0]
        self.assertEqual([x['capture_row'] for x in image['geometry']['source_row_map']],list(range(1,17)))
        with Image.open(image['file']) as png:
            self.assertEqual(png.height,16*33+5)
            self.assertEqual(png.getpixel((20,4*33+4)),core.BACKGROUND)

    def test_colored_and_decorated_spaces_are_not_trimmed(self):
        for sgr in ['48;2;10;20;30','7','4','4:2','9','53']:
            with self.subTest(sgr=sgr):
                result = self.run_capture('text\n\n\x1b['+sgr+'m     \x1b[0m\n\n\n',trim_empty_bottom=True)
                self.assertEqual(result['images'][0]['last_row'],3)
                self.assertEqual(result['trim']['trimmed_bottom_rows'],2)

    def test_invisible_foreground_style_and_default_background_can_be_trimmed(self):
        result = self.run_capture('text\n\x1b[1;32m    \x1b[0m\n\x1b[48;2;35;38;39m    \x1b[0m\n',trim_empty_bottom=True)
        self.assertEqual(result['images'][0]['last_row'],1)
        self.assertEqual(result['trim']['trimmed_bottom_rows'],2)

    def test_fullscreen_trim_requires_override(self):
        data='nano contents\n'+'\n'*23
        result = self.run_capture(data,trim_empty_bottom=True,fullscreen=True)
        self.assertEqual(result['images'][0]['last_row'],24)
        self.assertFalse(result['trim']['applied'])
        self.assertTrue(any('fullscreen' in x for x in result['warnings']))
        result = self.run_capture(data,trim_empty_bottom=True,fullscreen=True,trim_fullscreen=True,padding_rows=1)
        self.assertEqual(result['images'][0]['last_row'],2)
        self.assertTrue(result['trim']['applied'])

    def test_select_multiple_ranges_preserves_earlier_ansi_style(self):
        data='\x1b[1;32m'+'\n'.join(f'row {n}' for n in range(1,61))+'\n'
        result = self.run_capture(data,select_rows=[(41,42),(50,51)])
        self.assertEqual([(x['first_row'],x['last_row']) for x in result['images']],[(41,42),(50,51)])
        for image in result['images']:
            with Image.open(image['file']) as png:
                self.assertIn((28,220,154),[rgb for _,rgb in png.getcolors(png.width*png.height)])
            self.assertEqual(image['geometry']['source_row_map'][0]['capture_row'],image['first_row'])

    def test_ranges_are_inclusive_and_validate_before_render(self):
        for ranges in [[(0,2)],[(3,2)],[(1,6)],[(1,3),(3,5)],[(3,4),(1,2)],[]]:
            with self.subTest(ranges=ranges):
                with self.assertRaises(ValueError):
                    self.run_capture('1\n2\n3\n4\n5\n',select_rows=ranges)
        result = self.run_capture('1\n2\n3\n4\n5\n',select_rows=[(2,2)])
        self.assertEqual(result['images'][0]['row_count'],1)
        self.assertEqual(result['selected_ranges'],[[2,2]])

    def test_trim_is_local_to_each_selection_and_source_stays_byte_identical(self):
        data=b'one\n\n\n\x1b[31mtwo\x1b[0m\n\n\n'
        result = self.run_capture(data,select_rows=[(1,3),(4,6)],trim_empty_bottom=True)
        self.assertEqual([(x['first_row'],x['last_row']) for x in result['images']],[(1,1),(4,4)])
        self.assertEqual(result['trim']['trimmed_bottom_rows'],4)

    def test_all_empty_capture_can_trim_to_nothing_or_keep_existing_padding(self):
        result = self.run_capture('\n'*6,trim_empty_bottom=True)
        self.assertEqual(result['images'],[])
        self.assertEqual(result['trim']['trimmed_bottom_rows'],6)
        result = self.run_capture('\n'*6,trim_empty_bottom=True,padding_rows=2)
        self.assertEqual(result['images'][0]['row_count'],2)
        self.assertEqual(result['images'][0]['geometry']['source_row_map'][-1]['capture_row'],2)

    def test_pagination_prefers_explicit_block_boundaries(self):
        result = self.run_capture('\n'.join(str(x) for x in range(1,71))+'\n',
                                  blocks=[(1,10),(11,30),(31,70)])
        self.assertEqual([(x['first_row'],x['last_row']) for x in result['images']],[(1,30),(31,70)])
        self.assertEqual(result['pagination']['mode'],'explicit-blocks')
        self.assertEqual(result['images'][1]['blocks'][0]['block'],3)
        self.assertFalse(result['images'][1]['blocks'][0]['continues_before'])
        self.assertFalse(result['images'][1]['blocks'][0]['continues_after'])
        mapped=[row['capture_row'] for image in result['images'] for row in image['geometry']['source_row_map']]
        self.assertEqual(mapped,list(range(1,71)))

    def test_long_blocks_split_with_original_range_and_continuation_metadata(self):
        result = self.run_capture('\n'.join(str(x) for x in range(1,96))+'\n',blocks=[(1,95)])
        self.assertEqual([(x['first_row'],x['last_row']) for x in result['images']],[(1,40),(41,80),(81,95)])
        pieces=[image['blocks'][0] for image in result['images']]
        self.assertEqual([(p['part'],p['parts']) for p in pieces],[(1,3),(2,3),(3,3)])
        self.assertEqual([(p['continues_before'],p['continues_after']) for p in pieces],[(False,True),(True,True),(True,False)])
        selected = self.run_capture('\n'.join(str(x) for x in range(1,96))+'\n',blocks=[(1,95)],select_rows=[(41,60)])
        segment=selected['images'][0]['blocks'][0]
        self.assertEqual((segment['block_first_row'],segment['block_last_row']),(1,95))
        self.assertTrue(segment['continues_before'] and segment['continues_after'])

    def test_geometry_maps_final_pixels_to_original_capture_rows(self):
        result = self.run_capture('a\nb\nc\nd\n',width=1800,select_rows=[(2,4)])
        image=result['images'][0];g=image['geometry']
        self.assertEqual(g['native_width'],80*g['cell_width_px']+10)
        self.assertEqual(g['native_height'],3*g['row_height_px']+5)
        self.assertEqual(g['padding_native'],{'left':5,'right':5,'top':0,'bottom':5})
        self.assertEqual(g['scale_x'],1800/g['native_width'])
        self.assertEqual(g['scale_y'],image['height']/g['native_height'])
        for index,mapping in enumerate(g['source_row_map']):
            self.assertEqual(mapping['capture_row'],index+2)
            self.assertEqual(mapping['native_y'],[index*g['row_height_px'],(index+1)*g['row_height_px']])
            self.assertAlmostEqual(mapping['image_y'][1],(index+1)*g['row_height_px']*g['scale_y'])
        self.assertLessEqual(g['content_bounds_final'][3],image['height'])

    def test_printed_font_size_depends_on_physical_width_not_png_resolution(self):
        data='a full row of monospace text\n'
        a=self.run_capture(data,width=1894,columns=132,report_width_mm=170,min_font_pt=8)
        b=self.run_capture(data,width=1800,columns=132,report_width_mm=170,min_font_pt=8)
        c=self.run_capture(data,width=1894,columns=132,report_width_mm=340,min_font_pt=8)
        pa,pb,pc=[r['images'][0]['print'] for r in [a,b,c]]
        self.assertTrue(pa['below_minimum'])
        self.assertAlmostEqual(pa['effective_font_pt'],pb['effective_font_pt'],delta=.05)
        self.assertAlmostEqual(pc['effective_font_pt'],pa['effective_font_pt']*2)
        self.assertTrue(any('pixel width alone' in message for message in a['warnings']))

    def test_italic_descenders_survive_bottom_trim_without_pixel_loss(self):
        data='\x1b[3;4mgypjQ\x1b[0m\n'+'\n'*8
        before=self.run_capture(data)
        after=self.run_capture(data,trim_empty_bottom=True)
        with Image.open(before['images'][0]['file']) as full,Image.open(after['images'][0]['file']) as trimmed:
            self.assertIsNone(ImageChops.difference(trimmed,full.crop((0,0,trimmed.width,trimmed.height))).getbbox())
            ink=ImageChops.difference(trimmed,Image.new('RGB',trimmed.size,core.BACKGROUND)).getbbox()
            self.assertLess(ink[3],trimmed.height)

    def test_combining_wide_and_emoji_clusters_keep_terminal_columns(self):
        parser=core.CaptureParser()
        row=parser.line('A\u0301中👩\u200d💻Z')
        self.assertEqual([(g.column,g.width) for g in row],[(0,1),(1,2),(3,2),(5,1)])
        self.assertEqual([g.text for g in row],['A\u0301','中','👩\u200d💻','Z'])
        for cluster in ['👍🏽','🇮🇷','1\ufe0f\u20e3']:
            result=core.CaptureParser().line(cluster+'X')
            self.assertEqual(len(result),2)
            self.assertEqual(result[0].width,2)
            self.assertEqual(result[1].column,2)
        self.assertTrue(any('Emoji' in x for x in parser.warnings))

    def test_persian_mixed_text_is_preserved_with_explicit_typography_warning(self):
        text='سلام 123 /tmp/فایل.txt'
        parser=core.CaptureParser()
        self.assertEqual(''.join(g.text for g in parser.line(text)),text)
        result=self.run_capture(text+'\n')
        self.assertTrue(any('Arabic/Persian' in message for message in result['warnings']))
        with Image.open(result['images'][0]['file']) as image:
            self.assertEqual(image.width,1130)
            self.assertIsNotNone(ImageChops.difference(image,Image.new('RGB',image.size,core.BACKGROUND)).getbbox())

    def test_combining_cjk_and_multi_codepoint_emoji_render_with_source_mapping(self):
        data='A\u0301中👩\u200d💻 /tmp/test\nfamily 👨\u200d👩\u200d👧\u200d👦 end\n'
        result=self.run_capture(data,select_rows=[(1,2)],trim_empty_bottom=True)
        image=result['images'][0]
        self.assertEqual([r['capture_row'] for r in image['geometry']['source_row_map']],[1,2])
        self.assertTrue(any('Emoji' in x for x in result['warnings']))
        with Image.open(image['file']) as png:
            self.assertEqual(png.height,2*33+5)
            self.assertEqual(png.getpixel((0,0)),core.BACKGROUND)


if __name__=='__main__':
    unittest.main()

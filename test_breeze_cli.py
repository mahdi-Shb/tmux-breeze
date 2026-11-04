"""Capture/offline integration tests; raw source fixtures are never rewritten."""
from contextlib import redirect_stdout,redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import tmux_shots as app
from test_tmux_shots import FakeTmux,font_dir


class OfflineTests(unittest.TestCase):
    def fixture(self, directory, settings=None):
        base=Path(directory)/'source';base.mkdir()
        raw=b'\x1b[1;32mfirst\nsecond\nthird\nfourth\n\n\n'
        (base/'capture.ansi').write_bytes(raw)
        source={'format_version':1,'session_name':'lab','session_id':'$0',
                'panes':[{'window_index':2,'window_id':'@9','window_name':'timing','pane_index':1,
                          'pane_id':'%7','pane_width':80,'pane_height':6,'buffers':[{
                              'buffer':'current','capture_file':'capture.ansi',
                              'sha256':hashlib.sha256(raw).hexdigest(),'images':[]}]}]}
        if settings:source['render_settings']=settings;source['format_version']=2
        manifest=base/'manifest.json';manifest.write_text(json.dumps(source),encoding='utf-8')
        return manifest,raw

    def run_render(self,manifest,output,*flags):
        stdout,stderr=io.StringIO(),io.StringIO()
        argv=['render',str(manifest),'-o',str(output),'--font-dir',str(font_dir()),'--json',*flags]
        with patch.object(app,'Tmux',side_effect=AssertionError('offline must not contact tmux')),redirect_stdout(stdout),redirect_stderr(stderr):
            result=app.main(argv)
        summary=json.loads(stdout.getvalue())
        return result,summary,stderr.getvalue()

    def test_offline_selection_trim_and_ansi_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest,raw=self.fixture(tmp)
            before={p.name:p.read_bytes() for p in manifest.parent.iterdir()}
            output=Path(tmp)/'rendered'
            code,summary,_=self.run_render(manifest,output,'--select-rows','2:6','--trim-empty-bottom','--padding-rows','1')
            self.assertEqual(code,0);self.assertEqual(summary['status'],'success')
            data=json.loads((output/'manifest.json').read_text(encoding='utf-8'))
            buffer=data['panes'][0]['buffers'][0]
            self.assertEqual((output/buffer['capture_file']).read_bytes(),raw)
            self.assertEqual(buffer['selected_ranges'],[[2,6]])
            self.assertEqual(buffer['trim']['trimmed_bottom_rows'],1)
            self.assertEqual((buffer['images'][0]['first_row'],buffer['images'][0]['last_row']),(2,5))
            self.assertEqual(data['format_version'],2)
            self.assertEqual(data['reproducibility']['tool_version'],'0.2.0')
            self.assertEqual(len(data['reproducibility']['fonts']),4)
            with Image.open(summary['images'][0]) as image:
                self.assertIn((28,220,154),[c for _,c in image.getcolors(image.width*image.height)])
            self.assertEqual(before,{p.name:p.read_bytes() for p in manifest.parent.iterdir()})

    def test_settings_and_blocks_inherit_then_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest,_=self.fixture(tmp,{'width':800,'rows':2,'select_rows':[[2,4]],'blocks':[[1,2],[3,4]]})
            code,summary,_=self.run_render(manifest,Path(tmp)/'rendered','--width','1200')
            self.assertEqual(code,0)
            data=json.loads(Path(summary['manifest']).read_text(encoding='utf-8'))
            item=data['panes'][0]['buffers'][0]
            self.assertEqual(data['render_settings']['width'],1200)
            self.assertEqual(item['selected_ranges'],[[2,4]])
            self.assertEqual(item['pagination']['blocks'],[[1,2],[3,4]])
            self.assertEqual([(i['first_row'],i['last_row']) for i in item['images']],[(2,2),(3,4)])
            with Image.open(summary['images'][0]) as image:self.assertEqual(image.width,1200)

    def test_saved_buffer_uses_its_capture_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest,_=self.fixture(tmp)
            data=json.loads(manifest.read_text())
            data['panes'][0]['buffers'][0]['geometry_before']={'pane_width':40,'pane_height':6}
            manifest.write_text(json.dumps(data))
            code,summary,_=self.run_render(manifest,Path(tmp)/'out')
            self.assertEqual(code,0)
            rendered=json.loads(Path(summary['manifest']).read_text())
            self.assertEqual(rendered['panes'][0]['buffers'][0]['images'][0]['columns'],40)

    def test_window_pane_and_buffer_filters_work_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest,_=self.fixture(tmp)
            code,summary,_=self.run_render(manifest,Path(tmp)/'rendered','--window','2','--pane','1','--buffer','current')
            self.assertEqual(code,0)
            self.assertEqual(summary['counts']['panes'],1)
            code,summary,_=self.run_render(manifest,Path(tmp)/'missing','--pane','%8')
            self.assertEqual(code,1);self.assertEqual(summary['status'],'failed')
            self.assertFalse((Path(tmp)/'missing').exists())

    def test_hash_mismatch_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest,_=self.fixture(tmp);(manifest.parent/'capture.ansi').write_bytes(b'tampered\n')
            target=Path(tmp)/'out'
            code,summary,_=self.run_render(manifest,target)
            self.assertEqual(code,1);self.assertEqual(summary['status'],'failed')
            self.assertIn('SHA-256 mismatch',summary['errors'][0]['message'])
            self.assertFalse(target.exists())

    def test_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest,_=self.fixture(tmp)
            data=json.loads(manifest.read_text());data['panes'][0]['buffers'][0]['capture_file']='../outside.ansi'
            manifest.write_text(json.dumps(data))
            code,summary,_=self.run_render(manifest,Path(tmp)/'out')
            self.assertEqual(code,1);self.assertIn('escapes',summary['errors'][0]['message'])

    def test_explicit_blocks_file_and_multiple_ranges(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest,_=self.fixture(tmp)
            blocks=Path(tmp)/'blocks.json';blocks.write_text(json.dumps({'buffers':{'%7:current':[[1,2],[3,4]]}}))
            code,summary,_=self.run_render(manifest,Path(tmp)/'out','--blocks',str(blocks),'--select-rows','1:2,4:4','--rows','2')
            self.assertEqual(code,0)
            data=json.loads(Path(summary['manifest']).read_text())
            self.assertEqual(data['render_settings']['blocks']['buffers']['%7:current'],[[1,2],[3,4]])
            self.assertEqual(data['panes'][0]['buffers'][0]['selected_ranges'],[[1,2],[4,4]])

    def test_all_resets_saved_row_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest,_=self.fixture(tmp,{'select_rows':[[2,3]]})
            code,summary,_=self.run_render(manifest,Path(tmp)/'out','--select-rows','all')
            self.assertEqual(code,0)
            data=json.loads(Path(summary['manifest']).read_text())
            self.assertEqual(data['panes'][0]['buffers'][0]['selected_ranges'],[[1,6]])

    def test_json_setup_failures_and_version(self):
        for flags in [[],['lab','--rows','0'],['render','missing.json'],['lab','--bogus']]:
            with self.subTest(flags=flags),redirect_stdout(io.StringIO()) as stdout,redirect_stderr(io.StringIO()):
                result=app.main(flags+['--json'])
                data=json.loads(stdout.getvalue())
                self.assertEqual(result,1);self.assertEqual(data['status'],'failed')
                self.assertEqual(data['counts']['errors'],1)
        with redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(app.main(['--json','--version']),0)
            self.assertEqual(json.loads(stdout.getvalue())['version'],'0.2.0')

    def test_json_help_is_one_json_document_and_success(self):
        for flags in [['--json','--help'],['render','--help','--json']]:
            with self.subTest(flags=flags),redirect_stdout(io.StringIO()) as stdout,redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(app.main(flags),0)
                data=json.loads(stdout.getvalue())
                self.assertEqual(data['version'],'0.2.0')
                self.assertIn('--trim-empty-bottom',data['help'])
                self.assertEqual(stderr.getvalue(),'')

    def test_intentionally_trimmed_empty_capture_is_success_with_raw_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest,_=self.fixture(tmp)
            raw=b'  \n\n  \n'
            (manifest.parent/'capture.ansi').write_bytes(raw)
            data=json.loads(manifest.read_text());data['panes'][0]['buffers'][0]['sha256']=hashlib.sha256(raw).hexdigest()
            manifest.write_text(json.dumps(data))
            code,summary,_=self.run_render(manifest,Path(tmp)/'out','--trim-empty-bottom')
            self.assertEqual(code,0);self.assertEqual(summary['status'],'success')
            self.assertEqual(summary['counts']['images'],0)
            self.assertEqual(summary['counts']['successful_buffers'],1)
            result=json.loads(Path(summary['manifest']).read_text())
            item=result['panes'][0]['buffers'][0]
            self.assertEqual(item['trim']['trimmed_bottom_rows'],3)
            self.assertEqual((Path(summary['manifest']).parent/item['capture_file']).read_bytes(),raw)


class CaptureMetadataTests(unittest.TestCase):
    def test_resize_warning_metadata_and_json(self):
        class ResizingTmux(FakeTmux):
            reads=0
            def metadata(self,pid):
                self.reads+=1
                return {'pane_width':80 if self.reads<3 else 100,'pane_height':24,
                        'alternate_on':0,'pane_in_mode':0,'pane_dead':0,'history_size':55}
        with tempfile.TemporaryDirectory() as tmp,redirect_stdout(io.StringIO()) as stdout,redirect_stderr(io.StringIO()):
            args=app.arguments(['lab','--pane','%0','--font-dir',str(font_dir()),'-o',str(Path(tmp)/'out'),'--json'])
            output,code=app.export_session(args,ResizingTmux())
            summary=json.loads(stdout.getvalue())
            self.assertEqual(code,2);self.assertEqual(summary['status'],'warning')
            data=json.loads((output/'manifest.json').read_text())
            item=data['panes'][0]['buffers'][0]
            self.assertEqual(item['geometry_before']['pane_width'],80)
            self.assertEqual(item['geometry_after']['pane_width'],100)
            self.assertLessEqual(item['capture_started_at_utc'],item['capture_finished_at_utc'])
            self.assertTrue(any('dimensions changed' in warning for warning in item['warnings']))

    def test_fullscreen_trim_remains_disabled_without_override(self):
        with tempfile.TemporaryDirectory() as tmp,redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            args=app.arguments(['capture','lab','--pane','%1','--buffer','alternate','--trim-empty-bottom',
                                '--font-dir',str(font_dir()),'-o',str(Path(tmp)/'out')])
            fake=FakeTmux()
            output,code=app.export_session(args,fake)
            data=json.loads((output/'manifest.json').read_text())
            item=data['panes'][0]['buffers'][0]
            self.assertEqual(item['buffer'],'alternate')
            self.assertTrue(item['fullscreen']);self.assertFalse(item['trim']['applied'])
            self.assertEqual(len(list(output.rglob('capture.ansi'))),1)
            self.assertEqual(len([call for call in fake.calls if call[0]=='capture-pane']),1)


if __name__=='__main__':unittest.main()

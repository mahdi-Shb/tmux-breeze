"""Black-box offline workflows, independent of a live tmux session."""
from contextlib import redirect_stdout, redirect_stderr
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


class OfflineWorkflows(unittest.TestCase):
    def fonts(self):
        directory = os.environ.get('TMUX_SHOTS_TEST_FONTS')
        return Path(directory) if directory else next(iter(app.find_fonts(None).values())).parent

    def snapshot(self, base):
        saved = base / 'saved'
        (saved / 'raw').mkdir(parents=True)
        lines = ['\x1b[1;32m' + 'line 01'] + [f'line {i:02d}' for i in range(2, 11)]
        lines += ['\x1b[0;44m        \x1b[0m', '', '\x1b[4m    \x1b[0m', '', 'visible end']
        lines += [''] * 21
        raw = ('\n'.join(lines) + '\n').encode()
        (saved / 'raw/capture.ansi').write_bytes(raw)
        manifest = {
            'format_version': 1, 'session_name': 'offline-session', 'session_id': '$99',
            'image_width': 1894, 'rows_per_image': 40, 'screen_only': False,
            'panes': [{'window_id': '@6', 'window_index': 2, 'window_name': 'build <example>',
                       'pane_id': '%3', 'pane_index': 0, 'pane_width': 80, 'pane_height': 36,
                       'alternate_on': 0, 'buffers': [{'buffer': 'current',
                       'capture_file': 'raw/capture.ansi', 'sha256': hashlib.sha256(raw).hexdigest(),
                       'images': []}]}], 'errors': []}
        path = saved / 'manifest.json'
        path.write_text(json.dumps(manifest), encoding='utf-8')
        return path, raw

    def invoke(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = app.main(argv)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_complete_offline_report_workflow_and_raw_immutability(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); source, raw = self.snapshot(base)
            before = source.read_bytes(); output = base / 'rendered'
            with patch.object(app.Tmux, 'call', side_effect=AssertionError('Offline rendering called tmux')):
                code, stdout, stderr = self.invoke(['render', str(source), '-o', str(output),
                    '--font-dir', str(self.fonts()), '--width', '1800', '--trim-empty-bottom',
                    '--padding-rows', '1', '--preview', '--simple-names', '--snippets', 'both', '--json'])
            self.assertIn(code, (0, 2), stderr)
            summary = json.loads(stdout)
            self.assertIn(summary['status'], ('success', 'warning'))
            manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
            images = [image for pane in manifest['panes'] for buffer in pane['buffers'] for image in buffer['images']]
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0]['last_row'], 16)
            self.assertEqual(images[0]['first_row'], 1)
            with Image.open(output / images[0]['file']) as image:
                self.assertEqual(image.width, 1800)
            self.assertTrue(list(output.rglob('*.html')))
            self.assertTrue(list(output.rglob('*.tex')))
            self.assertTrue(list(output.rglob('*.md')))
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual((source.parent / 'raw/capture.ansi').read_bytes(), raw)
            copies = list(output.rglob('*.ansi'))
            self.assertEqual(len(copies), 1)
            self.assertEqual(copies[0].read_bytes(), raw)

    def test_disjoint_ranges_keep_source_rows_and_inherited_ansi(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); source, raw = self.snapshot(base); output = base / 'selected'
            code, stdout, stderr = self.invoke(['render', str(source), '-o', str(output),
                '--font-dir', str(self.fonts()), '--buffer', 'current',
                '--select-rows', '3:5', '--select-rows', '8:10', '--json'])
            self.assertIn(code, (0, 2), stderr)
            json.loads(stdout)
            manifest = json.loads((output / 'manifest.json').read_text())
            images = manifest['panes'][0]['buffers'][0]['images']
            self.assertEqual([(x['first_row'], x['last_row']) for x in images], [(3, 5), (8, 10)])
            for spec in images:
                with Image.open(output / spec['file']) as image:
                    self.assertIn((28, 220, 154), [color for _, color in image.getcolors(image.width * image.height)])

    def test_bad_hash_returns_machine_readable_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); source, raw = self.snapshot(base)
            (source.parent / 'raw/capture.ansi').write_bytes(raw + b'changed')
            code, stdout, stderr = self.invoke(['render', str(source), '-o', str(base/'bad'),
                '--font-dir', str(self.fonts()), '--json'])
            self.assertEqual(code, 1)
            summary = json.loads(stdout)
            self.assertIn(summary['status'], ('failed', 'partial'))
            self.assertTrue(summary['errors'])
            self.assertFalse(list((base/'bad').rglob('*.png')))

    def test_machine_readable_invalid_options(self):
        code, stdout, stderr = self.invoke(['render', 'missing.json', '--rows', '0', '--json'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout)['status'], 'failed')


if __name__ == '__main__':
    unittest.main()

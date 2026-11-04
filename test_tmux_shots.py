"""Run: python3 -m unittest -v. Live tests use a disposable, isolated tmux server."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from PIL import Image
import tmux_shots as app


def font_dir():
    configured = os.environ.get('TMUX_SHOTS_TEST_FONTS')
    return Path(configured) if configured else next(iter(app.find_fonts(None).values())).parent


class ParserTests(unittest.TestCase):
    def test_sgr_and_reset(self):
        p = app.CaptureParser()
        row = p.line('\x1b[1;32mgreen\x1b[0m plain\n')
        self.assertEqual(''.join(g.text for g in row),'green plain')
        self.assertEqual(app.colors(row[0].style)[0],(28,220,154))
        self.assertEqual(row[-1].style,app.Style())
        self.assertFalse(p.warnings)

    def test_extended_colors_and_indexed_intensity(self):
        p = app.CaptureParser()
        row = p.line('\x1b[1;38;5;2mA\x1b[38;5;196mB\x1b[38:2::10:20:30;48;2;40;50;60mC')
        self.assertEqual(app.colors(row[0].style)[0],(17,209,22))
        self.assertEqual(app.colors(row[1].style)[0],(255,0,0))
        self.assertEqual(app.colors(row[2].style),((10,20,30),(40,50,60)))
        self.assertEqual(app.rgb(app.Indexed(255),app.FOREGROUND),(238,238,238))
        self.assertFalse(p.warnings)

    def test_style_carries_between_pages_and_no_lines_lost(self):
        lines = ['\x1b[34mline 0\n']+[f'line {i}\n' for i in range(1,101)]
        p = app.CaptureParser()
        batches = list(app.pages(lines,p,40))
        self.assertEqual([(start,len(rows)) for start,rows in batches],[(0,40),(40,40),(80,21)])
        self.assertEqual(batches[1][1][0][0].style.fg,4)
        self.assertEqual(''.join(g.text for g in batches[-1][1][-1]),'line 100')

    def test_breeze_default_and_faint_variants(self):
        self.assertEqual(app.colors(app.Style(bold=True))[0],(61,174,233))
        self.assertEqual(app.colors(app.Style(fg=2,faint=True))[0],(23,162,98))
        self.assertEqual(app.colors(app.Style(fg=app.Indexed(2),faint=True))[0],(17,209,22))
        self.assertEqual(app.colors(app.Style(bold=True,reverse=True))[0],(0,0,0))

    def test_cells_combining_wide_tabs_and_blank_rows(self):
        p = app.CaptureParser()
        row = p.line('A\u0301\u4e2dB\tZ  \n')
        self.assertEqual([(g.column,g.width) for g in row[:3]],[(0,1),(1,2),(3,1)])
        self.assertEqual(row[0].text,'A\u0301')
        self.assertEqual(row[-3].column,8)
        self.assertEqual(len(list(app.pages(['\n','x\n','\n'],p,40))[0][1]),3)
        self.assertFalse(p.warnings)

    def test_acs_underlines_inverse_and_malformed_escape(self):
        p = app.CaptureParser()
        row = p.line('\x1b(0lqqk\x1b(B\x1b[4:2;7mX\x1b[0m')
        self.assertEqual(''.join(g.text for g in row),'┌──┐X')
        self.assertEqual(row[-1].style.underline,2)
        self.assertEqual(app.colors(row[-1].style),(app.BACKGROUND,app.FOREGROUND))
        p.line('\x1b[38;2;badmY')
        self.assertTrue(p.warnings)


class RenderTests(unittest.TestCase):
    def test_background_width_and_longest_line_not_clipped(self):
        renderer = app.Renderer(app.find_fonts(font_dir()),1894)
        p = app.CaptureParser()
        rows = [p.line('\x1b[1;32mvboxuser@debian1\x1b[0m:\x1b[1;34m~\x1b[0m$ ls'),p.line('x'*120)]
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)/'image.png'
            result = renderer.render(rows,80,dest)
            with Image.open(dest) as image:
                self.assertEqual(image.width,1894)
                self.assertEqual(image.getpixel((0,0)),app.BACKGROUND)
                self.assertEqual(result['columns'],120)
                self.assertEqual(result['row_count'],2)
                self.assertIn((28,220,154),[rgb for _,rgb in image.getcolors(image.width*image.height)])

    def test_full_cell_background_including_spaces(self):
        renderer = app.Renderer(app.find_fonts(font_dir()),122)
        p = app.CaptureParser()
        with tempfile.TemporaryDirectory() as tmp:
            dest=Path(tmp)/'background.png'
            renderer.render([p.line('\x1b[48;2;10;20;30m        ')],8,dest)
            with Image.open(dest) as image:
                self.assertEqual(image.getpixel((10,1)),(10,20,30))


class FakeTmux(app.Tmux):
    def __init__(self, fail_alternate=False):
        super().__init__()
        self.calls=[]
        self.fail_alternate=fail_alternate

    def call(self,*args):
        self.calls.append(args)
        command=args[0]
        if command=='-V':return b'tmux 3.4\n'
        if command=='list-sessions':return b'$0\n$1\n'
        if command=='display-message':
            target=args[args.index('-t')+1]
            form=args[-1]
            if form=='#{session_name}':return (b'lab\n' if target=='$0' else b'laboratory\n')
            if form=='#{window_name}':return (b'build / kernel\n' if target=='@0' else b'modules\n')
            return (b'80\t24\t1\t0\t0\t55\n' if target=='%1' else b'80\t24\t0\t0\t0\t55\n')
        if command=='list-windows':return b'@0\t0\n@1\t1\n'
        if command=='list-panes':return (b'%0\t0\n%1\t1\n' if args[args.index('-t')+1]=='@0' else b'%2\t0\n')
        if command=='capture-pane':
            pid=args[args.index('-t')+1]
            if self.fail_alternate and pid=='%1' and '-a' not in args:
                raise app.TmuxError('pane disappeared')
            count=24 if '-a' not in args and pid=='%1' else 95
            return ('\x1b[32m'+'\n'.join(f'{pid} row {i:03d}' for i in range(count))+'\n').encode()
        raise AssertionError(args)


class ExportTests(unittest.TestCase):
    def args(self,tmp):
        return app.arguments(['lab','--output',str(Path(tmp)/'shots'),'--font-dir',str(font_dir())])

    def test_full_session_pagination_buffers_and_read_only_commands(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            fake=FakeTmux()
            output,code=app.export_session(self.args(tmp),fake)
            self.assertEqual(code,0)
            data=json.loads((output/'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(len(data['panes']),3)
            self.assertEqual(sum(len(p['buffers']) for p in data['panes']),4)
            self.assertEqual(len(list(output.rglob('*.png'))),10)
            self.assertFalse(data['errors'])
            for pane in data['panes']:
                for buffer in pane['buffers']:
                    self.assertTrue((output/buffer['capture_file']).exists())
                    spans=[(image['first_row'],image['last_row']) for image in buffer['images']]
                    self.assertEqual(spans,[(1,24)] if buffer['buffer']=='alternate' else [(1,40),(41,80),(81,95)])
            self.assertTrue(all(call[0] in {'-V','list-sessions','list-windows','list-panes','display-message','capture-pane'} for call in fake.calls))
            captures=[call for call in fake.calls if call[0]=='capture-pane']
            self.assertTrue(all('-e' in c and '-N' in c and '-J' not in c and '-C' not in c for c in captures))
            self.assertEqual(len([c for c in captures if '-a' in c]),1)

    def test_literal_session_resolution(self):
        fake=FakeTmux()
        self.assertEqual(fake.session_id('lab'),'$0')
        self.assertEqual(fake.session_id('laboratory'),'$1')
        for value in ['la','lab*','lab; kill-server']:
            with self.assertRaises(app.TmuxError):fake.session_id(value)

    def test_window_selection_by_index_name_or_id(self):
        inventory=list(FakeTmux().panes('$0'))
        for selector in ['0','@0','build / kernel','name:build / kernel']:
            with self.subTest(selector=selector):
                self.assertEqual([p['pane_id'] for p in app.select_panes(inventory,window=selector)],['%0','%1'])
        self.assertEqual([p['pane_id'] for p in app.select_panes(inventory,window='modules')],['%2'])
        for selector in ['2','@99','build*','build','modules; kill-server']:
            with self.subTest(selector=selector),self.assertRaises(app.TmuxError):
                app.select_panes(inventory,window=selector)

    def test_pane_selection_scoping_and_qualified_index(self):
        inventory=list(FakeTmux().panes('$0'))
        for window,pane,expected in [(None,'%2','%2'),(None,'0.1','%1'),('0','1','%1'),('modules','0','%2'),('@0','%1','%1')]:
            with self.subTest(window=window,pane=pane):
                self.assertEqual([p['pane_id'] for p in app.select_panes(inventory,window,pane)],[expected])
        for window,pane in [(None,'0'),('0','%2'),('modules','0.1'),(None,'%99'),('0','9'),(None,'; kill-server')]:
            with self.subTest(window=window,pane=pane),self.assertRaises(app.TmuxError):
                app.select_panes(inventory,window,pane)

    def test_ambiguous_names_numeric_names_and_window_links(self):
        inventory=list(FakeTmux().panes('$0'))
        duplicated=[dict(p,window_name='same') for p in inventory]
        with self.assertRaises(app.TmuxError):app.select_panes(duplicated,window='same')
        numeric=[dict(p,window_name='0' if p['window_index']==1 else 'build') for p in inventory]
        self.assertEqual(app.select_panes(numeric,window='name:0')[0]['pane_id'],'%2')
        linked=inventory+[dict(inventory[0],window_index=3)]
        with self.assertRaises(app.TmuxError):app.select_panes(linked,window='@0')
        with self.assertRaises(app.TmuxError):app.select_panes(linked,pane='%0')
        self.assertEqual(app.select_panes(linked,window='3',pane='%0')[0]['window_index'],3)

    def test_only_selected_targets_are_captured_and_recorded(self):
        for selectors,expected in [(['--window','modules'],['%2']),(['--window','0','--pane','1'],['%1']),(['--pane','0.0'],['%0'])]:
            with self.subTest(selectors=selectors),tempfile.TemporaryDirectory() as tmp,redirect_stdout(io.StringIO()):
                args=self.args(tmp)
                args=app.arguments(['lab','--output',str(args.output),'--font-dir',str(font_dir())]+selectors)
                fake=FakeTmux();output,code=app.export_session(args,fake)
                self.assertEqual(code,0)
                captured={call[call.index('-t')+1] for call in fake.calls if call[0]=='capture-pane'}
                self.assertEqual(captured,set(expected))
                data=json.loads((output/'manifest.json').read_text(encoding='utf-8'))
                self.assertEqual([p['pane_id'] for p in data['panes']],expected)
                self.assertEqual(data['selection'],{'window':args.window,'pane':args.pane,'buffer':args.buffer})

    def test_bad_selection_does_not_capture_or_create_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            args=self.args(tmp);args.window='not-present'
            fake=FakeTmux()
            with self.assertRaises(app.TmuxError):app.export_session(args,fake)
            self.assertFalse(args.output.exists())
            self.assertFalse(any(call[0]=='capture-pane' for call in fake.calls))

    def test_partial_failure_keeps_saved_main_and_continues(self):
        with tempfile.TemporaryDirectory() as tmp,redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            output,code=app.export_session(self.args(tmp),FakeTmux(fail_alternate=True))
            data=json.loads((output/'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(code,1)
            self.assertEqual(data['panes'][1]['buffers'][0]['buffer'],'main')
            self.assertEqual(len(data['panes'][1]['buffers'][0]['images']),3)
            self.assertEqual(len(data['panes'][2]['buffers'][0]['images']),3)
            self.assertEqual(len(data['errors']),1)

    def test_existing_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            args=self.args(tmp);args.output.mkdir()
            marker=args.output/'keep';marker.write_text('keep')
            with self.assertRaises(RuntimeError):app.export_session(args,FakeTmux())
            self.assertEqual(marker.read_text(),'keep')


@unittest.skipUnless(shutil.which('tmux') and os.name=='posix','requires Linux/Unix with tmux')
class LiveTmuxTests(unittest.TestCase):
    def test_real_scroll_history_multiple_windows_panes_and_ansi(self):
        socket='tmux-shots-test-'+uuid.uuid4().hex
        # Tests alone create a separate server; the application never does so.
        tmux=app.Tmux(socket_name=socket)
        try:
            code="import sys,time; print(''.join('\\x1b[1;32mLIVE-%03d\\x1b[0m\\n'%i for i in range(90)),end='',flush=True); time.sleep(60)"
            import shlex
            command=shlex.join([sys.executable,'-c',code])
            tmux.call('-f','/dev/null','new-session','-d','-s','snapshot-test','-x','80','-y','24',command)
            tmux.call('set-option','-g','history-limit','1000')
            tmux.call('split-window','-d','-t','snapshot-test:0','sleep 60')
            tmux.call('new-window','-d','-t','snapshot-test','-n','second','sleep 60')
            sid=tmux.session_id('snapshot-test')
            panes=list(tmux.panes(sid))
            first=panes[0]['pane_id']
            for _ in range(100):
                raw=tmux.call('capture-pane','-p','-e','-N','-t',first,'-S','-','-E','-')
                if b'LIVE-089' in raw:break
                time.sleep(.05)
            self.assertIn(b'LIVE-089',raw)
            with tempfile.TemporaryDirectory() as tmp,redirect_stdout(io.StringIO()):
                args=app.arguments(['snapshot-test','-L',socket,'--output',str(Path(tmp)/'shots'),'--font-dir',str(font_dir())])
                output,result=app.export_session(args,tmux)
                self.assertEqual(result,0)
                captured=(output/json.loads((output/'manifest.json').read_text())['panes'][0]['buffers'][0]['capture_file']).read_bytes()
                self.assertIn(b'\x1b[',captured)
                p=app.CaptureParser()
                texts=[''.join(g.text for g in row).rstrip() for _,rows in app.pages(io.StringIO(captured.decode()),p,40) for row in rows]
                self.assertEqual([x for x in texts if x.startswith('LIVE-')],[f'LIVE-{i:03d}' for i in range(90)])
                self.assertEqual(len(list(output.glob('window-*'))),2)
        finally:
            try:tmux.call('kill-server')
            except app.TmuxError:pass


if __name__=='__main__':unittest.main()

#!/usr/bin/env python3
"""Export actual tmux pane grids and scrollback as paginated Breeze PNGs.

No shell commands are injected into panes; captures are read-only. Requires
Python 3.10+, Pillow and wcwidth. Run --help for options.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Iterable, Iterator

try:
    from PIL import Image, ImageDraw, ImageFont
    from wcwidth import wcwidth
except ImportError as exc:
    raise SystemExit("Missing dependency. On Debian: sudo apt install python3-pil python3-wcwidth fonts-dejavu-core") from exc

BACKGROUND = (35, 38, 39)
FOREGROUND = (252, 252, 252)
# KDE's unmodified Breeze.colorscheme, Color0..7 then Color0Intense..7Intense.
PALETTE = (
    (35,38,39), (237,21,21), (17,209,22), (246,116,0),
    (29,153,243), (155,89,182), (26,188,156), (252,252,252),
    (127,140,141), (192,57,43), (28,220,154), (253,188,75),
    (61,174,233), (142,68,173), (22,160,133), (255,255,255),
)
FAINT_PALETTE = (
    (49,54,59),(120,50,40),(23,162,98),(182,86,25),
    (27,102,143),(97,74,115),(24,108,96),(99,104,109),
)
@dataclass(frozen=True)
class Indexed:
    value: int


Color = int | Indexed | tuple[int, int, int] | None
ESCAPE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][0-~]|[ -/]*[0-~])"
)
ACS = {'`':'◆','a':'▒','f':'°','g':'±','j':'┘','k':'┐','l':'┌','m':'└',
       'n':'┼','o':'⎺','p':'⎻','q':'─','r':'⎼','s':'⎽','t':'├','u':'┤',
       'v':'┴','w':'┬','x':'│','y':'≤','z':'≥','{':'π','|':'≠','}':'£','~':'·'}


@dataclass(frozen=True)
class Style:
    fg: Color = None
    bg: Color = None
    underline_color: Color = None
    bold: bool = False
    faint: bool = False
    italic: bool = False
    underline: int = 0
    reverse: bool = False
    hidden: bool = False
    strike: bool = False
    overline: bool = False


@dataclass(frozen=True)
class Glyph:
    column: int
    text: str
    width: int
    style: Style


def rgb(color: Color, default: tuple[int, int, int], bright: bool = False) -> tuple[int, int, int]:
    if color is None:
        return default
    if isinstance(color, tuple):
        return color
    if isinstance(color, Indexed):
        color, bright = color.value, False
    if color < 16:
        return PALETTE[color + 8 if bright and color < 8 else color]
    if color < 232:
        n = color - 16
        cube = (0, 95, 135, 175, 215, 255)
        return cube[n // 36], cube[(n // 6) % 6], cube[n % 6]
    return (8 + (color - 232) * 10,) * 3


def themed_color(color: Color, role: str, intensity: int = 0):
    if color is None:
        defaults = {'fg':(FOREGROUND,(61,174,233),(239,240,241)),
                    'bg':(BACKGROUND,(0,0,0),(49,54,59))}
        return defaults[role][intensity]
    if isinstance(color,int) and intensity:
        return PALETTE[color % 8 + 8] if intensity == 1 else FAINT_PALETTE[color % 8]
    return rgb(color,FOREGROUND if role == 'fg' else BACKGROUND)


def colors(style: Style, bold_bright: bool = True):
    intensity = 1 if style.bold and not style.faint and bold_bright else (2 if style.faint and not style.bold else 0)
    # Konsole resolves reverse first, then foreground intensity. Indexed/RGB
    # colors do not use the system palette's intensive/faint variants.
    fg_color,bg_color = (style.bg,style.fg) if style.reverse else (style.fg,style.bg)
    fg_role,bg_role = ('bg','fg') if style.reverse else ('fg','bg')
    fg = themed_color(fg_color,fg_role,intensity)
    bg = themed_color(bg_color,bg_role)
    if style.hidden:
        fg = bg
    return fg, bg


class CaptureParser:
    """Parse SGR/character-set attributes from an already resolved tmux grid.

    This is deliberately not a replay engine for arbitrary application output:
    tmux has already resolved cursor motions, progress bars and editor redraws.
    Attribute state is preserved across lines and image/page boundaries.
    """
    def __init__(self):
        self.style = Style()
        self.g0_acs = False
        self.g1_acs = False
        self.use_g1 = False
        self.warnings: set[str] = set()

    def sgr(self, parameters: str):
        parts = parameters.split(';')
        i = 0
        while i < len(parts):
            group = parts[i].split(':')
            try:
                code = int(group[0] or 0)
                if code in (38, 48, 58):
                    if len(group) > 1:
                        mode = int(group[1])
                        values = [int(x) for x in group[2:] if x != '']
                        value = values[-1] if mode == 5 else tuple(values[-3:])
                        valid = (mode == 5 and len(values) == 1) or (mode == 2 and len(values) in (3, 4))
                    else:
                        mode = int(parts[i + 1])
                        count = 1 if mode == 5 else 3
                        values = [int(x) for x in parts[i + 2:i + 2 + count]]
                        valid = mode in (2, 5) and len(values) == count
                        value = values[0] if mode == 5 else tuple(values)
                        i += 1 + count
                    if not valid or any(not 0 <= x <= 255 for x in values):
                        raise ValueError('invalid extended color')
                    if mode == 5:
                        value = Indexed(value)
                    field = {38:'fg', 48:'bg', 58:'underline_color'}[code]
                    self.style = replace(self.style, **{field:value})
                elif code == 0:
                    self.style = Style()
                elif code in (1,2,3,7,8,9,53):
                    field = {1:'bold',2:'faint',3:'italic',7:'reverse',8:'hidden',9:'strike',53:'overline'}[code]
                    self.style = replace(self.style, **{field:True})
                elif code == 4:
                    variant = int(group[1]) if len(group) > 1 else 1
                    self.style = replace(self.style, underline=variant)
                elif code == 21:
                    self.style = replace(self.style, underline=2)
                elif code == 22:
                    self.style = replace(self.style, bold=False, faint=False)
                elif code in (23,24,27,28,29,55):
                    field = {23:'italic',24:'underline',27:'reverse',28:'hidden',29:'strike',55:'overline'}[code]
                    self.style = replace(self.style, **{field:False})
                elif 30 <= code <= 37:
                    self.style = replace(self.style, fg=code - 30)
                elif 40 <= code <= 47:
                    self.style = replace(self.style, bg=code - 40)
                elif 90 <= code <= 97:
                    self.style = replace(self.style, fg=code - 90 + 8)
                elif 100 <= code <= 107:
                    self.style = replace(self.style, bg=code - 100 + 8)
                elif code in (39,49,59):
                    self.style = replace(self.style, **{{39:'fg',49:'bg',59:'underline_color'}[code]:None})
                elif code not in (5,6,10,25):  # blink: render its visible phase
                    self.warnings.add(f'Unsupported SGR attribute {code}')
            except (ValueError, IndexError):
                self.warnings.add(f'Malformed SGR sequence: {parameters!r}')
            i += 1

    def line(self, text: str) -> list[Glyph]:
        glyphs: list[Glyph] = []
        column = 0
        pos = 0
        # Remove only the transport newline, never significant terminal spaces.
        if text.endswith('\n'):
            text = text[:-1]
        while pos < len(text):
            char = text[pos]
            if char == '\x1b':
                match = ESCAPE.match(text, pos)
                if not match:
                    self.warnings.add('Unrecognized escape byte in capture')
                    pos += 1
                    continue
                seq = match[0]
                if seq.startswith('\x1b[') and seq.endswith('m'):
                    self.sgr(seq[2:-1])
                elif seq.startswith(('\x1b(', '\x1b)')):
                    if seq[1] == '(':
                        self.g0_acs = seq[2] == '0'
                    else:
                        self.g1_acs = seq[2] == '0'
                elif not seq.startswith('\x1b]8;'):  # hyperlink target is not visible
                    self.warnings.add(f'Unsupported escape sequence {seq!r}')
                pos = match.end()
                continue
            pos += 1
            if char in ('\x0e', '\x0f'):
                self.use_g1 = char == '\x0e'
                continue
            if char == '\t':
                for _ in range(8 - column % 8):
                    glyphs.append(Glyph(column, ' ', 1, self.style))
                    column += 1
                continue
            if ord(char) < 32 or ord(char) == 127:
                self.warnings.add(f'Unexpected control character U+{ord(char):04X}')
                continue
            if char == '\ufffd':
                self.warnings.add('UTF-8 decoding replacement character present')
            if self.g1_acs if self.use_g1 else self.g0_acs:
                char = ACS.get(char, char)
            width = wcwidth(char)
            if width < 0:
                self.warnings.add(f'Unprintable character U+{ord(char):04X}')
            elif width == 0 and glyphs:
                glyphs[-1] = replace(glyphs[-1], text=glyphs[-1].text + char)
            elif width > 0:
                glyphs.append(Glyph(column, char, width, self.style))
                column += width
        return glyphs


def pages(lines: Iterable[str], parser: CaptureParser, size: int) -> Iterator[tuple[int,list[list[Glyph]]]]:
    batch = []
    start = 0
    for line in lines:
        batch.append(parser.line(line))
        if len(batch) == size:
            yield start, batch
            start += len(batch)
            batch = []
    if batch:
        yield start, batch


def find_fonts(directory: Path | None) -> dict[tuple[bool,bool], Path]:
    files = {
        (False,False):'DejaVuSansMono.ttf',
        (True,False):'DejaVuSansMono-Bold.ttf',
        (False,True):'DejaVuSansMono-Oblique.ttf',
        (True,True):'DejaVuSansMono-BoldOblique.ttf',
    }
    candidates = [directory] if directory else [
        Path('/usr/share/fonts/truetype/dejavu'),
        Path('/usr/share/fonts/dejavu-sans-mono-fonts'),
        Path('/usr/share/fonts/TTF'),
    ]
    for candidate in candidates:
        if candidate and all((candidate/name).is_file() for name in files.values()):
            return {style:candidate/name for style,name in files.items()}
    if not directory and shutil.which('fc-match'):
        found = {}
        for style, variant in [((False,False),'Book'),((True,False),'Bold'),((False,True),'Oblique'),((True,True),'Bold Oblique')]:
            result = subprocess.run(['fc-match','-f','%{file}',f'DejaVu Sans Mono:style={variant}'],capture_output=True,check=False)
            path = Path(result.stdout.decode('utf-8',errors='replace'))
            if path.is_file() and path.name == files[style]:
                found[style] = path
        if len(found) == 4:
            return found
    raise RuntimeError('DejaVu Sans Mono fonts not found. Install fonts-dejavu-core or use --font-dir DIR.')


class Renderer:
    def __init__(self, font_paths: dict, width: int = 1894, font_size: int = 24, bold_bright: bool = True):
        self.fonts = {key:ImageFont.truetype(str(path),font_size,layout_engine=ImageFont.Layout.BASIC) for key,path in font_paths.items()}
        regular = self.fonts[False,False]
        self.cell_width = math.ceil(regular.getlength('M'))
        self.ascent, self.descent = regular.getmetrics()
        self.line_height = max(self.ascent + self.descent, math.ceil(font_size * 1.375))
        self.width = width
        self.bold_bright = bold_bright
        self.padding_left, self.padding_top, self.padding_bottom = 5, 0, 5

    def render(self, rows: list[list[Glyph]], columns: int, destination: Path):
        widest = max((g.column + g.width for row in rows for g in row),default=0)
        columns = max(columns, widest, 1)
        native_width = columns * self.cell_width + self.padding_left * 2
        native_height = max(1,len(rows)) * self.line_height + self.padding_top + self.padding_bottom
        image = Image.new('RGB',(native_width,native_height),BACKGROUND)
        draw = ImageDraw.Draw(image)
        for y,row in enumerate(rows):
            top = self.padding_top + y * self.line_height
            baseline = top + self.ascent
            # Paint all backgrounds first: neighboring glyphs must not erase
            # italic overhangs or combining marks drawn in the text pass.
            for glyph in row:
                x = self.padding_left + glyph.column * self.cell_width
                _,bg = colors(glyph.style,self.bold_bright)
                if bg != BACKGROUND:
                    draw.rectangle((x,top,x+glyph.width*self.cell_width-1,top+self.line_height-1),fill=bg)
            for glyph in row:
                x = self.padding_left + glyph.column * self.cell_width
                style = glyph.style
                fg,bg = colors(style,self.bold_bright)
                font = self.fonts[style.bold,style.italic]
                if not style.hidden:
                    draw.text((x,baseline),glyph.text,font=font,fill=fg,anchor='ls')
                else:
                    continue
                right = x + glyph.width * self.cell_width - 1
                under = rgb(style.underline_color,fg)
                uy = min(top+self.line_height-2,baseline+2)
                if style.underline:
                    if style.underline == 3:
                        draw.line([(px,uy+(px-x)%4//2) for px in range(x,right+1)],fill=under,width=1)
                    elif style.underline in (4,5):
                        step = 2 if style.underline == 4 else 6
                        for px in range(x,right+1,step):
                            draw.line((px,uy,min(px+(0 if step==2 else 3),right),uy),fill=under)
                    else:
                        draw.line((x,uy,right,uy),fill=under)
                        if style.underline == 2:
                            draw.line((x,uy+2,right,uy+2),fill=under)
                if style.strike:
                    draw.line((x,baseline-self.ascent//3,right,baseline-self.ascent//3),fill=fg)
                if style.overline:
                    draw.line((x,top+1,right,top+1),fill=fg)
        height = max(1,round(native_height * self.width / native_width))
        if image.size != (self.width,height):
            image = image.resize((self.width,height),Image.Resampling.LANCZOS)
        image.save(destination,'PNG')
        return {'width':self.width,'height':height,'columns':columns,'row_count':len(rows)}


class TmuxError(RuntimeError):
    pass


class Tmux:
    def __init__(self, socket_name: str | None = None, socket_path: str | None = None, timeout: float = 30):
        self.prefix = ['tmux','-u']
        if socket_name:
            self.prefix += ['-L',socket_name]
        if socket_path:
            self.prefix += ['-S',socket_path]
        self.timeout = timeout

    def call(self, *args: str) -> bytes:
        try:
            result = subprocess.run(self.prefix+list(args),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=self.timeout,check=False)
        except FileNotFoundError as exc:
            raise TmuxError('tmux is not installed or is not on PATH.') from exc
        except subprocess.TimeoutExpired as exc:
            raise TmuxError(f'tmux {args[0]} timed out after {self.timeout:g}s') from exc
        if result.returncode:
            raise TmuxError(result.stderr.decode('utf-8',errors='replace').strip() or f'tmux {args[0]} failed')
        return result.stdout

    def text(self, *args: str) -> str:
        return self.call(*args).decode('utf-8',errors='replace').removesuffix('\n')

    def session_id(self, name: str) -> str:
        # Resolve the literal full name before using any target. Never pass a
        # user-supplied name through tmux's fuzzy/glob target selection.
        ids = self.text('list-sessions','-F','#{session_id}').splitlines()
        candidates = [(sid,self.text('display-message','-p','-t',sid,'#{session_name}')) for sid in ids]
        for sid,session_name in candidates:
            if session_name == name:
                return sid
        for sid,_ in candidates:
            if sid == name:
                return sid
        available = ', '.join(repr(s) for _,s in candidates)
        raise TmuxError(f'No session named {name!r}. Available: {available}')

    def panes(self, session_id: str) -> Iterator[dict]:
        windows = self.text('list-windows','-t',session_id,'-F','#{window_id}\t#{window_index}').splitlines()
        for window in windows:
            wid,index = window.split('\t')
            name = self.text('display-message','-p','-t',wid,'#{window_name}')
            listing = self.text('list-panes','-t',wid,'-F','#{pane_id}\t#{pane_index}').splitlines()
            for pane in listing:
                pid,pindex = pane.split('\t')
                yield {'window_id':wid,'window_index':int(index),'window_name':name,'pane_id':pid,'pane_index':int(pindex)}

    def capture(self, pane: dict, destination: Path, screen_only: bool) -> Iterator[dict]:
        pid = pane['pane_id']
        fields = ['pane_width','pane_height','alternate_on','pane_in_mode','pane_dead','history_size']
        values = self.text('display-message','-p','-t',pid,'\t'.join('#{'+x+'}' for x in fields)).split('\t')
        if len(values) != len(fields):
            raise TmuxError(f'Unexpected pane metadata for {pid}')
        pane.update({key:int(value or '0') for key,value in zip(fields,values)})
        pane['captured_at_utc'] = datetime.now(timezone.utc).isoformat()
        buffers = ['current'] if not pane['alternate_on'] else (['alternate'] if screen_only else ['main','alternate'])
        for buffer in buffers:
            arguments = ['capture-pane','-p','-e','-N','-t',pid,'-S','0' if screen_only else '-','-E','-']
            if buffer == 'main':
                # -a is tmux's saved screen grid, not the active editor grid.
                arguments.append('-a')
            data = self.call(*arguments)
            folder = destination / buffer
            folder.mkdir(parents=True,exist_ok=True)
            path = folder/'capture.ansi'
            path.write_bytes(data)
            yield {'buffer':buffer,'capture_file':path,'sha256':hashlib.sha256(data).hexdigest(),'capture_command':self.prefix+arguments}


def safe_name(name: str) -> str:
    return re.sub(r'[^\w.-]+','_',name).strip('._-')[:48] or 'untitled'


def select_panes(inventory: list[dict], window: str | None = None, pane: str | None = None) -> list[dict]:
    """Resolve selectors only against the chosen session's inventory.

    No tmux fuzzy matching is used. Ambiguous names/linked windows fail before
    any capture or output directory is created.
    """
    selected = inventory
    if window is not None:
        if window.startswith('name:'):
            matches = [p for p in selected if p['window_name'] == window[5:]]
        elif window.isdecimal():
            matches = [p for p in selected if p['window_index'] == int(window)]
        elif re.fullmatch(r'@\d+',window):
            matches = [p for p in selected if p['window_id'] == window]
        else:
            matches = [p for p in selected if p['window_name'] == window]
        identities = {(p['window_index'],p['window_id']) for p in matches}
        if not identities:
            available = sorted({(p['window_index'],p['window_name'],p['window_id']) for p in inventory})
            raise TmuxError(f'No window {window!r} in this session. Available (index, name, ID): {available!r}')
        if len(identities) != 1:
            raise TmuxError(f'Ambiguous window {window!r}; use its numeric window index.')
        selected = matches
    if pane is not None:
        if re.fullmatch(r'%\d+',pane):
            selected = [p for p in selected if p['pane_id'] == pane]
        elif match := re.fullmatch(r'(\d+)\.(\d+)',pane):
            selected = [p for p in selected if p['window_index'] == int(match[1]) and p['pane_index'] == int(match[2])]
        elif pane.isdecimal():
            if window is None:
                raise TmuxError('A plain pane index needs --window. Use --window 2 --pane 1, --pane 2.1, or --pane "%3".')
            selected = [p for p in selected if p['pane_index'] == int(pane)]
        else:
            raise TmuxError(f'Invalid pane selector {pane!r}; use %ID, WINDOW_INDEX.PANE_INDEX, or --window WINDOW --pane INDEX.')
        if not selected:
            raise TmuxError(f'Pane {pane!r} is not in the selected session/window.')
        if len(selected) != 1:
            raise TmuxError(f'Pane {pane!r} appears through multiple window links; also specify --window INDEX.')
    return selected


def export_session(args, tmux: Tmux | None = None) -> tuple[Path,int]:
    fonts = find_fonts(args.font_dir)
    renderer = Renderer(fonts,args.width,args.font_size,not args.no_bold_bright)
    tmux = tmux or Tmux(args.socket_name,args.socket,args.timeout)
    sid = tmux.session_id(args.session)
    inventory = list(tmux.panes(sid))
    if not inventory:
        raise TmuxError('The session has no panes.')
    inventory = select_panes(inventory,args.window,args.pane)
    now = datetime.now(timezone.utc)
    output = args.output or Path('tmux-shots')/(safe_name(args.session)+'-'+now.strftime('%Y%m%dT%H%M%S.%fZ'))
    output = output.expanduser().resolve()
    if output.exists():
        raise RuntimeError(f'Output directory already exists; choose a new --output path: {output}')
    output.mkdir(parents=True,mode=0o700)
    manifest = {
        'format_version':1,'session_name':args.session,'session_id':sid,
        'started_at_utc':now.isoformat(),'source':'tmux capture-pane cell grids',
        'theme':'Konsole Breeze','image_width':args.width,'rows_per_image':args.rows,
        'screen_only':args.screen_only,'font_files':{str(k):str(v) for k,v in fonts.items()},
        'selection':{'window':args.window,'pane':args.pane},
        'notes':['Pane snapshots are sequential; running processes are not paused.',
                 'Only scrollback still retained by tmux is available.',
                 'No commands, prompts or colors are invented. No desktop frame or cursor is drawn.'],
        'panes':[], 'errors':[],
    }
    jobs = []
    # Capture all panes before rendering or writing progress to the terminal.
    for pane in inventory:
        folder = output/f"window-{pane['window_index']:03d}-{safe_name(pane['window_name'])}"/f"pane-{pane['pane_index']:02d}-{pane['pane_id'].lstrip('%')}"
        record = dict(pane,buffers=[])
        manifest['panes'].append(record)
        try:
            for item in tmux.capture(record,folder,args.screen_only):
                path = item.pop('capture_file')
                item['capture_file'] = path.relative_to(output).as_posix()
                item['images'] = []
                record['buffers'].append(item)
                jobs.append((record,item,path))
        except (TmuxError,OSError,ValueError) as exc:
            manifest['errors'].append({'pane_id':pane['pane_id'],'stage':'capture','message':str(exc)})
    for record,item,path in jobs:
        parser = CaptureParser()
        try:
            with path.open('r',encoding='utf-8',errors='replace',newline='') as stream:
                for number,(start,rows) in enumerate(pages(stream,parser,args.rows),1):
                    destination = path.parent/f'part-{number:04d}.png'
                    metrics = renderer.render(rows,record['pane_width'],destination)
                    item['images'].append(dict(file=destination.relative_to(output).as_posix(),first_row=start+1,last_row=start+len(rows),**metrics))
            item['warnings'] = sorted(parser.warnings)
            item['total_rows'] = sum(image['row_count'] for image in item['images'])
            if not item['images']:
                manifest['errors'].append({'pane_id':record['pane_id'],'stage':'render','message':'Empty capture; raw file retained.'})
        except (OSError,ValueError) as exc:
            manifest['errors'].append({'pane_id':record['pane_id'],'stage':'render','message':str(exc)})
    manifest['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    count = sum(len(item['images']) for record in manifest['panes'] for item in record['buffers'])
    print(f'Saved {count} PNGs from {len(jobs)} buffers in {len(inventory)} panes.\n{output}')
    for error in manifest['errors']:
        print(f"WARNING {error['pane_id']} ({error['stage']}): {error['message']}",file=sys.stderr)
    warnings = sum(len(item.get('warnings',[])) for record in manifest['panes'] for item in record['buffers'])
    if warnings:
        print(f'WARNING: {warnings} rendering warnings; see manifest.json.',file=sys.stderr)
    return output,1 if manifest['errors'] else (2 if warnings else 0)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('session',help='Exact tmux session name, or a session ID such as $0')
    parser.add_argument('-w','--window',help='Only this window: index, @ID, or exact name (use name:NAME to force a literal name)')
    parser.add_argument('-p','--pane',help='Only this pane: %%ID, WINDOW_INDEX.PANE_INDEX, or a pane index combined with --window')
    parser.add_argument('-o','--output',type=Path,help='New output directory; existing directories are never overwritten')
    parser.add_argument('--rows',type=int,default=40,help='Maximum physical terminal rows per PNG (default: 40)')
    parser.add_argument('--width',type=int,default=1894,help='Exact output width in pixels (default: 1894)')
    parser.add_argument('--font-size',type=int,default=24,help='Native font size before uniform width scaling (default: 24)')
    parser.add_argument('--font-dir',type=Path,help='Directory containing the four DejaVuSansMono .ttf variants')
    parser.add_argument('--screen-only',action='store_true',help='Capture only the current screen, without scrollback or saved main buffer')
    parser.add_argument('--no-bold-bright',action='store_true',help='Do not map bold ANSI colors 0..7 to intense colors')
    socket = parser.add_mutually_exclusive_group()
    socket.add_argument('-L','--socket-name',help='tmux server socket name')
    socket.add_argument('-S','--socket',help='tmux server socket path')
    parser.add_argument('--timeout',type=float,default=30,help='Timeout per tmux call, in seconds (default: 30)')
    args = parser.parse_args(argv)
    if not 1 <= args.rows <= 500 or not 64 <= args.width <= 16384 or not 8 <= args.font_size <= 96 or args.timeout <= 0:
        parser.error('Use rows 1..500, width 64..16384, font-size 8..96, and a positive timeout.')
    return args


def main(argv=None):
    try:
        _,code = export_session(arguments(argv))
        return code
    except (TmuxError,RuntimeError,OSError,ValueError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted. Already captured files remain in the output directory.',file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())

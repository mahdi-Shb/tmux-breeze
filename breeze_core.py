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
import unicodedata
from typing import Iterable, Iterator

try:
    from PIL import Image, ImageDraw, ImageFont, ImageChops
    from wcwidth import wcwidth, wcswidth
except ImportError as exc:
    raise SystemExit("Missing dependency. On Debian: sudo apt install python3-pil python3-wcwidth fonts-dejavu") from exc

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
            if '\u0600' <= char <= '\u08ff' and unicodedata.category(char).startswith('L'):
                self.warnings.add('Arabic/Persian text is retained in terminal cell order; contextual shaping and font fallback are not implemented.')
            if ord(char) >= 0x1f000 or char == '\ufe0f':
                self.warnings.add('Emoji cell positions are preserved where possible; color emoji and font fallback are not implemented.')
            if self.g1_acs if self.use_g1 else self.g0_acs:
                char = ACS.get(char, char)
            width = wcwidth(char)
            if width < 0:
                self.warnings.add(f'Unprintable character U+{ord(char):04X}')
            elif glyphs and (width == 0 or glyphs[-1].text.endswith('\u200d')
                             or 0x1f3fb <= ord(char) <= 0x1f3ff
                             or (0x1f1e6 <= ord(char) <= 0x1f1ff and len(glyphs[-1].text) == 1
                                 and 0x1f1e6 <= ord(glyphs[-1].text) <= 0x1f1ff)):
                previous = glyphs[-1]
                cluster = previous.text + char
                span = wcswidth(cluster)
                if span < 0:
                    span = previous.width + max(0,width)
                glyphs[-1] = replace(previous, text=cluster, width=span)
                column += span - previous.width
            elif width == 0:
                self.warnings.add('An unattached zero-width character cannot be positioned reliably; raw capture is retained.')
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
    raise RuntimeError('DejaVu Sans Mono fonts not found. Install fonts-dejavu or use --font-dir DIR.')


class Renderer:
    def __init__(self, font_paths: dict, width: int = 1894, font_size: int = 24, bold_bright: bool = True):
        self.fonts = {key:ImageFont.truetype(str(path),font_size,layout_engine=ImageFont.Layout.BASIC) for key,path in font_paths.items()}
        regular = self.fonts[False,False]
        self.cell_width = math.ceil(regular.getlength('M'))
        self.ascent, self.descent = regular.getmetrics()
        self.line_height = max(self.ascent + self.descent, math.ceil(font_size * 1.375))
        self.width = width
        self.font_size = font_size
        self.bold_bright = bold_bright
        self.padding_left, self.padding_top, self.padding_bottom = 5, 0, 5

    def render(self, rows: list[list[Glyph]], columns: int, destination: Path, *,
               source_rows: list[int | None] | None = None,
               report_width_mm: float | None = None, min_font_pt: float | None = None):
        if source_rows is not None and len(source_rows) != len(rows):
            raise ValueError('source_rows must contain exactly one entry per rendered row.')
        if report_width_mm is not None and (not math.isfinite(report_width_mm) or report_width_mm <= 0):
            raise ValueError('report_width_mm must be finite and positive.')
        if min_font_pt is not None and (not math.isfinite(min_font_pt) or min_font_pt <= 0):
            raise ValueError('min_font_pt must be finite and positive.')
        if min_font_pt is not None and report_width_mm is None:
            raise ValueError('min_font_pt requires report_width_mm.')
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
        native_ink = ImageChops.difference(image,Image.new('RGB',image.size,BACKGROUND)).getbbox()
        height = max(1,round(native_height * self.width / native_width))
        if image.size != (self.width,height):
            image = image.resize((self.width,height),Image.Resampling.LANCZOS)
        image.save(destination,'PNG')
        sx,sy = self.width/native_width,height/native_height
        row_map = []
        for i,capture_row in enumerate(source_rows if source_rows is not None else [None]*len(rows)):
            top = self.padding_top+i*self.line_height
            row_map.append({'capture_row':capture_row,'native_y':[top,top+self.line_height],
                            'image_y':[top*sy,(top+self.line_height)*sy]})
        final_ink = ImageChops.difference(image,Image.new('RGB',image.size,BACKGROUND)).getbbox()
        geometry = {
            'native_width':native_width,'native_height':native_height,
            'cell_width_px':self.cell_width,'row_height_px':self.line_height,
            'font_size_px':self.font_size,'ascent_px':self.ascent,'descent_px':self.descent,
            'scale_x':sx,'scale_y':sy,
            'padding_native':{'left':self.padding_left,'top':self.padding_top,
                              'right':self.padding_left,'bottom':self.padding_bottom},
            'padding_final':{'left':self.padding_left*sx,'top':self.padding_top*sy,
                             'right':self.padding_left*sx,'bottom':self.padding_bottom*sy},
            'grid_bounds_native':[self.padding_left,self.padding_top,
                                  self.padding_left+columns*self.cell_width,self.padding_top+len(rows)*self.line_height],
            'content_bounds_native':list(native_ink) if native_ink else None,
            'content_bounds_final':list(final_ink) if final_ink else None,
            'source_row_map':row_map,
            'coordinate_convention':'Bounds are half-open [left, top, right, bottom]; row Y intervals are half-open. Capture rows are 1-based.'
        }
        warnings = []
        print_info = None
        if report_width_mm is not None:
            effective_font_pt = self.font_size*sy*report_width_mm*72/(25.4*self.width)
            print_info = {'report_width_mm':report_width_mm,'effective_font_pt':effective_font_pt,
                          'effective_cell_width_pt':self.cell_width*sx*report_width_mm*72/(25.4*self.width),
                          'min_font_pt':min_font_pt,
                          'below_minimum':min_font_pt is not None and effective_font_pt < min_font_pt}
            if print_info['below_minimum']:
                warnings.append(f'Effective printed font is {effective_font_pt:.2f} pt, below {min_font_pt:g} pt at {report_width_mm:g} mm. '
                                'Use a wider physical report width, landscape pages, or explicit column splitting. '
                                'Increasing PNG pixel width alone does not increase printed font size.')
        return {'width':self.width,'height':height,'columns':columns,'row_count':len(rows),
                'geometry':geometry,'print':print_info,'warnings':warnings}


def row_has_visible_content(row: list[Glyph], bold_bright: bool = True) -> bool:
    """Conservatively retain every cell that can affect the rendered image.

    Foreground-only styles on spaces (e.g. bold or green) are not visible.
    Decorations are retained even if hidden or matching the background; deleting
    a potentially styled row is riskier than retaining a harmless blank row.
    """
    for glyph in row:
        fg,bg = colors(glyph.style,bold_bright)
        if bg != BACKGROUND or glyph.style.underline or glyph.style.strike or glyph.style.overline:
            return True
        if not glyph.style.hidden and fg != bg and any(not char.isspace() for char in glyph.text):
            return True
    return False


def _validated_ranges(ranges, total_rows: int | None, label: str) -> list[tuple[int,int]]:
    result = []
    for value in ranges:
        if not isinstance(value,(list,tuple)) or len(value) != 2 or any(isinstance(x,bool) or not isinstance(x,int) for x in value):
            raise ValueError(f'{label} must contain integer (first, last) pairs.')
        first,last = value
        if first < 1 or last < first or (total_rows is not None and last > total_rows):
            limit = f'available 1:{total_rows} rows' if total_rows is not None else 'valid positive inclusive intervals'
            raise ValueError(f'{label} {first}:{last} is outside the {limit}.')
        if result and first <= result[-1][1]:
            raise ValueError(f'{label} must be ordered and non-overlapping.')
        result.append((first,last))
    return result


def _page_plan(ranges: list[dict], capacity: int, blocks: list[tuple[int,int]]) -> list[dict]:
    plan = []
    for selection_index,selection in enumerate(ranges,1):
        first,last = selection['retained_first_row'],selection['retained_last_row']
        if first is None:
            continue
        pos = first
        while pos <= last:
            stop = min(pos+capacity-1,last)
            # Prefer a block boundary instead of splitting a command/output
            # block. A block longer than a page is split when it starts the page.
            for block_first,block_last in blocks:
                clipped_first,clipped_last = max(first,block_first),min(last,block_last)
                if pos < clipped_first <= stop < clipped_last:
                    stop = clipped_first-1
                    break
            segments = []
            for block_index,(block_first,block_last) in enumerate(blocks,1):
                a,b = max(pos,block_first),min(stop,block_last)
                if a <= b:
                    segments.append({'block':block_index,'block_first_row':block_first,'block_last_row':block_last,
                                     'first_row':a,'last_row':b,'continues_before':a>block_first,'continues_after':b<block_last})
            plan.append({'first_row':pos,'last_row':stop,'selection_index':selection_index,'blocks':segments})
            pos = stop+1
    for block_index in range(1,len(blocks)+1):
        pieces = [segment for page in plan for segment in page['blocks'] if segment['block']==block_index]
        for index,piece in enumerate(pieces,1):
            piece.update(part=index,parts=len(pieces))
    return plan


def render_capture(capture_path: Path, output_dir: Path, columns: int, renderer: Renderer, *,
                   rows_per_image: int = 40, select_rows: list[tuple[int,int]] | None = None,
                   trim_empty_bottom: bool = False, padding_rows: int = 0,
                   fullscreen: bool = False, trim_fullscreen: bool = False,
                   blocks: list[tuple[int,int]] | None = None,
                   report_width_mm: float | None = None, min_font_pt: float | None = None) -> dict:
    """Render a saved ANSI grid without modifying or connecting to its source.

    Pass one parses the whole capture to find safe trailing trim boundaries.
    Pass two parses it again and renders only planned pages. Memory use is
    bounded by a page plus its metadata, rather than the complete scrollback.
    User ranges and block ranges use original, 1-based, inclusive capture rows.
    """
    capture_path,output_dir = Path(capture_path),Path(output_dir)
    if rows_per_image < 1 or padding_rows < 0 or columns < 1:
        raise ValueError('rows_per_image and columns must be positive; padding_rows must be nonnegative.')
    if select_rows is not None and not select_rows:
        raise ValueError('select_rows cannot be an empty list.')
    requested = list(select_rows) if select_rows is not None else None
    if requested is not None:
        # Validate interval shape/order before reading, and bounds afterwards.
        _validated_ranges(requested,None,'Selected row ranges')
    parser = CaptureParser()
    first_hash = hashlib.sha256()
    total_rows = 0
    last_visible = [None]*(len(requested) if requested is not None else 1)
    active = 0
    with capture_path.open('rb') as stream:
        for total_rows,raw in enumerate(stream,1):
            first_hash.update(raw)
            row = parser.line(raw.decode('utf-8',errors='replace'))
            if requested is not None:
                while active < len(requested) and total_rows > requested[active][1]:
                    active += 1
                if active == len(requested) or total_rows < requested[active][0]:
                    continue
            if row_has_visible_content(row,renderer.bold_bright):
                last_visible[active] = total_rows
    warnings = set(parser.warnings)
    requested = _validated_ranges(requested,total_rows,'Selected row ranges') if requested is not None else ([(1,total_rows)] if total_rows else [])
    block_ranges = _validated_ranges(blocks or [],total_rows,'Command blocks')
    apply_trim = trim_empty_bottom and (not fullscreen or trim_fullscreen)
    if trim_empty_bottom and fullscreen and not trim_fullscreen:
        warnings.add('Automatic bottom trim was skipped for a fullscreen/alternate buffer; explicitly enable trim_fullscreen to override.')
    selections = []
    for index,(first,last) in enumerate(requested):
        visible_end = last_visible[index]
        retained_end = last
        if apply_trim:
            retained_end = min(last,(visible_end if visible_end is not None else first-1)+padding_rows)
        count = max(0,retained_end-first+1)
        trim_first = retained_end+1
        selections.append({'first_row':first,'last_row':last,'selected_row_count':last-first+1,
                           'last_visible_row':visible_end,'retained_first_row':first if count else None,
                           'retained_last_row':retained_end if count else None,'rendered_row_count':count,
                           'trimmed_bottom_rows':last-retained_end,
                           'trimmed_row_ranges':[[trim_first,last]] if trim_first <= last else [],
                           'kept_padding_rows':max(0,retained_end-(visible_end if visible_end is not None else first-1)) if apply_trim else 0})
    plan = _page_plan(selections,rows_per_image,block_ranges)
    images = []
    output_dir.mkdir(parents=True,exist_ok=True)
    second_hash = hashlib.sha256()
    parser = CaptureParser()
    pending = []
    page_index = 0
    with capture_path.open('rb') as stream:
        for number,raw in enumerate(stream,1):
            second_hash.update(raw)
            row = parser.line(raw.decode('utf-8',errors='replace'))
            if page_index >= len(plan):
                continue
            page = plan[page_index]
            if page['first_row'] <= number <= page['last_row']:
                pending.append(row)
            if number == page['last_row']:
                destination = output_dir/f'part-{page_index+1:04d}.png'
                source_rows = list(range(page['first_row'],page['last_row']+1))
                metrics = renderer.render(pending,columns,destination,source_rows=source_rows,
                                          report_width_mm=report_width_mm,min_font_pt=min_font_pt)
                warnings.update(metrics.get('warnings',[]))
                images.append(dict(file=str(destination.resolve()),part=page_index+1,parts=len(plan),
                                   first_row=page['first_row'],last_row=page['last_row'],
                                   selection_index=page['selection_index'],blocks=page['blocks'],**metrics))
                pending = []
                page_index += 1
    if second_hash.hexdigest() != first_hash.hexdigest():
        raise ValueError('The capture file changed while rendering; output is incomplete. The exporter did not modify it.')
    if page_index != len(plan):
        raise ValueError('The capture ended before all planned images were rendered.')
    warnings.update(parser.warnings)
    return {'images':images,'total_rows':total_rows,'warnings':sorted(warnings),
            'capture_sha256':first_hash.hexdigest(),'selected_ranges':[[a,b] for a,b in requested],
            'selections':selections,'trim':{'requested':trim_empty_bottom,'applied':apply_trim,
                                         'fullscreen':fullscreen,'fullscreen_override':trim_fullscreen,
                                         'padding_rows':padding_rows,
                                         'trimmed_bottom_rows':sum(s['trimmed_bottom_rows'] for s in selections)},
            'pagination':{'rows_per_image':rows_per_image,'mode':'explicit-blocks' if block_ranges else 'rows',
                          'blocks':[[a,b] for a,b in block_ranges]}}

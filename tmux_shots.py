#!/usr/bin/env python3
"""Capture tmux panes or render saved ANSI captures as Breeze PNGs."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib, json, math, re, shutil, subprocess, sys
import copy
import importlib.metadata
import platform
from pathlib import Path
from typing import Iterator
from breeze_core import (BACKGROUND, FOREGROUND, PALETTE, Indexed, Style, Glyph, rgb, colors, themed_color, CaptureParser, pages, find_fonts, Renderer)

__version__ = "0.2.0"
FULLSCREEN_COMMANDS = {'nano','vim','nvim','vi','emacs','less','more','top','htop','btop','watch','menuconfig'}


def utc_now():
    return datetime.now(timezone.utc).isoformat()

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

    def metadata(self, pid: str) -> dict:
        fields = ['pane_width','pane_height','alternate_on','pane_in_mode','pane_dead','history_size']
        values = self.text('display-message','-p','-t',pid,'\t'.join('#{'+x+'}' for x in fields)).split('\t')
        if len(values) != len(fields):
            raise TmuxError(f'Unexpected pane metadata for {pid}')
        return {key:int(value or '0') for key,value in zip(fields,values)}

    def capture(self, pane: dict, destination: Path, screen_only: bool, buffer_filter: str | None = None) -> Iterator[dict]:
        pid = pane['pane_id']
        pane.update(self.metadata(pid))
        try:
            command = self.text('display-message','-p','-t',pid,'#{pane_current_command}')
            # An unavailable format must not be confused with a program name.
            pane['current_command'] = command if command and '\t' not in command else None
        except TmuxError:
            pane['current_command'] = None
        pane['captured_at_utc'] = utc_now()
        buffers = ['current'] if not pane['alternate_on'] else (['alternate'] if screen_only else ['main','alternate'])
        if buffer_filter is not None:
            buffers = [name for name in buffers if name == buffer_filter]
        for buffer in buffers:
            before = self.metadata(pid)
            started = utc_now()
            arguments = ['capture-pane','-p','-e','-N','-t',pid,'-S','0' if screen_only else '-','-E','-']
            if buffer == 'main':
                arguments.append('-a')
            data = self.call(*arguments)
            finished = utc_now()
            warnings = []
            try:
                after = self.metadata(pid)
                if any(before[key] != after[key] for key in ('pane_width','pane_height')):
                    warnings.append('Pane dimensions changed during capture; row geometry may be inconsistent.')
                if before['alternate_on'] != after['alternate_on']:
                    warnings.append('The active screen buffer changed during capture.')
            except TmuxError as exc:
                after = None
                warnings.append(f'Could not inspect pane after capture: {exc}')
            folder = destination / buffer
            folder.mkdir(parents=True,exist_ok=True)
            path = folder/'capture.ansi'
            path.write_bytes(data)
            fullscreen = buffer == 'alternate' or (buffer == 'current' and pane.get('current_command') in FULLSCREEN_COMMANDS)
            yield {'buffer':buffer,'capture_file':path,'sha256':hashlib.sha256(data).hexdigest(),
                   'capture_command':self.prefix+arguments,'capture_started_at_utc':started,
                   'capture_finished_at_utc':finished,'geometry_before':before,'geometry_after':after,
                   'fullscreen':fullscreen,'warnings':warnings}


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


def row_ranges(values) -> list[tuple[int,int]] | None:
    if not values or values == ['all']:
        return None
    result = []
    for value in values:
        for item in value.split(','):
            match = re.fullmatch(r'\s*(\d+)\s*:\s*(\d+)\s*',item)
            if not match or int(match[1]) < 1 or int(match[2]) < int(match[1]):
                raise ValueError(f'Invalid row interval {item!r}; use 1-based inclusive START:END or all.')
            result.append((int(match[1]),int(match[2])))
    for previous,current in zip(result,result[1:]):
        if current[0] <= previous[1]:
            raise ValueError('Row intervals must be ascending and nonoverlapping.')
    return result


def validate_blocks(data):
    if isinstance(data,dict):
        if set(data) == {'blocks'}:
            return validate_blocks(data['blocks'])
        if set(data) != {'buffers'} or not isinstance(data['buffers'],dict) or not data['buffers']:
            raise ValueError('Blocks JSON must be a range list, {"blocks": [...]}, or {"buffers": {"%ID:buffer": [...]}}.')
        return {'buffers':{str(key):validate_blocks(value) for key,value in data['buffers'].items()}}
    if not isinstance(data,list):
        raise ValueError('Blocks must be a list of [first_row,last_row] pairs.')
    result = []
    for pair in data:
        if not isinstance(pair,(list,tuple)) or len(pair) != 2 or any(type(n) is not int for n in pair) or pair[0] < 1 or pair[1] < pair[0]:
            raise ValueError('Each block must be two positive 1-based inclusive row numbers.')
        if result and pair[0] <= result[-1][1]:
            raise ValueError('Blocks must be ascending and nonoverlapping.')
        result.append([pair[0],pair[1]])
    return result


def buffer_blocks(data, pane: dict, item: dict):
    if data is None:
        return None
    if isinstance(data,dict):
        mapping = data['buffers']
        keys = [pane['pane_id']+':'+item['buffer'],item.get('source_capture_file',''),item.get('capture_file','')]
        matches = [key for key in keys if key in mapping]
        if not matches:
            raise ValueError(f'No blocks metadata for {keys[0]!r}; provide that key or the capture-file path.')
        selected = mapping[matches[0]]
        if isinstance(selected,dict):
            raise ValueError('Per-buffer block entries must be range lists, not nested mappings.')
        return [tuple(pair) for pair in selected]
    return [tuple(pair) for pair in data]


def resolve_settings(args, source: dict | None = None) -> dict:
    previous = (source or {}).get('render_settings',{})
    settings = {}
    defaults = {'rows':40,'width':1894,'font_size':24,'no_bold_bright':False,
                'trim_empty_bottom':False,'padding_rows':0,'trim_fullscreen':False,
                'report_width_mm':None,'min_font_pt':None}
    aliases = {'rows':'rows_per_image','width':'image_width'}
    for key,default in defaults.items():
        explicit = getattr(args,key,None)
        inherited = previous.get(key,(source or {}).get(aliases.get(key,''),default))
        settings[key] = explicit if explicit is not None else inherited
    if not 1 <= settings['rows'] <= 500 or not 64 <= settings['width'] <= 16384 or not 8 <= settings['font_size'] <= 96:
        raise ValueError('Use rows 1..500, width 64..16384, and font-size 8..96.')
    if type(settings['padding_rows']) is not int or not 0 <= settings['padding_rows'] <= 500:
        raise ValueError('padding-rows must be an integer from 0 to 500.')
    for key in ('report_width_mm','min_font_pt'):
        value = settings[key]
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError(key.replace('_','-')+' must be a positive finite number.')
    if settings['min_font_pt'] is not None and settings['report_width_mm'] is None:
        raise ValueError('--min-font-pt requires --report-width-mm.')
    if settings['report_width_mm'] is not None and settings['min_font_pt'] is None:
        settings['min_font_pt'] = 8.0
    settings['select_rows'] = row_ranges(args.select_rows) if args.select_rows is not None else previous.get('select_rows')
    if args.blocks:
        settings['blocks'] = validate_blocks(json.loads(args.blocks.read_text(encoding='utf-8')))
    else:
        settings['blocks'] = previous.get('blocks')
        if settings['blocks'] is not None:
            settings['blocks'] = validate_blocks(settings['blocks'])
    return settings


def reproducibility(fonts: dict) -> dict:
    from PIL import features
    dependencies = {}
    for name in ('Pillow','wcwidth'):
        try:dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:dependencies[name] = 'unknown'
    dependencies['FreeType'] = features.version_module('freetype2') or 'unknown'
    return {'tool_version':__version__,'python':platform.python_version(),
            'platform':platform.platform(),'dependencies':dependencies,
            'fonts':[{'style':f'bold={bold},italic={italic}','path':str(path),
                      'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
                     for (bold,italic),path in sorted(fonts.items())]}


def new_output(args, label: str) -> Path:
    output = args.output or Path('tmux-shots')/(safe_name(label)+'-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'))
    output = output.expanduser().resolve()
    if output.exists():
        raise RuntimeError(f'Output directory already exists; choose a new --output path: {output}')
    output.mkdir(parents=True,mode=0o700)
    return output


def manifest_base(args, fonts, settings, source: str) -> dict:
    return {'format_version':2,'tool_version':__version__,'started_at_utc':utc_now(),
            'source':source,'theme':'Konsole Breeze','image_width':settings['width'],
            'rows_per_image':settings['rows'],'render_settings':settings,
            'screen_only':args.screen_only,'font_files':{str(k):str(v) for k,v in fonts.items()},
            'reproducibility':reproducibility(fonts),
            'selection':{'window':args.window,'pane':args.pane,'buffer':args.buffer},
            'notes':['Pane snapshots are sequential; running processes are not paused.',
                     'Only scrollback still retained by tmux is available.',
                     'No commands, prompts or colors are invented. No desktop frame or cursor is drawn.',
                     'Row indices are 1-based inclusive within each named source buffer.'],
            'panes':[],'warnings':[],'errors':[]}


def choose_font_dir(args, source: dict | None = None):
    if args.font_dir is not None:
        return args.font_dir
    # Prefer installed fonts so captures are portable across hosts. Legacy
    # absolute paths are only a fallback when they still exist on this machine.
    try:
        return next(iter(find_fonts(None).values())).parent
    except RuntimeError:
        for path in (source or {}).get('font_files',{}).values():
            directory = Path(path).parent
            try:find_fonts(directory);return directory
            except RuntimeError:pass
        return None


def render_jobs(jobs, output, manifest, renderer, settings):
    from breeze_core import render_capture
    for record,item,path in jobs:
        item['render_status'] = 'failed'
        try:
            columns = (item.get('geometry_before') or {}).get('pane_width',record['pane_width'])
            if type(columns) is not int or columns < 1:
                raise ValueError('Buffer capture width must be a positive integer.')
            result = render_capture(path,path.parent,columns,renderer,
                rows_per_image=settings['rows'],select_rows=settings['select_rows'],
                trim_empty_bottom=settings['trim_empty_bottom'],padding_rows=settings['padding_rows'],
                fullscreen=item.get('fullscreen',item['buffer']=='alternate'),
                trim_fullscreen=settings['trim_fullscreen'],blocks=buffer_blocks(settings['blocks'],record,item),
                report_width_mm=settings['report_width_mm'],min_font_pt=settings['min_font_pt'])
            capture_warnings = item.get('warnings',[])
            item.update(result)
            item['render_status'] = 'success'
            item['warnings'] = list(dict.fromkeys(capture_warnings+item.get('warnings',[])))
            item.setdefault('images',[])
            for image in item['images']:
                image['file'] = Path(image['file']).resolve().relative_to(output).as_posix()
            if not item['images'] and not result.get('trim',{}).get('applied'):
                manifest['warnings'].append(f"{record['pane_id']}:{item['buffer']}: no visible rows selected for rendering; ANSI retained.")
        except (OSError,ValueError,RuntimeError) as exc:
            manifest['errors'].append({'pane_id':record['pane_id'],'buffer':item['buffer'],'stage':'render','message':str(exc)})


def output_summary(output: Path | None, manifest: dict) -> dict:
    images = [image for pane in manifest['panes'] for buffer in pane['buffers'] for image in buffer.get('images',[])]
    warnings = list(manifest.get('warnings',[]))
    for pane in manifest['panes']:
        for buffer in pane['buffers']:
            warnings += [f"{pane['pane_id']}:{buffer['buffer']}: {value}" for value in buffer.get('warnings',[])]
    errors = manifest['errors']
    buffers = [buffer for pane in manifest['panes'] for buffer in pane['buffers']]
    successful_buffers = sum(buffer.get('render_status') == 'success' for buffer in buffers)
    status = ('partial' if successful_buffers else 'failed') if errors else ('warning' if warnings else 'success')
    return {'status':status,'manifest':str(output/'manifest.json') if output else None,
            'images':[str(output/image['file']) for image in images] if output else [],
            'counts':{'panes':len(manifest['panes']),'buffers':len(buffers),'images':len(images),
                      'successful_buffers':successful_buffers,
                      'warnings':len(warnings),'errors':len(errors)},'warnings':warnings,'errors':errors}


def complete_output(args, output, manifest):
    from breeze_artifacts import finalize_artifacts
    try:
        finalize_artifacts(output,manifest,preview=args.preview,simple_names=args.simple_names,
                           snippets=args.snippets,report_width_mm=manifest['render_settings']['report_width_mm'])
    except (OSError,ValueError,RuntimeError) as exc:
        manifest['errors'].append({'stage':'artifacts','message':str(exc)})
    manifest['finished_at_utc'] = utc_now()
    summary = output_summary(output,manifest)
    manifest['status'] = summary['status']
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if args.json:
        print(json.dumps(summary,ensure_ascii=False))
    else:
        print(f"Saved {summary['counts']['images']} PNGs from {summary['counts']['buffers']} buffers in {summary['counts']['panes']} panes.\n{output}")
    for warning in summary['warnings']:
        print('WARNING: '+warning,file=sys.stderr)
    for error in summary['errors']:
        print('ERROR: '+error['message'],file=sys.stderr)
    return output,1 if summary['errors'] else (2 if summary['warnings'] else 0)


def export_session(args, tmux: Tmux | None = None) -> tuple[Path,int]:
    settings = resolve_settings(args)
    fonts = find_fonts(choose_font_dir(args))
    renderer = Renderer(fonts,settings['width'],settings['font_size'],not settings['no_bold_bright'])
    tmux = tmux or Tmux(args.socket_name,args.socket,args.timeout)
    sid = tmux.session_id(args.session)
    inventory = list(tmux.panes(sid))
    if not inventory:
        raise TmuxError('The session has no panes.')
    inventory = select_panes(inventory,args.window,args.pane)
    output = new_output(args,args.session)
    manifest = manifest_base(args,fonts,settings,'tmux capture-pane cell grids')
    manifest.update(session_name=args.session,session_id=sid)
    try:
        manifest['tmux_version'] = tmux.text('-V')
    except (TmuxError,AssertionError):
        manifest['tmux_version'] = 'unknown'
    jobs = []
    for pane in inventory:
        folder = output/f"window-{pane['window_index']:03d}-{safe_name(pane['window_name'])}"/f"pane-{pane['pane_index']:02d}-{pane['pane_id'].lstrip('%')}"
        record = dict(pane,buffers=[])
        manifest['panes'].append(record)
        try:
            for item in tmux.capture(record,folder,args.screen_only,buffer_filter=args.buffer):
                path = item.pop('capture_file')
                item['capture_file'] = path.relative_to(output).as_posix()
                item['images'] = []
                record['buffers'].append(item)
                jobs.append((record,item,path))
        except (TmuxError,OSError,ValueError) as exc:
            manifest['errors'].append({'pane_id':pane['pane_id'],'stage':'capture','message':str(exc)})
    if args.buffer and not jobs:
        manifest['errors'].append({'stage':'selection','message':f'No {args.buffer!r} buffer exists in the selected panes.'})
    render_jobs(jobs,output,manifest,renderer,settings)
    return complete_output(args,output,manifest)


def capture_path(base: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        raise ValueError('Manifest capture paths must be relative to the manifest directory.')
    resolved = (base/path).resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ValueError('Capture path escapes the manifest directory.')
    return resolved


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()


def render_saved(args) -> tuple[Path,int]:
    source_path = args.manifest.expanduser().resolve()
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes)
    if not isinstance(source,dict) or source.get('format_version',1) not in (1,2) or not isinstance(source.get('panes'),list):
        raise ValueError('Unsupported or malformed capture manifest.')
    settings = resolve_settings(args,source)
    fonts = find_fonts(choose_font_dir(args,source))
    renderer = Renderer(fonts,settings['width'],settings['font_size'],not settings['no_bold_bright'])
    selected = select_panes(source['panes'],args.window,args.pane)
    prepared = []
    for record in selected:
        if not isinstance(record.get('pane_width'),int) or record['pane_width'] < 1:
            raise ValueError('Each saved pane must include a positive pane_width.')
        for item in record.get('buffers',[]):
            if args.buffer and item['buffer'] != args.buffer:
                continue
            path = capture_path(source_path.parent,item['capture_file'])
            digest = file_sha256(path)
            if not item.get('sha256'):
                raise ValueError(f'Missing SHA-256 for capture {item["capture_file"]!r}; cannot verify source.')
            if digest != item['sha256']:
                raise ValueError(f'Capture SHA-256 mismatch: {item["capture_file"]!r}. Source was not modified by this tool.')
            prepared.append((record,item,path))
    if not prepared:
        raise ValueError('No saved buffers match the selection.')
    output = new_output(args,str(source.get('session_name','render'))+'-render')
    manifest = manifest_base(args,fonts,settings,'saved ANSI capture')
    manifest.update(session_name=source.get('session_name'),session_id=source.get('session_id'),
                    source_manifest={'path':str(source_path),'sha256':hashlib.sha256(source_bytes).hexdigest(),
                                     'format_version':source.get('format_version',1)},
                    tmux_version=source.get('tmux_version','unknown'))
    old_repro = source.get('reproducibility',{})
    manifest['source_reproducibility'] = old_repro
    old_fonts = {font['style']:font['sha256'] for font in old_repro.get('fonts',[]) if 'style' in font and 'sha256' in font}
    new_fonts = {font['style']:font['sha256'] for font in manifest['reproducibility']['fonts']}
    if old_fonts and old_fonts != new_fonts:
        manifest['warnings'].append('Font fingerprints differ from the source render; exact pixel reproduction is not guaranteed.')
    if old_repro.get('dependencies') and old_repro['dependencies'] != manifest['reproducibility']['dependencies']:
        manifest['warnings'].append('Rendering dependency versions differ from the source manifest.')
    records = {}
    jobs = []
    used_destinations = set()
    for source_record,source_item,path in prepared:
        identity = (source_record['window_index'],source_record['pane_id'])
        if identity not in records:
            record = copy.deepcopy(source_record)
            record['buffers'] = []
            manifest['panes'].append(record)
            records[identity] = record
        record = records[identity]
        item = copy.deepcopy(source_item)
        item['images'] = []
        item['warnings'] = []
        # Normalize paths without using window names as filesystem input.
        relative = Path(f"window-{record['window_index']:03d}-{safe_name(record['window_name'])}")/f"pane-{record['pane_index']:02d}-{record['pane_id'].lstrip('%')}"/safe_name(item['buffer'])/'capture.ansi'
        destination = output/relative
        if destination in used_destinations:
            raise ValueError('Duplicate buffer destination in manifest.')
        used_destinations.add(destination)
        destination.parent.mkdir(parents=True,exist_ok=True)
        with path.open('rb') as source_stream,destination.open('xb') as output_stream:
            shutil.copyfileobj(source_stream,output_stream,length=1024*1024)
        if file_sha256(destination) != source_item['sha256']:
            raise ValueError('The capture changed while it was being copied; source files were not modified by this tool.')
        item['source_capture_file'] = source_item['capture_file']
        item['capture_file'] = relative.as_posix()
        record['buffers'].append(item)
        jobs.append((record,item,destination))
    render_jobs(jobs,output,manifest,renderer,settings)
    return complete_output(args,output,manifest)


class UsageError(ValueError):
    pass


class ArgumentParser(argparse.ArgumentParser):
    def error(self,message):
        raise UsageError(message)

    def print_help(self,file=None):
        if getattr(self,'json_help',False):
            print(json.dumps({'tool':'tmux-breeze','version':__version__,'help':self.format_help()}),file=file)
        else:
            super().print_help(file=file)


def arguments(argv=None):
    values = list(sys.argv[1:] if argv is None else argv)
    command = values.pop(0) if values and values[0] in ('capture','render') else 'capture'
    parser = ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter,
                            epilog='Modes: tmux_shots.py [capture] SESSION ... | tmux_shots.py render MANIFEST ...')
    parser.json_help = '--json' in values
    parser.add_argument('--version',action='version',version=__version__)
    parser.add_argument('target',help='Saved manifest.json path' if command=='render' else 'Exact tmux session name or $ID')
    parser.add_argument('-w','--window',help='Window index, @ID, or exact name (name:NAME forces a literal name)')
    parser.add_argument('-p','--pane',help='Pane %%ID, WINDOW_INDEX.PANE_INDEX, or pane index with --window')
    parser.add_argument('--buffer',choices=['current','main','alternate'],help='Restrict rendering to this named buffer')
    parser.add_argument('-o','--output',type=Path,help='New output directory; existing directories are never overwritten')
    parser.add_argument('--rows',type=int,help='Maximum physical source rows per PNG (default: 40, or saved setting)')
    parser.add_argument('--width',type=int,help='Exact PNG width in pixels (default: 1894, or saved setting)')
    parser.add_argument('--font-size',type=int,help='Native font size before uniform scaling (default: 24)')
    parser.add_argument('--font-dir',type=Path,help='Directory containing four DejaVuSansMono font variants')
    parser.add_argument('--screen-only',action='store_true',help='Capture active screen only; no scrollback or saved main buffer')
    parser.add_argument('--no-bold-bright',action='store_true',default=None,help='Keep bold system colors at normal palette intensity')
    parser.add_argument('--trim-empty-bottom',action=argparse.BooleanOptionalAction,default=None,help='Remove only trailing visually empty rows')
    parser.add_argument('--trim-fullscreen',action=argparse.BooleanOptionalAction,default=None,help='Explicitly allow trimming fullscreen buffers')
    parser.add_argument('--padding-rows',type=int,help='Blank rows retained after bottom trimming (default: 0)')
    parser.add_argument('--select-rows',action='append',metavar='START:END',help='1-based inclusive rows per selected buffer; repeat or comma-separate; all resets saved selection')
    parser.add_argument('--blocks',type=Path,help='JSON explicit command/output row intervals, optionally mapped by %%ID:buffer')
    parser.add_argument('--report-width-mm',type=float,help='Physical image width in the printed report')
    parser.add_argument('--min-font-pt',type=float,help='Warn if effective printed font is smaller (default with report width: 8pt)')
    parser.add_argument('--preview',action='store_true',help='Create contact-sheet.png and preview.html')
    parser.add_argument('--simple-names',action='store_true',help='Give images short collision-free names with source row ranges')
    parser.add_argument('--snippets',choices=['latex','markdown','both'],help='Generate relative image inclusion snippets')
    parser.add_argument('--json',action='store_true',help='Write only a structured result object to stdout')
    socket = parser.add_mutually_exclusive_group()
    socket.add_argument('-L','--socket-name',help='tmux server socket name')
    socket.add_argument('-S','--socket',help='tmux server socket path')
    parser.add_argument('--timeout',type=float,default=30,help='Timeout per tmux request in seconds (default: 30)')
    args = parser.parse_args(values)
    args.command = command
    args.session = args.target if command == 'capture' else None
    args.manifest = Path(args.target) if command == 'render' else None
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('timeout must be a positive finite number.')
    if command == 'render' and (args.screen_only or args.socket_name or args.socket):
        parser.error('render is offline; --screen-only and socket options apply only to capture.')
    return args


def main(argv=None):
    values = list(sys.argv[1:] if argv is None else argv)
    json_mode = '--json' in values
    if '--version' in values and json_mode:
        print(json.dumps({'tool':'tmux-breeze','version':__version__}))
        return 0
    try:
        args = arguments(values)
        _,code = render_saved(args) if args.command == 'render' else export_session(args)
        return code
    except (TmuxError,RuntimeError,OSError,ValueError,TypeError,KeyError) as exc:
        error = {'stage':'setup','message':str(exc)}
        if json_mode:
            print(json.dumps({'status':'failed','manifest':None,'images':[],
                              'counts':{'panes':0,'buffers':0,'images':0,'successful_buffers':0,'warnings':0,'errors':1},
                              'warnings':[],'errors':[error]},ensure_ascii=False))
        print('ERROR: '+str(exc),file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if json_mode:
            print(json.dumps({'status':'failed','manifest':None,'images':[],'counts':{},'warnings':[],
                              'errors':[{'stage':'interrupted','message':'Interrupted; captured files remain on disk.'}]}))
        print('Interrupted. Already captured files remain in the output directory.',file=sys.stderr)
        return 130
    except SystemExit as exc:
        # argparse help/version are successful informational commands, including
        # the JSON-only equivalents, and must not abort embedded main() callers.
        return int(exc.code or 0)


if __name__ == '__main__':
    raise SystemExit(main())

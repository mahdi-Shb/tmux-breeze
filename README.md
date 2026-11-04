# tmux-breeze

Capture tmux panes and retained scrollback, then render them as paginated PNGs
using the Konsole Breeze palette. Rerender saved captures offline when preparing
reports: trim empty tails, select source-row ranges, and review print readability
without running the original commands again.

Requires Python 3.10+, Pillow, wcwidth and the four DejaVu Sans Mono font variants.
A live capture also requires tmux; offline rendering does not. The exporter reads
pane grids without sending keys, changing layouts or resizing windows.

## Install

On Debian or Ubuntu:

```bash
sudo apt install tmux fonts-dejavu python3-venv
git clone https://github.com/mahdi-Shb/tmux-breeze.git
cd tmux-breeze
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
tmux-breeze --version
```

`fonts-dejavu` includes the required regular, bold, oblique and bold-oblique
monospace fonts. Installing only `fonts-dejavu-core` is insufficient on current
Debian releases. A custom font directory can be passed with `--font-dir`.

For a source checkout without installing the command, use the distribution's
Python packages and replace `tmux-breeze` below with `python3 tmux_shots.py`:

```bash
sudo apt install tmux python3-pil python3-wcwidth fonts-dejavu
```

## Capture

Run as the user who owns the tmux server. Replace `lab` with the exact session
name. Each explicit output directory must be new; omit `-o` for an automatically
named directory.

```bash
# All windows/panes, including retained history
tmux-breeze capture lab -o shots

# The original invocation remains supported
tmux-breeze lab -o another-capture

# A single window, by index or exact name
tmux-breeze lab --window 2 -o window-2
tmux-breeze lab --window build -o build-window

# A single pane, by index pair or stable pane ID
tmux-breeze lab --window 2 --pane 1 -o pane-2-1
tmux-breeze lab --pane 2.1 -o pane-2-1-again
tmux-breeze lab --pane '%7' -o pane-7

# Current screen only, excluding scrollback
tmux-breeze lab --screen-only -o current
```

Defaults remain **1894 pixels wide**, **up to 40 physical source rows per image**,
all retained history in chronological order, small margins and no window frame.
Automatic trimming is opt-in. Blank lines between outputs are preserved.

Window selectors accept an index, `@ID`, or exact name. Use `name:2` for a window
whose literal name is a number. A bare pane index requires `--window`; `%ID` and
`WINDOW_INDEX.PANE_INDEX` work independently. Every selection is scoped to the
named session. Missing or ambiguous selectors are rejected.

If an editor uses the alternate screen, the saved normal grid is stored under
`main/` and the active editor under `alternate/`. Only the editor's currently
visible screen is captured; its whole file is not scrolled or exported. A normal
shell buffer is named `current`.

## Rerender offline

```bash
tmux-breeze render shots/manifest.json \
  --trim-empty-bottom --padding-rows 1 --width 1800 -o report-images
```

Rendering uses the saved ANSI and metadata, without contacting tmux. Original
captures, their manifests and existing images are left untouched. A new output
directory contains copied raw captures and a new manifest. Raw SHA-256 values are
checked before rendering. Version 1 capture manifests are accepted as well as
version 2. Missing source files and hash mismatches are reported as errors.

Rendering settings are recorded in the manifest and available for offline reuse;
explicit command-line options override stored settings. Font hashes and runtime
versions help identify differences between machines rather than assuming that
matching font paths mean identical files.

### Safe trimming

`--trim-empty-bottom` removes only blank rows at a selected segment's trailing
edge. Spaces with a visible nondefault background, reverse display, underline,
strike or other visible decoration are retained. Interior blank rows are never
removed. `--padding-rows N` keeps a small row margin and is recorded in the row
mapping. The raw ANSI is not modified.

Full-screen/alternate-screen views are protected from automatic trimming unless
`--trim-fullscreen` is also explicitly requested. This prevents editor layouts
and menus from being silently shortened. The protection is recorded in metadata.

### Select one or several ranges

```bash
tmux-breeze render shots/manifest.json --pane '%7' --buffer current \
  --select-rows 1:16 --select-rows 30:45 -o selected

# A comma-separated form is equivalent
tmux-breeze render shots/manifest.json --buffer current \
  --select-rows 1:16,30:45 -o selected-again
```

Rows are **1-based and inclusive**, counted from the oldest retained row of the
specific saved buffer, including original blank rows. Each range is rendered
separately and can produce several pages. The same requested ranges apply to
each selected buffer, so use `--pane` and `--buffer current|main|alternate` to
narrow the scope. Window and pane selectors also work in offline mode.

The ANSI parser consumes preceding rows before selecting the range. Colors and
styles established before the first selected row therefore remain effective.
Selections and omitted/trimmed row ranges are recorded in the new manifest.

### Keep command/output blocks together

Prompt detection from `$` or `#` is deliberately not used. Supply trusted row
blocks to guide pagination:

```json
{"blocks": [[1, 12], [13, 29], [30, 48]]}
```

```bash
tmux-breeze render shots/manifest.json --blocks blocks.json --rows 40 -o blocks
```

A plain list `[[1,12],[13,29]]` is also accepted. Blocks are 1-based, inclusive,
ordered and nonoverlapping; they guide page boundaries rather than filtering
rows. Use `--select-rows` to omit rows. A block longer than the page capacity is
split, with source-row mapping and page information retained in the manifest.

For per-buffer definitions, use a `buffers` mapping keyed by `%PANE_ID:BUFFER`
or the original manifest's relative capture path:

```json
{
  "buffers": {
    "%7:current": [[1,12],[13,29]],
    "%8:alternate": [[1,24]]
  }
}
```

Every selected buffer needs a matching entry in this scoped form. Missing keys
are errors rather than a fallback to a guessed prompt boundary. Native shell
integration events are not currently collected; explicit trusted blocks provide
the supported block metadata.

## Geometry and print readability

```bash
tmux-breeze render shots/manifest.json --report-width-mm 170 --min-font-pt 8 \
  --preview -o print-check
```

The physical report width determines the effective text size after placing the
whole image in a document. A warning is emitted when it falls below the requested
minimum. Merely raising PNG pixel width improves resolution, **not physical font
size at the same printed width**. Increase the physical placement width, use a
landscape page, or explicitly split the source into narrower column views in a
separate workflow. This tool never resizes a live pane or reflows captured text.

Each image's `geometry` records native cell width, row height and font size,
native/final image dimensions, actual horizontal/vertical scaling factors,
padding, grid/content bounds and `source_row_map`. Each row-map entry identifies
the original capture row and half-open native/final vertical pixel bounds.
Selected rows and removed trailing rows are also recorded. The print calculation
and any image warnings accompany the image record.

## Preview and report helpers

```bash
tmux-breeze render shots/manifest.json --trim-empty-bottom --preview \
  --simple-names --snippets both --report-width-mm 170 -o report-ready
```

- `--preview` writes `preview.html` and contact-sheet PNGs. The HTML lists window,
  pane, buffer, source-row range, dimensions and readability warnings. All labels
  are outside the terminal images. Collections larger than 60 images get several
  contact sheets instead of a single unbounded bitmap.
- `--simple-names` copies images into `images/` using names such as
  `lifetime-p02-rows-0001-0016.png`. Collisions across windows, buffers, repeated
  ranges or pages get deterministic identity suffixes. Original paths and the
  complete source identity stay in the manifest; original PNGs remain available.
- `--snippets latex|markdown|both` writes `images.tex` and/or `images.md` with
  relative paths and TODO caption placeholders, not a full report. Use the output
  directory as the snippet's path base. Markdown paths are URL-encoded. TeX-special
  or non-ASCII filenames get byte-identical ASCII aliases in `latex-images/`;
  their mapping is recorded. Ordinary underscore-containing paths use
  `\detokenize`. LaTeX snippets require `graphicx`; captions can be edited normally.

## Manifest and machine-readable output

The manifest retains raw ANSI and hashes, session/window/pane identities, buffer
capture start/end timestamps, before/after pane dimensions and resize warnings,
render settings, tool/dependency versions, font file SHA-256 values, image row
ranges, geometry, print information and artifact paths. Captures are sequential,
not a paused or atomic snapshot of the entire session.

```bash
tmux-breeze render shots/manifest.json --preview --json -o automated
```

For capture/render with `--json`, stdout contains one JSON object only. Progress
and diagnostics go to stderr. The summary includes `status`, absolute `manifest`
and image paths, counts, warnings and errors. Status values distinguish
`success`, `warning`, `partial` and `failed`.

| Exit code | Meaning |
| --- | --- |
| 0 | Complete output without warnings |
| 1 | Error, including partial output; successful files are retained |
| 2 | Complete output with rendering, reproducibility or readability warnings |
| 130 | Interrupted by the user |

Other options: `--rows`, `--width`, `--font-size`, `--font-dir`,
`--no-bold-bright`, `-L/--socket-name`, `-S/--socket`, `--timeout`, `--version`.
Use `tmux-breeze --help` and `tmux-breeze render --help` for full argument details.

## Boundaries and tests

Only history still retained by tmux is available. Deleted/truncated history is
not recoverable, and raising `history-limit` is not retroactive. Colors must
already be present in tmux; plain output is not recolored. Captures can observe
ongoing changes, and panes whose geometry changes are flagged. Running from a
terminal outside the target session keeps the exporter invocation out of that
session's captured content.

These are rendered cell-grid exports, not desktop screenshots. The official
Breeze palette is used, but Pillow font rasterization need not match Qt pixel for
pixel. No cursor or window frame is invented. Sixel/Kitty graphics, font fallback
and full complex-script shaping are not implemented. Combining characters,
double-width characters, emoji sequences, Persian/mixed text and alternate
buffers have regression coverage; such coverage does not claim complete RTL,
font-coverage or emoji rendering support. Respect recorded rendering warnings.

```bash
python3 -m unittest -v
```

Tests cover attribute continuity, safe blank-tail trimming, source ranges,
pagination, row geometry, print metrics, offline rerendering and raw integrity,
scoped target selection, previews/snippets, JSON output and partial failures. On
Linux/Unix with tmux installed, an isolated disposable server also exercises live
capture. Tests alone create and close that test server; the exporter never does.

- [راهنمای فارسی](docs/USAGE_FA.txt)
- [tmux capture-pane](https://man.openbsd.org/tmux#capture-pane)
- [Konsole Breeze palette](https://github.com/KDE/konsole/blob/master/data/color-schemes/Breeze.colorscheme)

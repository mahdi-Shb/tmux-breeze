# tmux-breeze

Export tmux pane contents and scrollback as paginated PNG images using the
Konsole Breeze palette. Runs headlessly on Linux; no desktop session or browser
is required.

The exporter reads the pane cell grids already retained by tmux. It preserves
their text, spacing and ANSI attributes without running commands inside panes,
changing the layout, or adding prompts or inferred colors.

## Install

Requires Python 3.10 or newer, tmux, Pillow, wcwidth and DejaVu Sans Mono fonts.
On Debian or Ubuntu:

```bash
sudo apt install tmux python3-pil python3-wcwidth fonts-dejavu-core
git clone https://github.com/mahdi-Shb/tmux-breeze.git
cd tmux-breeze
```

Alternatively, install Python dependencies in a virtual environment with
`python -m pip install -r requirements.txt`. tmux and the fonts are still needed.

## Usage

Run as the user who owns the tmux server. Replace `lab` with your exact session
name. Each explicitly chosen output directory must be new; omit `-o` to create
an automatically named directory.

```bash
# Every window and pane, including retained scrollback
python3 tmux_shots.py lab -o ./shots

# One window, by index or exact name
python3 tmux_shots.py lab --window 2 -o ./window-2
python3 tmux_shots.py lab --window build -o ./build-window

# One pane, by window/pane index or stable pane ID
python3 tmux_shots.py lab --window 2 --pane 1 -o ./pane-2-1
python3 tmux_shots.py lab --pane 2.1 -o ./pane-2-1-again
python3 tmux_shots.py lab --pane '%7' -o ./pane-7

# Current screen only
python3 tmux_shots.py lab --screen-only -o ./current
```

Defaults are **1894 pixels wide**, **up to 40 physical rows per image**, all
retained history in chronological order, small margins, and terminal content
only. The final image contains the remaining rows rather than artificial padding.

Window selectors accept an index, `@ID`, or exact name. Prefix a numeric window
name with `name:` to distinguish it from an index. A bare pane index requires
`--window`; pane IDs and `WINDOW_INDEX.PANE_INDEX` work independently. Selection
is always restricted to the named session, and ambiguous selectors are rejected.

Other options include `--rows`, `--width`, `--font-size`, `--font-dir`,
`--no-bold-bright`, `-L/--socket-name`, `-S/--socket`, and `--timeout`.
Use `python3 tmux_shots.py --help` for details.

## Output

```text
shots/
  manifest.json
  window-000-build/
    pane-00-3/
      current/
        capture.ansi
        part-0001.png
        part-0002.png
```

The manifest records pane identities, capture times, raw-capture SHA-256 hashes,
image dimensions and row ranges, selection filters, and any errors or warnings.
Raw ANSI captures are retained alongside the images for inspection.

For programs using an alternate screen, such as an editor, the saved normal
buffer is exported under `main/`, and the active program screen under
`alternate/`. Only the editor's currently visible screen is available; the
exporter does not scroll through the file. `--screen-only` exports just the
active screen.

The application returns 0 for success, 1 for an error, 2 for parser warnings, or
130 for interruption. If a pane disappears, successfully captured panes are
retained and errors are recorded in the manifest.

## Boundaries

- Only history still retained by tmux is available. Deleted or truncated history
  cannot be recovered, and increasing `history-limit` is not retroactive.
- Colors must already be present in tmux. Plain output is not recolored.
- Panes are captured sequentially; running processes are not paused. Run from
  outside the target session where practical to keep the invocation out of its
  contents.
- Physical row boundaries are preserved rather than reflowed. Wide panes scale
  uniformly to the requested pixel width, so their text may appear smaller.
- These are rendered exports, not desktop screenshots. The official Breeze
  palette is used, but Pillow rendering is not guaranteed to match Qt/Konsole
  pixel for pixel. There is no window frame, scrollbar or synthetic cursor.
- Font fallback, complex-script shaping, emoji clusters and Sixel/Kitty images
  are not implemented. The focus is ordinary terminal text and code.

## Tests

```bash
python3 -m unittest -v
```

The suite covers ANSI and truecolor attributes, page continuity, whitespace,
target selection, alternate-buffer handling, partial failures and PNG rendering.
On Linux/Unix with tmux available, it also creates a disposable isolated tmux
server for an integration test, then shuts down only that test server. The
exporter itself never creates or shuts down servers.

The GitHub Actions workflow runs the suite on Linux with tmux and fonts installed.

## Documentation and references

- [راهنمای فارسی](docs/USAGE_FA.txt)
- [tmux capture-pane documentation](https://man.openbsd.org/tmux#capture-pane)
- [Konsole Breeze palette](https://github.com/KDE/konsole/blob/master/data/color-schemes/Breeze.colorscheme)

"""Optional gallery, short image names and report insertion snippets.

All labels live outside the terminal PNG content. Raw captures are never edited.
"""
from __future__ import annotations

from collections import Counter
import html
import math
from pathlib import Path
import re
import shutil
from urllib.parse import quote

from PIL import Image, ImageDraw, ImageFont


def _relative_file(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or "\\" in relative:
        raise ValueError(f"Expected a relative artifact path: {relative!r}")
    target = (root / path).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"Artifact path escapes output directory: {relative!r}")
    if not target.is_file():
        raise FileNotFoundError(f"Artifact image does not exist: {relative}")
    return target


def _images(manifest: dict):
    for pane in manifest.get("panes", []):
        for buffer in pane.get("buffers", []):
            for image in buffer.get("images", []):
                yield pane, buffer, image


def _slug(value: str) -> str:
    # ASCII-only names are portable to TeX engines and do not require URL escaping.
    return re.sub(r"[^A-Za-z0-9]+", "-", str(value)).strip("-").lower()[:36] or "window"


def _identity(pane: dict, buffer: dict) -> dict:
    return {"window_id": pane.get("window_id"), "window_index": pane.get("window_index"),
            "window_name": pane.get("window_name"), "pane_id": pane.get("pane_id"),
            "pane_index": pane.get("pane_index"), "buffer": buffer.get("buffer")}


def _rename_images(output: Path, records: list):
    images_dir = output / "images"
    images_dir.mkdir(exist_ok=True)
    candidates = []
    for pane, buffer, image in records:
        first, last = image.get("first_row", 0), image.get("last_row", 0)
        first, last = int(first or 0), int(last or 0)
        base = f"{_slug(pane.get('window_name', 'window'))}-p{int(pane.get('pane_index', 0)):02d}-rows-{first:04d}-{last:04d}"
        candidates.append(base)
    counts, used = Counter(candidates), set()
    for number, ((pane, buffer, image), base) in enumerate(zip(records, candidates), 1):
        if counts[base] > 1:
            base += f"-w{pane.get('window_index', 0)}-{_slug(pane.get('pane_id', 'pane'))}-{_slug(buffer.get('buffer', 'current'))}"
        candidate = base + ".png"
        sequence = 1
        while candidate in used:
            sequence += 1
            candidate = f"{base}-{sequence:02d}.png"
        source = _relative_file(output, image["file"])
        target = images_dir / candidate
        # Do not overwrite a pre-existing unrelated artifact.
        while target.exists() and target.resolve() != source and target.read_bytes() != source.read_bytes():
            sequence += 1
            candidate = f"{base}-{sequence:02d}.png"
            target = images_dir / candidate
        used.add(candidate)
        if target.resolve() != source:
            shutil.copy2(source, target)
        image.setdefault("original_file", image["file"])
        image["file"] = target.relative_to(output).as_posix()
        image["source_identity"] = _identity(pane, buffer)


def _label(pane: dict, buffer: dict, image: dict) -> str:
    return (f"Window {pane.get('window_index', '?')}: {pane.get('window_name', '')} | "
            f"pane {pane.get('pane_index', '?')} ({pane.get('pane_id', '?')}) | "
            f"{buffer.get('buffer', 'current')} | rows {image.get('first_row', '?')}-{image.get('last_row', '?')}")


def _preview(output: Path, manifest: dict, records: list, artifacts: dict):
    cards = []
    for pane, buffer, image in records:
        label = _label(pane, buffer, image)
        path = quote(image["file"], safe="/")
        dimensions = f"{image.get('width', '?')} × {image.get('height', '?')} px"
        details = []
        if image.get("effective_font_pt") is not None:
            details.append(f"Effective print font: {float(image['effective_font_pt']):.2f} pt")
        for field in ("print_readability", "print"):
            info = image.get(field)
            if isinstance(info, dict):
                if info.get("effective_font_pt") is not None:
                    details.append(f"Print font: {float(info['effective_font_pt']):.2f} pt at {info.get('report_width_mm', '?')} mm")
                if info.get("below_minimum"):
                    details.append(f"Below requested minimum {info.get('min_font_pt', '?')} pt")
        warnings = image.get("warnings", [])
        if isinstance(warnings, str):
            warnings = [warnings]
        warnings = list(warnings) + list(buffer.get("warnings", []))
        warnings_html = "".join(f"<li>{html.escape(str(item))}</li>" for item in warnings)
        cards.append(
            '<figure><figcaption>' + html.escape(label) + '</figcaption>'
            '<a href="' + html.escape(path, quote=True) + '"><img loading="lazy" src="' + html.escape(path, quote=True)
            + '" alt="' + html.escape(label, quote=True) + '"></a><p>' + html.escape(dimensions)
            + '</p><p>' + html.escape("; ".join(details)) + '</p>'
            + ('<ul class="warnings">' + warnings_html + '</ul>' if warnings_html else '') + '</figure>'
        )
    title = "tmux-breeze preview: " + str(manifest.get("session_name", "saved capture"))
    text = ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' data:; style-src \'unsafe-inline\'">'
            '<title>' + html.escape(title) + '</title><style>'
            'body{font:15px system-ui,sans-serif;margin:24px;background:#f1f3f5;color:#202428}'
            'main{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:20px}'
            'figure{margin:0;padding:14px;background:white;border:1px solid #ccd2d8;border-radius:6px;overflow-wrap:anywhere}'
            'figcaption{font-weight:600;margin-bottom:12px}img{display:block;width:100%;height:auto}'
            'p{margin:8px 0}.warnings{color:#8b3200}a{color:inherit}'
            '</style><h1>' + html.escape(title) + '</h1><p>' + str(len(records))
            + ' images. Labels are outside terminal content; click an image for its full size.</p><main>'
            + "\n".join(cards) + '</main></html>')
    (output / "preview.html").write_text(text, encoding="utf-8")
    artifacts["preview_html"] = "preview.html"

    # Bound each contact sheet's dimensions and memory, even for long sessions.
    contacts = []
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 13)
    except OSError:
        font = ImageFont.load_default()
    for offset in range(0, len(records), 60):
        group = records[offset:offset + 60]
        columns = min(3, len(group))
        tile_width, tile_height = 440, 330
        sheet = Image.new("RGB", (columns * tile_width, math.ceil(len(group) / columns) * tile_height), "#e2e6e9")
        draw = ImageDraw.Draw(sheet)
        for i, (pane, buffer, image) in enumerate(group):
            x, y = (i % columns) * tile_width, (i // columns) * tile_height
            # ASCII labels remain legible even when the host has no Unicode font.
            label = _label(pane, buffer, image).encode("ascii", "replace").decode()
            for line, text_line in enumerate((label[:64], label[64:128], f"{image.get('width', '?')} x {image.get('height', '?')} px")):
                draw.text((x + 8, y + 5 + line * 17), text_line, fill="#202428", font=font)
            with Image.open(_relative_file(output, image["file"])) as source:
                thumb = source.convert("RGB")
                thumb.thumbnail((tile_width - 16, tile_height - 66), Image.Resampling.LANCZOS)
                sheet.paste(thumb, (x + 8, y + 61))
        filename = "contact-sheet.png" if offset == 0 else f"contact-sheet-{offset // 60 + 1:03d}.png"
        sheet.save(output / filename, "PNG")
        contacts.append(filename)
    artifacts["contact_sheets"] = contacts


def _snippets(output: Path, records: list, kind: str, width_mm: float | None, artifacts: dict):
    if kind not in {"latex", "markdown", "both"}:
        raise ValueError("snippets must be latex, markdown or both")
    if kind in {"latex", "both"}:
        width = f"{width_mm:g}mm" if width_mm else r"\linewidth"
        lines = ["% Requires \\usepackage{graphicx}. Paths are relative to this output directory.",
                 "% Replace each TODO caption. This is an insertion fragment, not a complete report."]
        aliases = []
        for i, (_, _, image) in enumerate(records, 1):
            relative = image["file"]
            # TeX special tokens and non-ASCII filenames differ across engines.
            # Safe byte-identical aliases avoid catcode or encoding surprises.
            if not re.fullmatch(r"[A-Za-z0-9_./-]+", relative):
                alias = f"latex-images/figure-{i:04d}.png"
                target = output / alias
                target.parent.mkdir(exist_ok=True)
                source = _relative_file(output, relative)
                if target.exists() and target.read_bytes() != source.read_bytes():
                    raise ValueError(f"Refusing to overwrite existing LaTeX alias {alias}")
                if target.resolve() != source:
                    shutil.copy2(source, target)
                aliases.append({"file": alias, "source_file": relative})
                relative = alias
            image["latex_file"] = relative
            lines.extend([r"\begin{figure}[htbp]", r"  \centering",
                          r"  \includegraphics[width=" + width + r"]{\detokenize{" + relative + "}}",
                          r"  \caption{TODO: add a caption.}", r"\end{figure}", ""])
        (output / "images.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")
        artifacts["latex_snippet"] = "images.tex"
        artifacts["latex_image_aliases"] = aliases
    if kind in {"markdown", "both"}:
        lines = ["<!-- Paths are relative to this output directory. Replace the TODO captions. -->", ""]
        for _, _, image in records:
            path = quote(image["file"], safe="/")
            lines.extend([f"![TODO: add a caption.](<{path}>)", ""])
        (output / "images.md").write_text("\n".join(lines), encoding="utf-8")
        artifacts["markdown_snippet"] = "images.md"


def finalize_artifacts(output: Path, manifest: dict, *, preview=False, simple_names=False,
                       snippets=None, report_width_mm=None) -> None:
    """Add optional presentation artifacts, preserving original image/raw bytes.

    Image ``file`` paths must be relative to ``output``. Short names are copied
    rather than replacing the original PNGs; their identity remains in manifest.
    """
    output = Path(output).resolve()
    records = list(_images(manifest))
    for _, _, image in records:
        _relative_file(output, image["file"])
    artifacts = manifest.setdefault("artifacts", {})
    if simple_names:
        _rename_images(output, records)
        artifacts["simple_image_directory"] = "images"
    if snippets:
        _snippets(output, records, snippets, report_width_mm, artifacts)
    if preview:
        _preview(output, manifest, records, artifacts)

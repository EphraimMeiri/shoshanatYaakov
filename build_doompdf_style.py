#!/usr/bin/env python3
"""Build a Shoshanat Yaakov PDF using DoomPDF-style techniques.

Strategy:
  1. Render the full poem with XeLaTeX (with \phantom on target lines)
  2. Render each variant of the target lines as separate tiny PDFs
  3. Extract line-variant PDFs as Form XObjects for appearance streams
  4. Create widget fields at the exact line positions, each with a
     pre-rendered appearance (so Hebrew fonts render perfectly)
  5. Use page.AA.O JavaScript (the DoomPDF trigger) to randomly show/hide
     fields via field.display — confirmed working in Chrome's PDFium

This produces perfect Hebrew rendering with runtime randomization.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

from pdfrw import PdfWriter, PdfReader
from pdfrw.objects.pdfname import PdfName
from pdfrw.objects.pdfstring import PdfString
from pdfrw.objects.pdfdict import PdfDict
from pdfrw.objects.pdfarray import PdfArray


# ---------------------------------------------------------------------------
# Hebrew text constants
# ---------------------------------------------------------------------------

def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)

TARGET_A = _nfc("אָרוּר הָמָן אֲשֶׁר בִּקֵּשׁ לְאַבְּדִי:")
TARGET_B = _nfc("בָּרוּךְ מָרְדְּכַי הַיְּהוּדִי:")

WORD_ARUR   = _nfc("אָרוּר")
WORD_BARUCH = _nfc("בָּרוּךְ")
NAME_HAMAN     = _nfc("הָמָן")
NAME_MORDECHAI = _nfc("מָרְדְּכַי")
SUFFIX_A = _nfc("אֲשֶׁר בִּקֵּשׁ לְאַבְּדִי:")
SUFFIX_B = _nfc("הַיְּהוּדִי:")

# The 3 variants: (line_a_text, line_b_text)
VARIANTS = [
    # 0: original
    (f"{WORD_ARUR} {NAME_HAMAN} {SUFFIX_A}",
     f"{WORD_BARUCH} {NAME_MORDECHAI} {SUFFIX_B}"),
    # 1: swap adjectives
    (f"{WORD_BARUCH} {NAME_HAMAN} {SUFFIX_A}",
     f"{WORD_ARUR} {NAME_MORDECHAI} {SUFFIX_B}"),
    # 2: swap names
    (f"{WORD_ARUR} {NAME_MORDECHAI} {SUFFIX_A}",
     f"{WORD_BARUCH} {NAME_HAMAN} {SUFFIX_B}"),
]


# ---------------------------------------------------------------------------
# LaTeX helpers
# ---------------------------------------------------------------------------

def latex_escape(text: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{", "}": r"\}",
        "$": r"\$", "&": r"\&", "#": r"\#",
        "%": r"\%", "_": r"\_",
        "^": r"\^{}", "~": r"\~{}",
    }
    return "".join(replacements.get(ch, ch) for ch in text)


def load_lines(input_txt: Path) -> list[str]:
    lines = [
        unicodedata.normalize("NFC", l.strip())
        for l in input_txt.read_text(encoding="utf-8").splitlines()
    ]
    if TARGET_A not in lines or TARGET_B not in lines:
        raise ValueError("Could not find both target lines in the input text.")
    return lines


def compile_tex(tex_file: Path, workdir: Path) -> Path:
    if not shutil.which("xelatex"):
        raise RuntimeError("xelatex not found in PATH")
    cmd = ["xelatex", "-interaction=nonstopmode", "-halt-on-error", tex_file.name]
    run = subprocess.run(cmd, cwd=workdir, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    if run.returncode != 0:
        raise RuntimeError(f"xelatex failed:\n{run.stdout}")
    pdf_path = workdir / f"{tex_file.stem}.pdf"
    if not pdf_path.exists():
        raise RuntimeError("xelatex completed but no PDF produced")
    return pdf_path


# ---------------------------------------------------------------------------
# Step 1: Build the base PDF (full poem, phantom on target lines)
# ---------------------------------------------------------------------------

def build_base_pdf(lines: list[str], workdir: Path) -> Path:
    body: list[str] = []
    for line in lines:
        if not line:
            body.append(r"\vspace{0.4em}")
            continue
        if line == TARGET_A or line == TARGET_B:
            body.append(r"\phantom{" + latex_escape(line) + r"}\\[0.3em]")
            continue
        body.append(f"{latex_escape(line)}\\\\[0.3em]")

    tex = "\n".join([
        r"\documentclass[12pt]{article}",
        r"\usepackage[paperwidth=6in,paperheight=9in,margin=0.5in]{geometry}",
        r"\usepackage{fontspec}",
        r"\usepackage{polyglossia}",
        r"\setmainlanguage{hebrew}",
        r"\newfontfamily\hebrewfont[Script=Hebrew]{Times New Roman}",
        r"\pagestyle{empty}",
        r"\begin{document}",
        r"\begin{flushright}",
        *body,
        r"\end{flushright}",
        r"\end{document}",
        "",
    ])
    tex_file = workdir / "base.tex"
    tex_file.write_text(tex, encoding="utf-8")
    return compile_tex(tex_file, workdir)


# ---------------------------------------------------------------------------
# Step 2: Render each line variant as a tiny single-line PDF
# ---------------------------------------------------------------------------

def build_line_pdf(text: str, width_pt: float, workdir: Path, name: str) -> Path:
    """Render a single Hebrew line into a tight-cropped PDF."""
    tex = "\n".join([
        r"\documentclass[12pt]{article}",
        # Use a very wide page so text doesn't wrap, tight vertical margins
        r"\usepackage[paperwidth=" + f"{width_pt}pt" + r",paperheight=30pt,margin=0pt]{geometry}",
        r"\usepackage{fontspec}",
        r"\usepackage{polyglossia}",
        r"\setmainlanguage{hebrew}",
        r"\newfontfamily\hebrewfont[Script=Hebrew]{Times New Roman}",
        r"\pagestyle{empty}",
        r"\begin{document}",
        r"\begin{flushright}",
        r"\vspace*{0pt}",
        latex_escape(text),
        r"\end{flushright}",
        r"\end{document}",
        "",
    ])
    tex_file = workdir / f"{name}.tex"
    tex_file.write_text(tex, encoding="utf-8")
    return compile_tex(tex_file, workdir)


# ---------------------------------------------------------------------------
# Step 3: Extract page content as a Form XObject
# ---------------------------------------------------------------------------

def page_to_form_xobject(page) -> PdfDict:
    """Convert a page's content stream + resources into a Form XObject dict.

    Handles FlateDecode-compressed streams by decompressing first, since
    pdfrw doesn't automatically manage Filter on re-serialized streams.
    """
    import zlib

    contents = page.Contents
    if contents is None:
        stream_data = ""
    elif isinstance(contents, PdfArray):
        parts = []
        for c in contents:
            if hasattr(c, 'stream') and c.stream:
                raw = c.stream
                if c.Filter == '/FlateDecode':
                    # Decompress
                    raw = zlib.decompress(raw.encode('latin-1')).decode('latin-1')
                parts.append(raw)
        stream_data = "\n".join(parts)
    else:
        raw = contents.stream or ""
        if contents.Filter == '/FlateDecode' and raw:
            raw = zlib.decompress(raw.encode('latin-1')).decode('latin-1')
        stream_data = raw

    mb = page.MediaBox or page.inheritable.MediaBox
    if mb is None:
        mb = [0, 0, 432, 30]

    form = PdfDict()
    form.Type = PdfName.XObject
    form.Subtype = PdfName.Form
    form.FormType = 1
    form.BBox = PdfArray([mb[0], mb[1], mb[2], mb[3]])
    form.Resources = page.Resources or page.inheritable.Resources
    form.stream = stream_data
    form.Matrix = PdfArray([1, 0, 0, 1, 0, 0])

    return form


# ---------------------------------------------------------------------------
# Step 4: Create widget fields with pre-rendered appearances
# ---------------------------------------------------------------------------

def create_script(js: str) -> PdfDict:
    action = PdfDict()
    action.S = PdfName.JavaScript
    # Use a stream object for JS to avoid pdfrw escaping parentheses
    # in PDF literal strings (which breaks JS execution in Chrome PDFium)
    js_stream = PdfDict()
    js_stream.stream = js
    action.JS = js_stream
    return action


def create_widget_with_appearance(name: str, rect: list, appearance_xobj: PdfDict) -> PdfDict:
    """Create a Widget annotation with a pre-rendered appearance stream."""
    field = PdfDict()
    field.Type = PdfName.Annot
    field.Subtype = PdfName.Widget
    field.FT = PdfName.Btn          # Button type (just a visual container)
    field.Ff = 65536                 # Pushbutton — no user interaction
    field.Rect = PdfArray(rect)
    field.T = PdfString.encode(name)
    field.F = 4                      # Print flag

    # No border
    field.BS = PdfDict()
    field.BS.W = 0
    field.MK = PdfDict()            # Empty appearance characteristics

    # Set the appearance stream
    ap = PdfDict()
    ap.N = appearance_xobj           # Normal appearance
    field.AP = ap

    return field


# ---------------------------------------------------------------------------
# Step 5: Assemble the final PDF
# ---------------------------------------------------------------------------

def build_final_pdf(
    base_pdf_path: Path,
    line_variant_pdfs: dict[str, Path],
    output_path: Path,
) -> None:
    reader = PdfReader(str(base_pdf_path))
    page = reader.pages[0]

    mb = page.MediaBox or page.inheritable.MediaBox
    if mb is None:
        mb = [0, 0, 432, 648]

    page_width = float(mb[2])

    # Measured positions from the static original PDF (PyMuPDF extraction):
    #   Line 7 (TARGET_A): y=147.0–159.0  x=284.1–396.0  (top-left coords)
    #   Line 8 (TARGET_B): y=165.1–177.0  x=316.2–396.0
    # Text baseline in line PDFs: content stream does
    #   q 1 0 0 1 72 -42.112 cm ... Td at y=45.599
    #   → effective baseline y = -42.112 + 45.599 = 3.487 from page bottom
    page_height = float(mb[3])

    # In the main poem, the text baseline for line A is at:
    #   top-left y of glyph bottom ≈ 159.0, but baseline sits above descenders
    #   Baseline ≈ top-left y=155.5 → PDF bottom-up y = 648 - 155.5 = 492.5
    # Line B baseline:
    #   Baseline ≈ top-left y=173.5 → PDF bottom-up y = 648 - 173.5 = 474.5
    #
    # The line PDF has its text baseline at y=3.487 from its own bottom.
    # So to align baselines:
    #   field_bottom = poem_baseline_y - 3.487

    line_a_baseline_y = page_height - 155.5
    line_b_baseline_y = page_height - 173.5
    xobj_baseline = 3.487  # baseline offset from bottom of the 30pt line PDF

    # Read each line variant PDF and extract as XObject
    fields = []
    field_names_a = []
    field_names_b = []

    for vi in range(3):
        for line_id in ["a", "b"]:
            key = f"line_{line_id}_v{vi}"
            variant_pdf = PdfReader(str(line_variant_pdfs[key]))
            vpage = variant_pdf.pages[0]
            xobj = page_to_form_xobject(vpage)

            # Get the line PDF's native dimensions
            vmb = vpage.MediaBox or vpage.inheritable.MediaBox
            v_width = float(vmb[2]) - float(vmb[0])
            v_height = float(vmb[3]) - float(vmb[1])

            if line_id == "a":
                baseline_y = line_a_baseline_y
            else:
                baseline_y = line_b_baseline_y

            # Position field so baselines align; no vertical scaling
            rect_bottom = baseline_y - xobj_baseline
            rect_top = rect_bottom + v_height

            # Horizontally: the line PDF is 360pt wide, same as our field width.
            # The XeLaTeX right-alignment handles text positioning within.
            x_left = 36.0
            x_right = x_left + v_width

            # Identity matrix — render XObject at native size
            xobj.Matrix = PdfArray([1, 0, 0, 1, 0, 0])

            field_name = f"{line_id}{vi}"
            rect = [x_left, rect_bottom, x_right, rect_top]

            widget = create_widget_with_appearance(field_name, rect, xobj)
            fields.append(widget)

            if line_id == "a":
                field_names_a.append(field_name)
            else:
                field_names_b.append(field_name)

    # Attach fields to page
    if page.Annots is None:
        page.Annots = PdfArray()
    for f in fields:
        page.Annots.append(f)

    # AcroForm
    acroform = PdfDict()
    acroform.Fields = PdfArray(fields)
    acroform.NeedAppearances = PdfName("false")
    reader.Root.AcroForm = acroform

    # --- JavaScript ---
    # Use field.display to show one variant per line and hide the others
    # display.visible = 0, display.hidden = 1
    js_lines = []
    js_lines.append("var roll = Math.floor(Math.random() * 3);")

    # For each line, show variant[roll] and hide the other two
    for names, label in [(field_names_a, "a"), (field_names_b, "b")]:
        for i, name in enumerate(names):
            js_lines.append(
                f'this.getField("{name}").display = (roll === {i}) ? display.visible : display.hidden;'
            )

    js = "\n".join(js_lines)

    # page.AA.O — the DoomPDF trigger (works in Chrome PDFium)
    if page.AA is None:
        page.AA = PdfDict()
    page.AA.O = create_script("try {\n" + js + "\n} catch(e) { app.alert(e.stack || e); }")

    # Also set document OpenAction for Adobe Acrobat
    reader.Root.OpenAction = create_script("try {\n" + js + "\n} catch(e) { app.alert(e.stack || e); }")

    writer = PdfWriter()
    writer.trailer = reader
    writer.write(str(output_path))


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def build(input_txt: Path, output_pdf: Path) -> None:
    lines = load_lines(input_txt)

    with tempfile.TemporaryDirectory(prefix="doom_style_") as tmp:
        tmpdir = Path(tmp)

        # 1. Build base PDF (poem with phantom lines)
        base_pdf = build_base_pdf(lines, tmpdir)

        # 2. Render each line variant
        # Field width: 396 - 36 = 360pt
        field_width = 360.0
        line_pdfs: dict[str, Path] = {}
        for vi, (text_a, text_b) in enumerate(VARIANTS):
            line_pdfs[f"line_a_v{vi}"] = build_line_pdf(
                text_a, field_width, tmpdir, f"line_a_v{vi}"
            )
            line_pdfs[f"line_b_v{vi}"] = build_line_pdf(
                text_b, field_width, tmpdir, f"line_b_v{vi}"
            )

        # 3. Assemble final PDF
        build_final_pdf(base_pdf, line_pdfs, output_pdf)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build Shoshanat Yaakov PDF with DoomPDF-style JS randomization."
    )
    parser.add_argument("--input", default="שושנת.txt", type=Path)
    parser.add_argument("--output", default="outputs/trials/trial-doom-style.pdf", type=Path)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    build(args.input, args.output)
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()

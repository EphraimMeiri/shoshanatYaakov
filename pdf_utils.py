"""Shared utilities for Shoshanat Yaakov PDF generation."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

from PyPDF2 import PdfReader, PdfWriter
from PyPDF2.generic import (
    ArrayObject,
    DecodedStreamObject,
    FloatObject,
    NameObject,
    NumberObject,
)

# ---------------------------------------------------------------------------
# Hebrew text constants
# ---------------------------------------------------------------------------

def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)

TARGET_A = _nfc("אָרוּר הָמָן אֲשֶׁר בִּקֵּשׁ לְאַבְּדִי:")
TARGET_B = _nfc("בָּרוּךְ מָרְדְּכַי הַיְּהוּדִי:")

WORD_ARUR = _nfc("אָרוּר")
WORD_BARUCH = _nfc("בָּרוּךְ")
NAME_HAMAN = _nfc("הָמָן")
NAME_MORDECHAI = _nfc("מָרְדְּכַי")

SUFFIX_A = _nfc("אֲשֶׁר בִּקֵּשׁ לְאַבְּדִי:")
SUFFIX_B = _nfc("הַיְּהוּדִי:")

VARIANT_LABELS = ["original", "adj-swap", "name-swap"]

# ---------------------------------------------------------------------------
# LaTeX helpers
# ---------------------------------------------------------------------------


def latex_escape(text: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "$": r"\$",
        "&": r"\&",
        "#": r"\#",
        "%": r"\%",
        "_": r"\_",
        "^": r"\^{}",
        "~": r"\~{}",
    }
    return "".join(replacements.get(ch, ch) for ch in text)


def load_lines(input_txt: Path) -> list[str]:
    lines = [
        unicodedata.normalize("NFC", line.strip())
        for line in input_txt.read_text(encoding="utf-8").splitlines()
    ]
    nfc_a = unicodedata.normalize("NFC", TARGET_A)
    nfc_b = unicodedata.normalize("NFC", TARGET_B)
    if nfc_a not in lines or nfc_b not in lines:
        raise ValueError(
            "Could not find both target lines in the input text. "
            "Make sure the two Purim lines are present exactly."
        )
    return lines


def variant_line_pair(mode: int) -> tuple[str, str]:
    if mode == 0:
        return TARGET_A, TARGET_B
    if mode == 1:
        return (
            f"{WORD_BARUCH} {NAME_HAMAN} {SUFFIX_A}",
            f"{WORD_ARUR} {NAME_MORDECHAI} {SUFFIX_B}",
        )
    if mode == 2:
        return (
            f"{WORD_ARUR} {NAME_MORDECHAI} {SUFFIX_A}",
            f"{WORD_BARUCH} {NAME_HAMAN} {SUFFIX_B}",
        )
    raise ValueError(f"Unsupported mode: {mode}")


def apply_variant(lines: list[str], mode: int) -> list[str]:
    line_a, line_b = variant_line_pair(mode)
    out: list[str] = []
    for line in lines:
        if line == TARGET_A:
            out.append(line_a)
        elif line == TARGET_B:
            out.append(line_b)
        else:
            out.append(line)
    return out


# ---------------------------------------------------------------------------
# TeX compilation
# ---------------------------------------------------------------------------


def write_static_tex(lines: list[str], tex_out: Path) -> None:
    body_lines: list[str] = []
    for line in lines:
        if not line:
            body_lines.append(r"\vspace{0.4em}")
            continue
        body_lines.append(f"{latex_escape(line)}\\\\[0.3em]")

    tex = "\n".join(
        [
            r"\documentclass[12pt]{article}",
            r"\usepackage[paperwidth=6in,paperheight=9in,margin=0.5in]{geometry}",
            r"\usepackage{fontspec}",
            r"\usepackage{polyglossia}",
            r"\setmainlanguage{hebrew}",
            r"\newfontfamily\hebrewfont[Script=Hebrew]{Times New Roman}",
            r"\pagestyle{empty}",
            r"\begin{document}",
            r"\begin{flushright}",
            *body_lines,
            r"\end{flushright}",
            r"\end{document}",
            "",
        ]
    )
    tex_out.write_text(tex, encoding="utf-8")


def compile_tex(tex_file: Path, workdir: Path) -> Path:
    if not shutil.which("xelatex"):
        raise RuntimeError("xelatex is required but not found in PATH")

    cmd = ["xelatex", "-interaction=nonstopmode", "-halt-on-error", tex_file.name]
    run = subprocess.run(
        cmd, cwd=workdir, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    if run.returncode != 0:
        raise RuntimeError(f"xelatex failed:\n{run.stdout}")

    pdf_path = workdir / f"{tex_file.stem}.pdf"
    if not pdf_path.exists():
        raise RuntimeError("xelatex completed but PDF was not produced")
    return pdf_path


def build_static_variants(input_txt: Path, workdir: Path) -> list[Path]:
    """Compile 3 static variant PDFs, return list of paths [original, adj-swap, name-swap]."""
    lines = load_lines(input_txt)
    paths: list[Path] = []
    for mode, label in enumerate(VARIANT_LABELS):
        tex_file = workdir / f"static-{label}.tex"
        variant_lines = apply_variant(lines, mode)
        write_static_tex(variant_lines, tex_file)
        pdf_path = compile_tex(tex_file, workdir)
        paths.append(pdf_path)
    return paths


# ---------------------------------------------------------------------------
# PDF manipulation helpers
# ---------------------------------------------------------------------------


def make_form_xobject_from_page(page, writer: PdfWriter):
    """Extract a page's content stream + resources into a Form XObject."""
    contents = page.get("/Contents")
    content_data = b""
    if contents is not None:
        resolved = contents.get_object()
        if isinstance(resolved, ArrayObject):
            chunks: list[bytes] = []
            for part_ref in resolved:
                part = part_ref.get_object()
                chunks.append(part.get_data())
            content_data = b"\n".join(chunks)
        else:
            content_data = resolved.get_data()

    resources = page["/Resources"].clone(writer)
    mb = page.mediabox
    bbox = ArrayObject(
        [
            FloatObject(float(mb.left)),
            FloatObject(float(mb.bottom)),
            FloatObject(float(mb.right)),
            FloatObject(float(mb.top)),
        ]
    )

    form = DecodedStreamObject()
    form.set_data(content_data)
    form.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/FormType"): NumberObject(1),
            NameObject("/BBox"): bbox,
            NameObject("/Resources"): resources,
        }
    )
    return writer._add_object(form)

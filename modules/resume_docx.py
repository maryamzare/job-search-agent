"""
resume_docx.py — render a validated structured resume into an ATS-safe .docx.

Input is a plain dict (see `RESUME_SCHEMA_DOC` below) plus a contact dict.
Output is a single-column .docx with standard section headings and no tables,
text boxes, images, headers, or footers. Styling measurements come from
modules/resume_style.py (frozen from the approved reference document).

This module never calls an API and never reads the authoritative profile;
it only lays out text it is handed. Factual correctness is the caller's job
(modules/module3_resume.py validates before calling render()).
"""
from __future__ import annotations

import os

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.section import WD_SECTION
from docx.oxml.ns import qn
from docx.shared import Inches, Pt

from modules import resume_style as S

RESUME_SCHEMA_DOC = """
resume dict:
  summary: str | None
  experience: [ {title, company, location, start, end, current: bool, bullets: [str, ...]}, ... ]
  skills: [str, ...]
  education: [ {credential, institution, year}, ... ]
  certifications: [ {name, year: str | None}, ... ]
contact dict:
  {name, email, phone, location, linkedin, github, website}  (strings; "" allowed except name/location)
"""


def _set_base_font(document: Document) -> None:
    style = document.styles["Normal"]
    style.font.name = S.BODY_FONT
    style.font.size = Pt(S.BODY_SIZE_PT)
    # east-asian / complex-script slots so Word doesn't fall back to a serif
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(qn(attr), S.BODY_FONT)
    pf = style.paragraph_format
    pf.line_spacing = S.LINE_SPACING
    pf.space_before = Pt(0)
    pf.space_after = Pt(S.SPACE_AFTER_BODY_PT)


def _page_setup(document: Document) -> None:
    for section in document.sections:
        section.page_width = Inches(S.PAGE_WIDTH_IN)
        section.page_height = Inches(S.PAGE_HEIGHT_IN)
        section.top_margin = Inches(S.MARGIN_IN)
        section.bottom_margin = Inches(S.MARGIN_IN)
        section.left_margin = Inches(S.MARGIN_IN)
        section.right_margin = Inches(S.MARGIN_IN)
        section.different_first_page_header_footer = False
        # Leave header/footer parts empty — never populated by this module.


def _para(document, text, *, size=None, bold=False, italic=False, allcaps=False,
          space_before=0, space_after=None, align=None, keep_with_next=False):
    p = document.add_paragraph()
    run = p.add_run(text.upper() if allcaps else text)
    run.font.name = S.BODY_FONT
    run.font.size = Pt(size if size is not None else S.BODY_SIZE_PT)
    run.font.bold = bold
    run.font.italic = italic
    pf = p.paragraph_format
    pf.space_before = Pt(space_before)
    pf.space_after = Pt(S.SPACE_AFTER_BODY_PT if space_after is None else space_after)
    if align is not None:
        p.alignment = align
    # Keeps this paragraph on the same page as the one immediately after it.
    # Used to chain a role's heading -> date line -> first bullet so a page
    # break can never strand the heading alone with its bullets pushed to
    # the next page (or, equivalently, leave an unlabeled continuation
    # bullet orphaned at the top of a new page).
    pf.keep_with_next = keep_with_next
    return p


def _section_heading(document, label):
    return _para(
        document, label,
        size=S.SECTION_HEADING_SIZE_PT, bold=S.SECTION_HEADING_BOLD,
        allcaps=S.SECTION_HEADING_ALLCAPS,
        space_before=S.SPACE_BEFORE_SECTION_PT, space_after=S.SPACE_AFTER_SECTION_PT,
    )


def _bullet(document, text):
    p = document.add_paragraph()
    run = p.add_run(f"{S.BULLET_CHAR}  {text}")
    run.font.name = S.BODY_FONT
    run.font.size = Pt(S.BODY_SIZE_PT)
    pf = p.paragraph_format
    pf.left_indent = Inches(S.BULLET_LEFT_INDENT_IN)
    pf.first_line_indent = Inches(-S.BULLET_HANGING_IN)
    pf.space_before = Pt(0)
    pf.space_after = Pt(S.SPACE_AFTER_BULLET_PT)
    return p


def _date_range(start, end, current):
    end_text = S.CURRENT_END_LABEL if current else (end or "")
    if start and end_text:
        return f"{start}{S.DATE_RANGE_DASH}{end_text}"
    return start or end_text or ""


def _contact_line(contact: dict, *, fields=None) -> str:
    order = list(fields) if fields is not None else list(S.CONTACT_LINE_FIELDS)
    parts = [str(contact.get(k, "")).strip() for k in order]
    return "  |  ".join(p for p in parts if p)


def render(resume: dict, contact: dict, out_path: str, *, include_summary: bool,
           contact_fields=None) -> str:
    """Write the .docx to out_path and return out_path. Overwrites out_path if
    it exists — the caller is responsible for temp-file + promote semantics.

    contact_fields: which contact dict keys to render, in order. Defaults to
    resume_style.CONTACT_LINE_FIELDS (job-relevant fields only -- excludes
    "github" and "website" even when present in contact). Pass an explicit
    tuple including "github" and/or "website" only when the target job
    actually calls for them.
    """
    document = Document()
    _set_base_font(document)
    _page_setup(document)

    # --- Name + contact line (no "Contact" heading above them) ---
    _para(document, contact["name"], size=S.NAME_SIZE_PT, bold=S.NAME_BOLD,
          space_after=S.SPACE_AFTER_NAME_PT)
    _para(document, _contact_line(contact, fields=contact_fields), size=S.CONTACT_SIZE_PT,
          space_after=S.SPACE_AFTER_CONTACT_PT)

    # --- Summary (only when explicitly requested) ---
    if include_summary and resume.get("summary"):
        _section_heading(document, "Summary")
        _para(document, resume["summary"].strip())

    # --- Experience (reverse chronological; caller supplies order) ---
    _section_heading(document, "Experience")
    for role in resume.get("experience", []):
        header = f"{role['title']}, {role['company']}, {role['location']}"
        _para(document, header, size=S.ROLE_HEADING_SIZE_PT, bold=S.ROLE_HEADING_BOLD,
              space_before=S.SPACE_BEFORE_ROLE_PT, space_after=0, keep_with_next=True)
        _para(document, _date_range(role.get("start"), role.get("end"), role.get("current", False)),
              size=S.ROLE_SUBLINE_SIZE_PT, italic=S.ROLE_SUBLINE_ITALIC,
              space_after=S.SPACE_AFTER_ROLE_PT, keep_with_next=True)
        for b in role.get("bullets", []):
            _bullet(document, b.strip())

    # --- Skills ---
    if resume.get("skills"):
        _section_heading(document, "Skills")
        _para(document, ", ".join(s.strip() for s in resume["skills"]))

    # --- Education ---
    _section_heading(document, "Education")
    for e in resume.get("education", []):
        line = f"{e['credential']}, {e['institution']}"
        if e.get("year"):
            line += f", {e['year']}"
        _para(document, line, space_after=2)

    # --- Certifications ---
    if resume.get("certifications"):
        _section_heading(document, "Certifications")
        for c in resume["certifications"]:
            line = c["name"] + (f", {c['year']}" if c.get("year") else "")
            _para(document, line, space_after=2)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    document.save(out_path)
    return out_path


# --------------------------------------------------------------------------- #
# Structural inspection — used by module3_resume.validate_deterministic on the
# rendered file, and by tests. Pure reads; no side effects.
# --------------------------------------------------------------------------- #
def inspect_structure(path: str) -> dict:
    """Return ATS-relevant structural facts about a .docx on disk."""
    doc = Document(path)
    body = doc.element.body

    # header/footer parts that actually carry content
    def _has_content(parts):
        for part in parts:
            if part is None:
                continue
            if part.paragraphs and any(p.text.strip() for p in part.paragraphs):
                return True
            if getattr(part, "tables", None):
                return True
        return False

    headers, footers = [], []
    for section in doc.sections:
        headers += [section.header, section.first_page_header, section.even_page_header]
        footers += [section.footer, section.first_page_footer, section.even_page_footer]

    section_headings = []
    for p in doc.paragraphs:
        runs = p.runs
        txt = p.text.strip()
        if not txt:
            continue
        if runs and all(r.font.bold for r in runs if r.text.strip()) and txt.isupper():
            section_headings.append(txt.title())

    ncols = 1
    for sectPr in body.findall(qn("w:sectPr")):
        cols = sectPr.find(qn("w:cols"))
        if cols is not None and cols.get(qn("w:num")):
            ncols = max(ncols, int(cols.get(qn("w:num"))))

    return {
        "tables": len(doc.tables),
        "images": len(body.findall(".//" + qn("w:drawing"))) + len(body.findall(".//" + qn("w:pict"))),
        "text_boxes": len(body.findall(".//" + qn("w:txbxContent"))),
        "has_header_content": _has_content(headers),
        "has_footer_content": _has_content(footers),
        "columns": ncols,
        "section_headings": section_headings,
        "paragraph_count": len(doc.paragraphs),
    }


def opens_cleanly(path: str) -> bool:
    """True if python-docx can reopen and read the file end to end."""
    try:
        doc = Document(path)
        _ = [p.text for p in doc.paragraphs]
        return True
    except Exception:
        return False

"""
resume_style.py — typographic constants for the generated .docx resume.

These values were measured ONCE, read-only, from the approved visual style
reference:

    /Users/marmar/Downloads/MaryamZareResume 4-21-25.docx
    SHA-256: 8cc7ce83f63518e252eed09e9a3077238b25e74cc1cd565fab59672b1915abd8

Only page/typography measurements were taken. No resume *content*, wording,
section names, or bullet text was copied. The reference document is NOT a
runtime dependency — nothing here reads it; the numbers are frozen below.

Where the reference could not supply an ATS-safe value it is noted inline:
the reference puts the name and contact line in a Word *header* part
(word/header1.xml), which is not ATS-safe, so this module defines body-line
sizes for the name/contact block instead of reproducing that header.
"""

# --- Page (from <w:pgSz> / <w:pgMar>) ---------------------------------------
PAGE_WIDTH_IN = 8.5          # 12240 twips — US Letter
PAGE_HEIGHT_IN = 11.0        # 15840 twips
MARGIN_IN = 1.0             # 1440 twips, all four sides
COLUMNS = 1                 # <w:cols w:space="720"/> — single column

# --- Body text (paragraph runs use w:sz=22 → 11pt; theme minor font = Aptos) -
BODY_FONT = "Aptos"
# Real fallbacks for machines without Aptos (python-docx sets the primary;
# the renderer also writes an eastAsia/cs fallback and relies on Word/LibreOffice
# substitution). Calibri/Carlito are metric-compatible stand-ins.
BODY_FONT_FALLBACKS = ("Calibri", "Carlito", "Helvetica", "Arial")
BODY_SIZE_PT = 11
LINE_SPACING = 1.16         # w:line="278" / 240
SPACE_AFTER_BODY_PT = 8     # w:after="160"

# --- Headings / role headers (reference: bold, same 11pt as body, no color) --
SECTION_HEADING_SIZE_PT = 11
SECTION_HEADING_BOLD = True
SECTION_HEADING_ALLCAPS = True   # ATS-standard section labels, rendered in caps
SPACE_BEFORE_SECTION_PT = 12
SPACE_AFTER_SECTION_PT = 4

ROLE_HEADING_SIZE_PT = 11
ROLE_HEADING_BOLD = True
SPACE_BEFORE_ROLE_PT = 8
SPACE_AFTER_ROLE_PT = 2
# Sub-line under a role header (dates / location if placed on their own line)
ROLE_SUBLINE_SIZE_PT = 11
ROLE_SUBLINE_ITALIC = True

# --- Name + contact block (no reference precedent in the body; ATS-safe pick) -
NAME_SIZE_PT = 16
NAME_BOLD = True
CONTACT_SIZE_PT = 10
SPACE_AFTER_NAME_PT = 2
SPACE_AFTER_CONTACT_PT = 10

# --- Bullets (reference: standard Word bullet list, 11pt, not bold) ----------
BULLET_CHAR = "•"
BULLET_LEFT_INDENT_IN = 0.25
BULLET_HANGING_IN = 0.25
SPACE_AFTER_BULLET_PT = 2

# --- Section order (spec-mandated ATS-standard names) -----------------------
# Name + contact line sit at the very top with no "Contact" heading above
# them (2026-09-05: a labeled Contact section read as filler on a one-page
# resume), so neither list below starts with one.
SECTION_ORDER_WITH_SUMMARY = ["Summary", "Experience", "Skills", "Education", "Certifications"]
SECTION_ORDER_NO_SUMMARY = ["Experience", "Skills", "Education", "Certifications"]

# Contact-line fields, in display order. Job-relevant only by default: the
# job-search-agent GitHub repo and personal art-website links are real
# fields in data/contact_block.txt but are not included in a generated
# resume unless explicitly requested for that job -- add "github" and/or
# "website" back to this tuple (or pass an override) only on explicit request.
CONTACT_LINE_FIELDS = ("location", "phone", "email", "linkedin")

# Date display format used consistently throughout the document.
# "Mon YYYY" (e.g. "Jun 2022"); year-only ("2017") is allowed for education.
# The renderer joins a date range with a plain spaced hyphen. (An en dash in a
# range would also be permitted by the validator; a hyphen is chosen here so
# the generated document is deterministic and unambiguously ATS-safe. Em dashes
# are never produced or accepted.)
DATE_RANGE_DASH = " - "
CURRENT_END_LABEL = "Present"

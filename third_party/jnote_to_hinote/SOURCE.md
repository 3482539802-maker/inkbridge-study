# Native PDF metadata reference

Upstream: https://github.com/LTong-g/jnote_to_hinote

Pinned commit: `532c561b2ad2d53022c53f73ee02662cccfcf298`.

License: MIT, Copyright (c) 2026 Ray L; full text in LICENSE.

`scripts/hinote_workbook.py` adapts the PDF-backed archive metadata layout from
`src/jnotes2hinote/converter.py` (`_convert_pdf`, `_rewrite_thumbnails`). Reference
file SHA256: `b157a5925032c50e02abf1c08bfb6e251703d9a54313e9d349be6d7d18880d4a`.
It uses PyMuPDF/Pillow to render previews; it does not ship the Jnotes parser,
GUI or handwriting encoder. Output retains `hinote_1.0.5`, no invented version
upgrade and no synthetic user strokes. Native import compatibility and UUID
preservation require separate device observation.

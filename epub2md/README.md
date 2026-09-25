# epub2md

Converts a PDF-derived epub (the kind calibre makes from `pdftohtml` output) of a programming
book into GitHub-flavored Markdown. Python 3 standard library only.

These epubs have no real structure: every line is a flat `<p>`, code has lost its monospace
font and indentation, and paragraphs break at every page. A plain `pandoc` run gives a wall of
text. This script rebuilds:

- **Headings** from the PDF Document Outline the epub carries (Parts `#`, chapters `##`,
  sections `###`/`####`), plus short bold-only lines as `####`.
- **A linked table of contents** in place of the book's flat one.
- **Fenced code blocks** from line heuristics, tagged `apex`, `javascript`, `html`, `json`,
  `xml` or `text` (debug logs). Merged statements are split and code is re-indented by brace
  depth.
- **Paragraphs** rejoined across page breaks, including words hyphenated at a page edge.
- **Figure captions** split from the prose that ran into them, and images copied to `images/`.

## Usage

```bash
python3 epub2md.py book.epub out_dir --diag
```

Writes `out_dir/<book title>.md` and `out_dir/images/`. `--diag` prints counts to stderr (code
blocks by language, headings matched against the outline, joins, splits) and lists any outline
entry it could not find in the body.

## Limits

- The code heuristics are tuned for Apex/Java-style code (keywords, braces, semicolons). Books
  in other languages will need the patterns in `CODE_START` adjusted.
- Indentation is reconstructed, not recovered. Prefer the book's own sample-code download for
  anything you intend to run.
- A source paragraph that fuses a code line with the next sentence stays as prose.

The output contains the book's full text and images, so keep it out of version control.

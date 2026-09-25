#!/usr/bin/env python3
"""Convert a PDF-derived epub (pdftohtml via calibre) of a programming book to Markdown.

Such an epub body is a flat run of <p> paragraphs with page anchors, so structure is rebuilt:
headings from the PDF Document Outline, fenced code from line heuristics, prose paragraphs
rejoined across page breaks. Writes <out_dir>/<book title>.md and <out_dir>/images/.

usage: epub2md.py <book.epub> <out_dir> [--diag]
"""
import argparse
import collections
import html
import posixpath
import re
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path

# ---------------------------------------------------------------- parsing

class BodyParser(HTMLParser):
    """Flatten a body file into anchor / img / p items; p items carry (text, style) segments."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items, self.segs, self.style, self.had_img = [], None, [], False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "p":
            self.segs, self.had_img = [], False
        elif tag == "a" and "id" in a:
            self.items.append({"t": "anchor", "id": a["id"]})
        elif tag == "img":
            self._flush(keep_empty=False)
            self.had_img = True
            self.items.append({"t": "img", "src": a["src"]})
        elif tag in ("b", "i"):
            self.style.append(tag)

    def handle_endtag(self, tag):
        if tag == "p":
            self._flush(keep_empty=not self.had_img)
            self.segs = None
        elif tag in ("b", "i") and tag in self.style:
            self.style.remove(tag)

    def handle_data(self, data):
        if self.segs is not None:
            data = data.replace("\xa0", " ").replace("\xad", "")
            self.segs.append((data, "".join(sorted(set(self.style)))))

    def _flush(self, keep_empty):
        if self.segs is None:
            return
        if keep_empty or "".join(s for s, _ in self.segs).strip():
            self.items.append({"t": "p", "segs": self.segs})
        self.segs = []


class OutlineParser(HTMLParser):
    """Read the PDF Document Outline: nested <ul> of links -> (level, title, file, page)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth, self.href, self.text, self.entries = 0, None, "", []

    def handle_starttag(self, tag, attrs):
        if tag == "ul":
            self.depth += 1
        elif tag == "a":
            self.href, self.text = dict(attrs).get("href"), ""

    def handle_endtag(self, tag):
        if tag == "ul":
            self.depth -= 1
        elif tag == "a" and self.href:
            f, _, anchor = self.href.partition("#")
            title = re.sub(r"\s+", " ", self.text).strip()
            self.entries.append({"level": self.depth, "title": title, "file": f,
                                 "page": int(anchor[1:])})
            self.href = None

    def handle_data(self, data):
        if self.href:
            self.text += data


def read_epub(path):
    z = zipfile.ZipFile(path)
    container = z.read("META-INF/container.xml").decode()
    opf_path = re.search(r'full-path="([^"]+)"', container).group(1)
    opf_dir = posixpath.dirname(opf_path)
    opf = z.read(opf_path).decode()
    manifest = {m.group(1): html.unescape(m.group(2)) for m in
                re.finditer(r'<item\b[^>]*\bid="([^"]+)"[^>]*\bhref="([^"]+)"', opf)}
    manifest.update({m.group(2): html.unescape(m.group(1)) for m in
                     re.finditer(r'<item\b[^>]*\bhref="([^"]+)"[^>]*\bid="([^"]+)"', opf)})
    spine = [manifest[i] for i in re.findall(r'<itemref\b[^>]*\bidref="([^"]+)"', opf)]
    read = lambda href: z.read(posixpath.join(opf_dir, href) if opf_dir else href)
    m = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", opf, re.S)
    title = norm_ws(html.unescape(m.group(1))) if m else Path(path).stem
    return z, spine, read, title


# ---------------------------------------------------------------- text helpers

def seg_text(segs):
    return "".join(s for s, _ in segs)


def norm_ws(s):
    return re.sub(r"\s+", " ", s).strip()


def key(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def is_bold_only(segs):
    text = [(s, st) for s, st in segs if s.strip()]
    return bool(text) and all("b" in st for _, st in text)


def split_segs(segs, offset):
    """Split a segment list at a character offset into (head, tail)."""
    head, tail, pos = [], [], 0
    for s, st in segs:
        if pos + len(s) <= offset:
            head.append((s, st))
        elif pos >= offset:
            tail.append((s, st))
        else:
            head.append((s[:offset - pos], st))
            tail.append((s[offset - pos:], st))
        pos += len(s)
    return head, tail


# ---------------------------------------------------------------- code heuristics

CODE_START = re.compile(r"""^(?:
    (?:public|private|global|protected)
        (?:\s+(?:static|virtual|abstract|override|final|transient|testmethod|webservice
              |with\s+sharing|without\s+sharing|inherited\s+sharing))*
        \s+(?:class|interface|enum|void|[A-Za-z_][\w.]*(?:<.*>)?(?:\[\])?)\b
  | (?:class|interface|enum)\s+[A-Za-z_]\w*\s*(?:\{|extends\b|implements\b|$)
  | trigger\s+\w+\s+on\s+\w+
  | (?:if|for|while|catch|switch\s+on)\s*\(
  | (?:else|try|finally|do)\b\s*(?:\{.*|if\b.*)?$
  | @[A-Za-z]\w*(?:\(.*?\))?(?:\s*$|\s+(?:public|private|global|protected|static|void|webservice
        |testmethod|override|[A-Z][\w<>,]*\s+\w+\s*\())
  | //|/\*|\*/|\*\s|\*$
  | [}\])]
  | </?[A-Za-z!?][\w:.-]*(?:[\s>/]|$)
  | \[\s*(?i:select)\b
  | (?:List|Map|Set)\s*<
  | [A-Za-z_][\w.]*\s*\([^()]*\)\s*$
  | [A-Z_]{4,}(?:\||\s\[|$)
)""", re.X)

PROSE_WORD = re.compile(r"[A-Za-z’'\-,.;:!?()“”\"–—]+")


def strip_comment(line):
    """Drop a trailing // comment, ignoring // inside string literals."""
    q = None
    for i, c in enumerate(line):
        if q:
            if c == "\\":
                continue
            if c == q:
                q = None
        elif c in "'\"":
            q = c
        elif line.startswith("//", i) and not line.startswith("://", i - 1):
            return line[:i]
    return line


def is_prose(t):
    """Lines that are certainly prose, even inside an unclosed code fragment."""
    if re.match(r"(//|/\*|\*|<|>|[{}\[\]()])", t) or t.endswith((";", "{")) or CODE_START.match(t) and t[0] == "@":
        return False
    if "{" in t or "}" in t:
        return False
    if re.match(r"(public|private|global|protected)\s+\w+", t) and re.search(r"[;(]", t):
        return False
    words = t.split()
    alpha = sum(1 for w in words if PROSE_WORD.fullmatch(w)) / len(words)
    ends = re.search(r'[.?!:]["”’)\]]*$', t)
    if len(words) >= 12 and alpha >= 0.75 and " = " not in t:
        return True
    if ends and len(words) >= 8 and alpha >= 0.7:
        return True
    if ends and (len(words) >= 2 or t.endswith(":")) and t[0].isupper() and alpha >= 0.8 \
            and not re.search(r"\w\(|=", t):
        return True
    return False


def looks_prose(t):
    words = t.split()
    return len(words) >= 6 and not re.search(r"[;{}=]|\w\(", t) \
        and sum(1 for w in words if PROSE_WORD.fullmatch(w)) / len(words) >= 0.85


def is_strong_code(t):
    if CODE_START.match(t):
        return True
    s = strip_comment(t).rstrip()
    if s.endswith((";", "{")):
        return True
    if s.endswith("}") and ("{" in s or len(s.split()) <= 3):
        return True
    return False


def is_weak_code(t):
    if re.search(r"(,|\(|=|\+|\|\||&&|\?|:|\[|=>)$", t) or re.match(r"(\.|\|\||&&|\+|\?|:|'|\"|,)", t):
        return True
    return sum(t.count(c) for c in ";{}()=[]<>'\"") / max(len(t), 1) >= 0.08


def is_incomplete(t):
    """A code line that the next line must continue."""
    if re.match(r"@\w+(\(.*\))?$", t):
        return True
    s = strip_comment(t).rstrip()
    if not s:
        return False
    return not re.search(r"(?:[;{}>]|\*/)$", s)


def brace_delta(line, opens="{[", closes="}]"):
    """Net bracket count outside strings and comments; returns (delta, paren_delta, in_block)."""
    d = p = 0
    q = None
    i = 0
    while i < len(line):
        c = line[i]
        if q:
            if c == "\\":
                i += 1
            elif c == q:
                q = None
        elif line.startswith("//", i):
            break
        elif c in "'\"":
            q = c
        elif c in opens:
            d += 1
        elif c in closes:
            d -= 1
        elif c == "(":
            p += 1
        elif c == ")":
            p -= 1
        i += 1
    return d, p


def split_statements(line):
    """Split merged statements at '; ' outside strings, parens and same-line braces."""
    out, start, paren, brace, q = [], 0, 0, 0, None
    i = 0
    while i < len(line):
        c = line[i]
        if q:
            if c == "\\":
                i += 1
            elif c == q:
                q = None
        elif line.startswith("//", i):
            break
        elif c in "'\"":
            q = c
        elif c == "(":
            paren += 1
        elif c == ")":
            paren -= 1
        elif c == "{":
            brace += 1
        elif c == "}":
            brace -= 1
        elif c == ";" and paren <= 0 and brace <= 0:
            rest = line[i + 1:].lstrip()
            if rest and not rest.startswith("//"):
                out.append(line[start:i + 1])
                start = len(line) - len(rest)
        i += 1
    out.append(line[start:])
    return [s.strip() for s in out if s.strip()]


COMMENT_CODE = re.compile(
    r"\s((?:List|Map|Set)<|(?:for|if|while)\s*\(|(?:public|private|global|protected)\s+[A-Z]\w*[\w<>,]*\s+\w+\s*[;=(]|[A-Z]\w*(?:<[^=]*>)?\s+[a-z]\w*\s*=\s)")


def split_comment_code(line):
    """'// comment text Map<ID, X> y = ...' -> ['// comment text', 'Map<ID, X> y = ...']."""
    if not line.startswith("//"):
        return [line]
    m = COMMENT_CODE.search(line)
    if m and len(line[2:m.start()].split()) >= 2:
        return [line[:m.start()].rstrip(), line[m.start() + 1:]]
    return [line]


def explode(line):
    """Split one source line into statements, peeling off code that follows a // comment."""
    out, todo = [], [line]
    while todo:
        l = todo.pop(0).strip()
        if not l:
            continue
        if l.startswith(("/*", "*")):
            out.append(l)
            continue
        code = strip_comment(l)
        comment, rest = l[len(code):], None
        if comment:
            parts = split_comment_code(comment)
            comment, rest = parts[0], (parts[1] if len(parts) > 1 else None)
        stmts = split_statements(code) if code.strip() else []
        if comment:
            if stmts:
                stmts[-1] += " " + comment
            else:
                stmts = [comment]
        out.extend(stmts)
        if rest:
            todo.insert(0, rest)
    return out


LOG_LINE = re.compile(r"[A-Z_]{4,}(?:\||\s\[|$)")


def block_lang(lines):
    body = [l for l in lines if l.strip()]
    if sum(bool(LOG_LINE.match(l)) for l in body) * 2 >= len(body):
        return "text"
    if any(l.startswith("<?xml") for l in body):
        return "xml"
    if sum(l.startswith(("<", ">")) for l in body) * 2 >= len(body):
        return "html"
    if body and body[0] in ("{", "[") and any(re.match(r'"\w+"\s*:', l) for l in body) \
            and not any(";" in l for l in body):
        return "json"
    if any("=>" in l or re.match(r"(import|export|const|let)\s", l) for l in body):
        return "javascript"
    return "apex"


def format_code(raw_lines):
    lang = block_lang(raw_lines)
    lines = []
    for l in raw_lines:
        l = norm_ws(l) if lang not in ("html", "xml") else l.strip()
        if not l:
            lines.append("")
        elif lang in ("apex", "javascript"):
            lines.extend(explode(l))
        else:
            lines.append(l)
    out, depth, paren, prev = [], 0, 0, ""
    for l in lines:
        if not l:
            if out and out[-1]:
                out.append("")
            continue
        if lang in ("html", "xml"):
            out.append(l)
            continue
        lead = re.match(r"[\s})\];,.]*", l).group()
        dedent = lead.count("}") + lead.count("]")
        prev_s = strip_comment(prev).rstrip()
        cont = 1 if paren > 0 or re.search(r"(=|\+|\|\||&&|\?)$", prev_s) else 0
        out.append("    " * (max(depth - dedent, 0) + cont) + l)
        d, p = brace_delta(l)
        depth, paren = max(depth + d, 0), max(paren + p, 0)
        if not l.startswith(("//", "/*", "*")):
            prev = l
    while out and not out[-1]:
        out.pop()
    return lang, out


# ---------------------------------------------------------------- markdown helpers

def escape_prose(s):
    s = s.replace("\\", "\\\\").replace("`", "\\`").replace("*", "\\*").replace("<", "\\<")
    return s


def render_segs(segs):
    merged = []
    for s, st in segs:
        st = st if s.strip() else ""
        if merged and merged[-1][1] == st:
            merged[-1][0] += s
        elif merged and not s.strip():
            merged[-1][0] += s
        else:
            merged.append([s, st])
    out = []
    for s, st in merged:
        text = escape_prose(s)
        if st:
            mark = {"b": "**", "i": "*", "bi": "***"}[st]
            core = text.strip()
            lead, trail = text[:len(text) - len(text.lstrip())], text[len(text.rstrip()):]
            text = f"{lead}{mark}{core}{mark}{trail}" if core else text
        out.append(text)
    line = norm_ws("".join(out))
    line = re.sub(r"^(#|>|[-+] |\d+\. )", lambda m: "\\" + m.group(1), line)
    return line


def gh_slug(text, seen):
    s = re.sub(r"[^\w\- ]", "", text.lower().strip()).replace(" ", "-")
    n = seen[s]
    seen[s] += 1
    return s if n == 0 else f"{s}-{n}"


CAPTION = re.compile(r"^Figure \d+-\d+\s*[–-]")
CAPTION_BREAK = re.compile(
    r"(?<=[a-z0-9)]) (?=(?:As|To|This|Is|In|Yes|Let’s|With|Always|A|The|If|You|Now|Here|When|"
    r"Once|It|Notice|Note|What|So|But|For|At|I|Why)\b)")


def split_caption(text):
    """'Figure 1-2 –Caption words As you can see...' -> (caption, rest)."""
    m = CAPTION.match(text)
    if not m:
        return None
    for b in CAPTION_BREAK.finditer(text, m.end()):
        if len(text[m.end():b.start()].split()) >= 2:
            return text[:b.start()], text[b.end():]
    return text, ""


# ---------------------------------------------------------------- conversion

def convert(epub, out_dir, diag):
    z, spine, read, title = read_epub(epub)
    body_files, outline = [], []
    for href in spine:
        if not href.endswith((".html", ".xhtml")):
            continue
        text = read(href).decode("utf-8")
        if "Document Outline" in text:
            op = OutlineParser()
            op.feed(text)
            outline = op.entries
        elif "<img" in text or "<p" in text:
            body_files.append((href, text))

    items = []
    for href, text in body_files:
        bp = BodyParser()
        bp.feed(text)
        items.extend(bp.items)
    page = 0
    for it in items:
        if it["t"] == "anchor" and re.fullmatch(r"p\d+", it["id"]):
            page = int(it["id"][1:])
        it["page"] = page

    # pages before the book's own table of contents are front matter; its TOC pages are dropped
    toc_entry = next((e for e in outline if key(e["title"]) == "tableofcontents"), None)
    if toc_entry:
        later = [e["page"] for e in outline if e["page"] > toc_entry["page"]]
        toc_start, toc_end = toc_entry["page"], min(later, default=toc_entry["page"] + 1)
    else:
        toc_start = toc_end = min((e["page"] for e in outline), default=0)
    front = [it for it in items if it["page"] < toc_start]
    body = [it for it in items if it["page"] >= toc_end]
    stats = collections.Counter()

    # headings from the outline
    entries = [e for e in outline if e is not toc_entry]
    cursor, unmatched = 0, []
    for e in entries:
        k = key(e["title"])
        hit = None
        i = cursor
        while i < len(body) and body[i]["page"] <= e["page"] + 1 and hit is None:
            it = body[i]
            if it["t"] == "p" and is_bold_only(it["segs"]):
                joined, j, used = "", i, []
                while j < len(body) and len(used) < 3:
                    if body[j]["t"] == "anchor":
                        j += 1
                        continue
                    if body[j]["t"] != "p" or not is_bold_only(body[j]["segs"]):
                        break
                    joined += seg_text(body[j]["segs"])
                    used.append(j)
                    if key(joined) == k:
                        hit = ("whole", used)
                        break
                    if not k.startswith(key(joined)):
                        break
                    j += 1
            elif it["t"] == "p":
                segs = it["segs"]
                bold = ""
                for n, (s, st) in enumerate(segs):
                    if "b" not in st and s.strip():
                        break
                    bold += s
                if bold.strip() and key(bold) == k and n < len(segs):
                    hit = ("prefix", i, len(bold))
                tail = ""
                for s, st in reversed(segs):
                    if "b" not in st and s.strip():
                        break
                    tail = s + tail
                if hit is None and tail.strip() and key(tail) == k:
                    hit = ("suffix", i, len(seg_text(segs)) - len(tail))
            i += 1
        heading = {"t": "h", "level": {1: 2, 2: 3, 3: 4}[e["level"]], "title": e["title"], "entry": e}
        if e["level"] == 1 and e["title"].startswith("Part "):
            heading["level"] = 1
        e["heading"] = heading
        if hit and hit[0] == "whole":
            used = hit[1]
            heading["page"] = body[used[0]]["page"]
            for j in used:
                body[j] = {"t": "skip", "page": body[j]["page"]}
            body[used[0]] = heading
            cursor = used[-1] + 1
            stats["heading matched"] += 1
        elif hit and hit[0] == "suffix":
            _, i, off = hit
            head, _ = split_segs(body[i]["segs"], off)
            heading["page"] = body[i]["page"]
            body[i:i + 1] = [{"t": "p", "segs": head, "page": body[i]["page"]}, heading]
            cursor = i + 2
            stats["heading matched (bold suffix)"] += 1
        elif hit:
            _, i, off = hit
            _, rest = split_segs(body[i]["segs"], off)
            heading["page"] = body[i]["page"]
            body[i:i + 1] = [heading, {"t": "p", "segs": rest, "page": body[i]["page"]}]
            cursor = i + 2
            stats["heading matched (bold prefix)"] += 1
        else:
            i = next((n for n, it in enumerate(body) if n >= cursor and it["page"] >= e["page"]), len(body))
            if i < len(body) and body[i]["t"] == "anchor":
                i += 1
            heading["page"] = e["page"]
            body.insert(i, heading)
            cursor = i + 1
            unmatched.append(e["title"])
    body = [it for it in body if it["t"] != "skip"]

    for n, it in enumerate(body):
        if it["t"] != "p" or not is_bold_only(it["segs"]):
            continue
        t = norm_ws(seg_text(it["segs"]))
        nxt = next((b for b in body[n + 1:] if b["t"] != "anchor"), None)
        nxt_t = norm_ws(seg_text(nxt["segs"])) if nxt and nxt["t"] == "p" else ""
        continued = nxt_t and is_bold_only(nxt["segs"]) and (nxt_t[0].islower() or nxt_t[0] in "“\"")
        if len(t.split()) <= 12 and re.match(r"[A-Z0-9]", t) and not re.search(r"[.!?,;:]$", t) \
                and not re.match(r"Note \d", t) and not continued:
            it.update(t="h", level=4 if entries else 2, title=t)
            stats["heading from unmatched bold"] += 1

    # split "<code>; Prose sentence..." paragraphs
    out_items = []
    for it in body:
        if it["t"] == "p":
            t = seg_text(it["segs"])
            for m in re.finditer(r"[;}] (?=[A-Z])", t):
                head, tail = t[:m.start() + 1].strip(), t[m.end():].strip()
                if is_strong_code(head) and not is_prose(head) and len(tail.split()) >= 6 and (is_prose(tail) or looks_prose(tail)):
                    h, tl = split_segs(it["segs"], m.end())
                    out_items.append({"t": "p", "segs": h, "page": it["page"]})
                    it = {"t": "p", "segs": tl, "page": it["page"]}
                    stats["code/prose tail split"] += 1
                    break
        out_items.append(it)
    body = out_items

    # split "prose ...: <code start>" paragraphs
    out_items = []
    for n, it in enumerate(body):
        if it["t"] == "p":
            t = seg_text(it["segs"])
            cut = t.rfind(": ")
            if cut > 0 and not CODE_START.match(t.strip()):
                head, tail = t[:cut + 1].strip(), t[cut + 2:].strip()
                nxt = next((b for b in body[n + 1:] if b["t"] == "p" and seg_text(b["segs"]).strip()), None)
                nxt_code = nxt is not None and is_strong_code(norm_ws(seg_text(nxt["segs"])))
                if tail and len(head.split()) >= 4 and len(tail.split()) <= 15 \
                        and not tail.endswith(".") and not is_prose(tail) and (is_strong_code(tail) or (nxt_code and is_weak_code(tail))):
                    h, tl = split_segs(it["segs"], cut + 2)
                    out_items.append({"t": "p", "segs": h, "page": it["page"]})
                    out_items.append({"t": "p", "segs": tl, "page": it["page"], "forced_code": True})
                    stats["prose/code tail split"] += 1
                    continue
        out_items.append(it)
    body = out_items

    # classify code lines within runs bounded by headings and images
    kinds = {}
    in_code, depth, incomplete, in_block = False, 0, False, False
    for n, it in enumerate(body):
        if it["t"] != "p":
            if it["t"] in ("h", "img"):
                in_code, depth, incomplete, in_block = False, 0, False, False
            continue
        t = norm_ws(seg_text(it["segs"]))
        if not t:
            kinds[n] = "blank"
            continue
        if it.get("forced_code") or in_block:
            code = True
        elif is_prose(t):
            code = False
        elif is_strong_code(t):
            code = True
        elif in_code and (incomplete or depth > 0 or is_weak_code(t)):
            code = True
        else:
            code = False
        kinds[n] = "code" if code else "prose"
        if code:
            if not in_code:
                depth = 0
            d, _ = brace_delta(t, "{", "}")
            depth = max(depth + d, 0)
            incomplete = is_incomplete(t)
            if "/*" in t and "*/" not in t[t.index("/*"):]:
                in_block = True
            elif "*/" in t:
                in_block = False
        else:
            depth, incomplete, in_block = 0, False, False
        in_code = code

    # sandwich rule: one non-certain prose line between code lines is code
    order = [n for n in sorted(kinds) if kinds[n] != "blank"]
    for a, b, c in zip(order, order[1:], order[2:]):
        if kinds[a] == "code" and kinds[c] == "code" and kinds[b] == "prose" \
                and not is_prose(norm_ws(seg_text(body[b]["segs"]))) \
                and all(body[x]["t"] in ("p", "anchor") for x in range(a, c + 1)):
            kinds[b] = "code"
            stats["sandwiched line to code"] += 1

    # group into blocks
    grouped, n = [], 0
    while n < len(body):
        it = body[n]
        if it["t"] == "p" and kinds.get(n) == "code":
            lines, m = [], n
            while m < len(body):
                b = body[m]
                if b["t"] == "anchor":
                    m += 1
                    continue
                if b["t"] != "p":
                    break
                k = kinds.get(m)
                if k == "code":
                    lines.append(seg_text(b["segs"]))
                elif k == "blank":
                    nxt = next((x for x in range(m + 1, len(body)) if body[x]["t"] != "anchor"
                                and kinds.get(x) != "blank"), None)
                    if nxt is None or kinds.get(nxt) != "code":
                        break
                    lines.append("")
                else:
                    break
                m += 1
            lang, code = format_code(lines)
            grouped.append({"t": "code", "lang": lang, "lines": code})
            stats[f"code block ({lang})"] += 1
            n = m
            continue
        if it["t"] == "p" and kinds.get(n) == "blank":
            n += 1
            continue
        grouped.append(it)
        n += 1

    # prose repair across page breaks
    all_text = " ".join(seg_text(it["segs"]) for it in grouped if it["t"] == "p")
    joins, merged, i = [], [], 0
    while i < len(grouped):
        it = grouped[i]
        if it["t"] != "p":
            merged.append(it)
            i += 1
            continue
        segs, deferred = list(it["segs"]), []
        j = i + 1
        while True:
            k = j
            held = []
            while k < len(grouped) and grouped[k]["t"] in ("anchor", "img"):
                held.append(grouped[k])
                k += 1
            if k >= len(grouped) or grouped[k]["t"] != "p":
                break
            nxt = grouped[k]
            a, b = seg_text(segs).rstrip(), seg_text(nxt["segs"]).lstrip()
            if not a or not b or not (b[0].islower() or b[0] == "@") or CAPTION.match(b) or CAPTION.match(a):
                break
            m = re.search(r"([A-Za-z]+)-$", a)
            if m and b[0].islower():
                first = re.match(r"[a-z]+", b).group()
                hyphenated = f"{m.group(1)}-{first}"
                keep = hyphenated in all_text and f"{m.group(1)}{first}" not in all_text
                segs = _trim_end(segs, 0 if keep else 1) + _trim_start(nxt["segs"])
                joins.append(hyphenated if keep else m.group(1) + first)
            elif not re.search(r"[.?!:;][\"”’)\]]*$", a):
                segs = _trim_end(segs, 0) + [(" ", "")] + _trim_start(nxt["segs"])
                stats["sentence joins"] += 1
            else:
                break
            deferred.extend(held)
            j = k + 1
        merged.append({"t": "p", "segs": segs, "page": it["page"]})
        merged.extend(deferred)
        i = j if j > i + 1 else i + 1
    stats["hyphen joins"] = len(joins)

    # emit
    out_dir = Path(out_dir)
    (out_dir / "images").mkdir(parents=True, exist_ok=True)
    images = []
    body_md = []
    seen = collections.Counter()
    gh_slug(title, seen)
    if entries:
        gh_slug("Table of Contents", seen)

    def emit(it, dest, front_matter=False):
        if it["t"] == "h":
            it["slug"] = gh_slug(it["title"], seen)
            dest.append("#" * it["level"] + " " + it["title"])
        elif it["t"] == "img":
            images.append(it["src"])
            dest.append(f"![Figure](images/{posixpath.basename(it['src'])})")
        elif it["t"] == "code":
            dest.append(f"```{it['lang']}\n" + "\n".join(it["lines"]) + "\n```")
        elif it["t"] == "p":
            text = seg_text(it["segs"])
            if not text.strip():
                return
            if front_matter and is_bold_only(it["segs"]):
                t = norm_ws(text)
                if t.startswith(title):
                    t = t[len(title):].strip()
                if t:
                    dest.append(f"**{t}**")
                return
            cap = split_caption(norm_ws(text))
            if cap:
                caption, rest = cap
                dest.append(f"*{escape_prose(caption)}*")
                stats["figure captions"] += 1
                if rest:
                    dest.append(escape_prose(rest))
                return
            dest.append(render_segs(it["segs"]))

    front_md = []
    for it in front:
        emit(it, front_md, front_matter=True)
    for it in merged:
        emit(it, body_md)

    toc = []
    for e in entries:
        h = e["heading"]
        indent = "  " * (e["level"] - 1)
        toc.append(f"{indent}- [{e['title']}](#{h['slug']})")

    md = [f"# {title}", *front_md, *(["## Table of Contents", "\n".join(toc)] if toc else []), *body_md]
    name = re.sub(r'[\\/:*?"<>|]', "", title).strip() or "book"
    (out_dir / f"{name}.md").write_text("\n\n".join(md) + "\n", encoding="utf-8")
    for src in images:
        (out_dir / "images" / posixpath.basename(src)).write_bytes(read(src))

    if diag:
        for k, v in sorted(stats.items()):
            print(f"{k}: {v}", file=sys.stderr)
        print(f"outline entries: {len(entries)}; not matched in body (inserted at page anchor): {unmatched}",
              file=sys.stderr)
        print(f"hyphen joins: {joins}", file=sys.stderr)


def _trim_end(segs, n):
    """Strip trailing whitespace and then n characters from a segment list."""
    segs = [list(s) for s in segs]
    while segs and not segs[-1][0].strip():
        segs.pop()
    if segs:
        segs[-1][0] = segs[-1][0].rstrip()
        segs[-1][0] = segs[-1][0][:len(segs[-1][0]) - n]
    return [tuple(s) for s in segs]


def _trim_start(segs):
    segs = [list(s) for s in segs]
    while segs and not segs[0][0].strip():
        segs.pop(0)
    if segs:
        segs[0][0] = segs[0][0].lstrip()
    return [tuple(s) for s in segs]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("epub")
    ap.add_argument("out_dir")
    ap.add_argument("--diag", action="store_true")
    args = ap.parse_args()
    convert(args.epub, args.out_dir, args.diag)

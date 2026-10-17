"""Redaction inside Office Open XML (Word, Excel, PowerPoint, Visio) and
OpenDocument packages.

Works on the XML parts directly with lxml rather than through openpyxl /
python-docx, so nothing the libraries don't model (charts, shapes, pivot
caches, VBA, custom XML) is dropped on save, and every part is covered:
body text, headers and footers, comments, tracked changes, field codes,
hyperlink targets, chart data, Visio shape data, embedded packages.

Word and PowerPoint routinely split one visible string over several runs
("10.1." in one, "2.3" in the next after a spell-check or formatting
change), so text is redacted per paragraph and edits are spread back over
the runs they came from.
"""

import bisect
import io
import zipfile

from .files import SkipFile

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
X = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
W_STRICT = "http://purl.oclc.org/ooxml/wordprocessingml/main"
A_STRICT = "http://purl.oclc.org/ooxml/drawingml/main"
X_STRICT = "http://purl.oclc.org/ooxml/spreadsheetml/main"
VISIO = "http://schemas.microsoft.com/office/visio/2012/main"
ODF_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
ODF_MANIFEST = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"
PKG_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
PKG_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def _q(ns, *names):
    return {f"{{{ns}}}{n}" for n in names}


# Paragraph-like containers -> the elements inside them whose .text is
# visible text. None means mixed content: every text and tail node inside
# the container is part of the paragraph (Visio <Text>, ODF <text:p>).
CONTAINERS = {}
for _w in (W, W_STRICT):
    CONTAINERS.update(dict.fromkeys(_q(_w, "p"), _q(_w, "t", "delText", "instrText", "delInstrText")))
for _a in (A, A_STRICT):
    CONTAINERS.update(dict.fromkeys(_q(_a, "p"), _q(_a, "t")))
for _x in (X, X_STRICT):
    CONTAINERS.update(dict.fromkeys(_q(_x, "si", "is", "text"), _q(_x, "t")))
CONTAINERS.update(dict.fromkeys(_q(VISIO, "Text"), None))
CONTAINERS.update(dict.fromkeys(_q(ODF_TEXT, "p", "h"), None))

# Elements where leading/trailing spaces are dropped unless xml:space is set.
SPACE_SENSITIVE = _q(W, "t", "delText", "instrText", "delInstrText") | _q(X, "t") \
    | _q(W_STRICT, "t", "delText", "instrText", "delInstrText") | _q(X_STRICT, "t")

XML_SUFFIXES = (".xml", ".rels", ".vml")
MAX_DEPTH = 3                   # packages embedded in packages embedded in ...
MAX_UNCOMPRESSED = 2 * 1024**3  # refuse zip bombs


def is_package(data):
    """An OOXML or ODF package (as opposed to any other zip file)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
    except zipfile.BadZipFile:
        return False
    return "[Content_Types].xml" in names or "mimetype" in names


def _parser():
    from lxml import etree
    # No entity expansion and no network access: document XML is untrusted.
    return etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)


class _Part:
    """Redacts one parsed XML part in place."""

    def __init__(self, redactor, reverse):
        self.r = redactor
        self.reverse = reverse
        self.groups = {}   # container element -> [(element, "text"|"tail"), ...]
        self.loose = []    # text slots outside any container
        self.changed = False

    def collect(self, el, container):
        if isinstance(el.tag, str):
            if el.tag in CONTAINERS:
                container = el
            for name, value in el.attrib.items():
                new = self.r.redact_text(value, self.reverse)
                if new != value:
                    el.set(name, new)
                    self.changed = True
            if el.text:
                self._slot(el, "text", container, el.tag)
            for child in el:
                self.collect(child, container)
                if child.tail:
                    self._slot(child, "tail", container, None)

    def _slot(self, el, kind, container, tag):
        if container is not None:
            runs = CONTAINERS[container.tag]
            if runs is None or (kind == "text" and tag in runs):
                self.groups.setdefault(container, []).append((el, kind))
                return
        # Not visible paragraph text (a formula, a cached value, a number in
        # a drawing): redact the node on its own so it can't combine with
        # neighbouring text into something that looks like an address.
        self.loose.append((el, kind))

    def apply(self):
        for el, kind in self.loose:
            old = getattr(el, kind)
            new = self.r.redact_text(old, self.reverse)
            if new != old:
                self._set(el, kind, new)
        for slots in self.groups.values():
            self._apply_group(slots)

    def _apply_group(self, slots):
        texts = [getattr(el, kind) or "" for el, kind in slots]
        full = "".join(texts)
        spans = self.r.spans(full, self.reverse)
        if not spans:
            return
        starts, pos = [], 0
        for t in texts:
            starts.append(pos)
            pos += len(t)
        work = list(texts)
        # Right to left, so earlier offsets stay valid. Each replacement goes
        # into the run where the address starts; the rest of the address is
        # cut from the runs it spilled into.
        for start, end, new in reversed(spans):
            i = bisect.bisect_right(starts, start) - 1
            j = bisect.bisect_right(starts, end - 1) - 1
            if i == j:
                work[i] = work[i][:start - starts[i]] + new + work[i][end - starts[i]:]
            else:
                work[i] = work[i][:start - starts[i]] + new
                for k in range(i + 1, j):
                    work[k] = ""
                work[j] = work[j][end - starts[j]:]
        for (el, kind), old, new in zip(slots, texts, work):
            if new != old:
                self._set(el, kind, new)

    def _set(self, el, kind, value):
        setattr(el, kind, value)
        if kind == "text" and el.tag in SPACE_SENSITIVE and value != value.strip():
            el.set(XML_SPACE, "preserve")
        self.changed = True


def redact_xml(redactor, data, reverse, drop=frozenset()):
    """Redact one XML part. `drop` holds part names (thumbnails) whose
    relationships / manifest entries / content-type overrides to remove.
    Returns the new bytes, or None if nothing changed."""
    from lxml import etree
    tree = etree.parse(io.BytesIO(data), _parser())
    root = tree.getroot()
    part = _Part(redactor, reverse)
    part.collect(root, None)
    part.apply()
    changed = part.changed
    if drop:
        changed |= _drop_references(root, drop)
    if not changed:
        return None
    info = tree.docinfo
    return etree.tostring(tree, xml_declaration=True, encoding=info.encoding or "UTF-8",
                          standalone=info.standalone)


def _drop_references(root, drop):
    removed = False
    for el in list(root.iter(f"{{{PKG_RELS}}}Relationship")):
        if el.get("TargetMode") != "External" and el.get("Target", "").lstrip("/") in drop:
            el.getparent().remove(el)
            removed = True
    for el in list(root.iter(f"{{{PKG_TYPES}}}Override")):
        if el.get("PartName", "").lstrip("/") in drop:
            el.getparent().remove(el)
            removed = True
    for el in list(root.iter(f"{{{ODF_MANIFEST}}}file-entry")):
        if el.get(f"{{{ODF_MANIFEST}}}full-path") in drop:
            el.getparent().remove(el)
            removed = True
    return removed


def _is_thumbnail(name):
    lower = name.lower()
    return lower.startswith("docprops/thumbnail") or lower.startswith("thumbnails/")


def redact_package(redactor, data, reverse, notes, depth=0):
    try:
        import lxml  # noqa: F401
    except ImportError:
        raise SkipFile("Office/Visio/OpenDocument files need lxml (pip install 'ipredact[office]')")
    try:
        zin = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise SkipFile(f"not a valid zip package: {e}")
    infos = zin.infolist()
    if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
        raise SkipFile("package expands to more than 2 GiB; refusing (possible zip bomb)")
    names = {i.filename for i in infos}
    if "[Content_Types].xml" not in names and "mimetype" not in names:
        raise SkipFile("zip file that isn't an Office/Visio/OpenDocument package")

    # The preview image shows the first page / slide / sheet, addresses and
    # all, and can't be redacted. Remove it along with its references.
    thumbs = {n for n in names if _is_thumbnail(n)}
    if thumbs:
        notes.append("removed thumbnail preview image (it could show unredacted addresses)")
    opaque = 0

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zout:
        for info in infos:
            if info.filename in thumbs:
                continue
            content = zin.read(info)
            lower = info.filename.lower()
            new = None
            if lower.endswith(XML_SUFFIXES) or info.filename == "[Content_Types].xml":
                try:
                    new = redact_xml(redactor, content, reverse, thumbs)
                except Exception:
                    # Legacy VML isn't always well-formed XML. Addresses are
                    # ASCII, so a text-level pass is still safe.
                    text = content.decode("utf-8", "surrogateescape")
                    redone = redactor.redact_text(text, reverse)
                    new = redone.encode("utf-8", "surrogateescape") if redone != text else None
            elif content[:4] == b"PK\x03\x04" and is_package(content):
                if depth >= MAX_DEPTH:
                    raise SkipFile("packages nested too deeply")
                new = redact_package(redactor, content, reverse, notes, depth + 1)
            elif lower.endswith((".bin", ".ole")) or "vbaproject" in lower:
                opaque += 1
            zi = zipfile.ZipInfo(info.filename, info.date_time)
            zi.compress_type = info.compress_type
            zi.external_attr = info.external_attr
            zi.create_system = info.create_system
            zout.writestr(zi, content if new is None else new)

    workbook = next((n for n in names if n.lower() in ("xl/workbook.xml",)), None)
    if workbook is not None:
        _check_sheet_names(out.getvalue(), workbook)
    if opaque:
        notes.append(f"{opaque} embedded binary object(s) or macro project(s) were not inspected")
    return out.getvalue()


def _check_sheet_names(data, workbook):
    from lxml import etree
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = etree.fromstring(z.read(workbook), _parser())
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.endswith("}sheet") and len(el.get("name", "")) > 31:
            raise SkipFile(f"sheet name {el.get('name')!r} would exceed Excel's 31-character limit")


def redact_package_file(redactor, infile, outfile, reverse, notes):
    outfile.write(redact_package(redactor, infile.read(), reverse, notes))


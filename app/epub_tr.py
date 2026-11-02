"""Traduce un EPUB cambiando solo el texto: estilos, imágenes y enlaces siguen intactos."""
from __future__ import annotations

import copy
import html
import html.entities
import posixpath
import re
import zipfile
from pathlib import Path
from urllib.parse import unquote

from lxml import etree
from lxml import html as lxml_html

from .pipeline import Item, Translator, has_letters, normalize

XHTML = "http://www.w3.org/1999/xhtml"
OPF = "http://www.idpf.org/2007/opf"
DC = "http://purl.org/dc/elements/1.1/"
NCX = "http://www.daisy.org/z3986/2005/ncx/"
EPUB_NS = "http://www.idpf.org/2007/ops"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
FONT_OBFUSCATION = {"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"}
MAX_TOTAL_BYTES = 2_000_000_000

BLOCK = {
    "address", "article", "aside", "blockquote", "body", "caption", "dd", "details", "div", "dl", "dt",
    "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "li", "main",
    "nav", "ol", "p", "section", "summary", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}
SKIP = {"script", "style", "code", "pre", "kbd", "samp", "var", "math", "svg", "head", "rt", "rp"}
ATOMIC = {"img", "image", "br", "hr", "wbr", "object", "video", "audio", "math", "svg", "code", "kbd", "samp", "var"}
INSIDE_ONLY = {"li", "td", "th", "dt", "dd", "caption", "figcaption", "summary"}
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class EpubError(Exception):
    pass


def _local(el) -> str:
    return etree.QName(el).localname.lower() if isinstance(el.tag, str) else ""


def _parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, recover=True)


def _replace_named_entities(data: bytes) -> bytes:
    text = data.decode("utf-8", errors="replace")

    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name in ("amp", "lt", "gt", "quot", "apos"):
            return match.group(0)
        code = html.entities.name2codepoint.get(name)
        return f"&#{code};" if code else ""

    return re.sub(r"&([A-Za-z][A-Za-z0-9]*);", replace, text).encode("utf-8")


def _parse(data: bytes) -> etree._ElementTree:
    tree = etree.ElementTree(etree.fromstring(_replace_named_entities(data), _parser()))
    return tree


def _serialize(tree: etree._ElementTree) -> bytes:
    return etree.tostring(tree, encoding="utf-8", xml_declaration=True, doctype=tree.docinfo.doctype or None)


class Book:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        try:
            self.zip = zipfile.ZipFile(self.path)
        except zipfile.BadZipFile as exc:
            raise EpubError("El archivo no es un EPUB válido.") from exc
        try:
            if sum(info.file_size for info in self.zip.infolist()) > MAX_TOTAL_BYTES:
                raise EpubError("El EPUB descomprimido es demasiado grande.")
            self._check_drm()
            self._read_package()
        except Exception:
            self.zip.close()
            raise

    def _check_drm(self) -> None:
        names = set(self.zip.namelist())
        if "META-INF/rights.xml" in names:
            raise EpubError("Este EPUB tiene protección DRM y no se puede traducir.")
        if "META-INF/encryption.xml" in names:
            root = etree.fromstring(self.zip.read("META-INF/encryption.xml"), _parser())
            methods = {
                node.get("Algorithm", "")
                for node in root.iter()
                if isinstance(node.tag, str) and etree.QName(node).localname == "EncryptionMethod"
            }
            if methods - FONT_OBFUSCATION:
                raise EpubError("Este EPUB tiene protección DRM y no se puede traducir.")

    def _read_package(self) -> None:
        try:
            container = etree.fromstring(self.zip.read("META-INF/container.xml"), _parser())
            rootfile = container.find(".//{*}rootfile")
            self.opf_path = rootfile.get("full-path")
            self.opf = _parse(self.zip.read(self.opf_path))
        except (KeyError, AttributeError, etree.XMLSyntaxError, ValueError) as exc:
            raise EpubError("No se encuentra la estructura interna del EPUB.") from exc
        base = posixpath.dirname(self.opf_path)
        self.items = {}
        self.documents: list[str] = []
        self.ncx = None
        root = self.opf.getroot()
        for item in root.iterfind(f".//{{{OPF}}}manifest/{{{OPF}}}item"):
            href = posixpath.normpath(posixpath.join(base, unquote(item.get("href", ""))))
            self.items[item.get("id")] = href
            media = item.get("media-type", "")
            if media in ("application/xhtml+xml", "text/html") and href in self.zip.namelist():
                self.documents.append(href)
            if media == "application/x-dtbncx+xml" and href in self.zip.namelist():
                self.ncx = href
        spine = [
            self.items.get(ref.get("idref"))
            for ref in root.iterfind(f".//{{{OPF}}}spine/{{{OPF}}}itemref")
        ]
        order = {href: index for index, href in enumerate(spine)}
        self.documents.sort(key=lambda href: order.get(href, 10 ** 6))
        title = root.find(f".//{{{DC}}}title")
        author = root.find(f".//{{{DC}}}creator")
        language = root.find(f".//{{{DC}}}language")
        self.title = (title.text or "").strip() if title is not None else ""
        self.author = (author.text or "").strip() if author is not None else ""
        self.language = (language.text or "").strip()[:2].lower() if language is not None else ""


class Block:
    """Un párrafo completo: mantiene cursivas, enlaces y notas como <span id>."""

    def __init__(self, element) -> None:
        self.element = element
        self.protos: dict[str, tuple] = {}
        self.counter = 0
        self.fragment = self._inner(element).strip()

    def _inner(self, element) -> str:
        parts = [_escape(element.text)]
        for child in element:
            if isinstance(child.tag, str):
                self.counter += 1
                ident = f"t{self.counter}"
                if _is_atomic(child):
                    self.protos[ident] = (child, True)
                    parts.append(f'<span id="{ident}" translate="no" class="notranslate">#</span>')
                else:
                    self.protos[ident] = (child, False)
                    parts.append(f'<span id="{ident}">{self._inner(child)}</span>')
            parts.append(_escape(child.tail))
        return "".join(parts)

    def apply(self, translated: str) -> list[str]:
        problems: list[str] = []
        try:
            holder = lxml_html.fragment_fromstring(CONTROL.sub("", translated), create_parent="div")
        except etree.ParserError:
            holder = lxml_html.fragment_fromstring(html.escape(translated), create_parent="div")
        used: set[str] = set()
        for child in list(self.element):
            self.element.remove(child)
        self.element.text = None
        self._fill(self.element, holder, used)
        for ident, (proto, atomic) in self.protos.items():
            if ident in used:
                continue
            if atomic:
                clone = copy.deepcopy(proto)
                clone.tail = None
                self.element.append(clone)
                problems.append(f"Se ha recolocado al final un elemento ({_local(proto)}).")
            else:
                problems.append(f"Se ha perdido un formato interno ({_local(proto)}).")
        return problems

    def _fill(self, target, source, used: set[str]) -> None:
        _append_text(target, source.text)
        for node in source:
            if not isinstance(node.tag, str):
                _append_text(target, node.tail)
                continue
            ident = node.get("id")
            proto = self.protos.get(ident) if ident else None
            if proto and ident not in used:
                used.add(ident)
                element, atomic = proto
                if atomic:
                    clone = copy.deepcopy(element)
                    clone.tail = None
                else:
                    clone = etree.Element(element.tag, attrib=dict(element.attrib), nsmap=element.nsmap)
                    self._fill(clone, node, used)
                target.append(clone)
            else:
                self._fill(target, node, used)
            _append_text(target, node.tail)


class TextRun:
    """Texto suelto dentro de un contenedor."""

    def __init__(self, element, attr: str) -> None:
        self.element = element
        self.attr = attr
        self.fragment = _escape(getattr(element, attr)).strip()

    def apply(self, translated: str) -> list[str]:
        original = getattr(self.element, self.attr) or ""
        lead = " " if original[:1].isspace() else ""
        trail = " " if original[-1:].isspace() else ""
        setattr(self.element, self.attr, lead + html.unescape(re.sub(r"<[^>]+>", "", translated)).strip() + trail)
        return []


def _escape(text: str | None) -> str:
    if not text:
        return ""
    return html.escape(re.sub(r"\s+", " ", CONTROL.sub("", text)), quote=False)


def _append_text(target, text: str | None) -> None:
    if not text:
        return
    if len(target):
        target[-1].tail = (target[-1].tail or "") + text
    else:
        target.text = (target.text or "") + text


def _is_atomic(element) -> bool:
    name = _local(element)
    if name in ATOMIC:
        return True
    if name == "a" and element.get(f"{{{EPUB_NS}}}type", "") in ("noteref", "biblioref"):
        return True
    text = "".join(element.itertext()).strip()
    if name in ("sup", "sub", "a", "span") and not has_letters(text):
        return True
    return len(element) == 0 and not text


def _has_block_child(element) -> bool:
    for node in element.iterdescendants():
        if isinstance(node.tag, str) and _local(node) in BLOCK:
            return True
    return False


def _collect(element, found: list) -> None:
    name = _local(element)
    if name in SKIP or not isinstance(element.tag, str):
        return
    if _has_block_child(element) or name == "body":
        if element.text and has_letters(element.text):
            found.append(TextRun(element, "text"))
        for child in element:
            if isinstance(child.tag, str):
                _collect(child, found)
            if child.tail and has_letters(child.tail):
                found.append(TextRun(child, "tail"))
        return
    if has_letters("".join(element.itertext())):
        found.append(Block(element))


def _make_bilingual(block: Block, original_children: list, original_text) -> None:
    element = block.element
    # Los índices deben seguir teniendo una estructura de navegación válida.
    if any(_local(ancestor) == "nav" for ancestor in element.iterancestors()) or _local(element) == "summary":
        return
    for child in original_children:
        child.attrib.pop("id", None)
        for node in child.iterdescendants():
            if isinstance(node.tag, str):
                node.attrib.pop("id", None)
    style = "opacity:.72;font-size:.92em;margin-top:.25em"
    if _local(element) in INSIDE_ONLY or element.getparent() is None:
        note = etree.Element(f"{{{XHTML}}}div", attrib={"class": "lumbre-original", "style": style})
        note.text = original_text
        for child in original_children:
            note.append(child)
        element.append(note)
        return
    clone = etree.Element(element.tag, attrib=dict(element.attrib), nsmap=element.nsmap)
    clone.attrib.pop("id", None)
    clone.set("class", (clone.get("class", "") + " lumbre-original").strip())
    clone.set("style", (clone.get("style", "") + ";" + style).strip(";"))
    clone.text = original_text
    for child in original_children:
        clone.append(child)
    clone.tail = element.tail
    element.tail = None
    element.addnext(clone)


def analyze(path: Path) -> dict:
    book = Book(path)
    try:
        return _analyze(book)
    finally:
        book.zip.close()


def _analyze(book: Book) -> dict:
    characters = 0
    samples: list[dict] = []
    for name in book.documents:
        root = _parse(book.zip.read(name)).getroot()
        body = root.find(f"{{{XHTML}}}body")
        if body is None:
            body = root
        found: list = []
        _collect(body, found)
        for entry in found:
            text = html.unescape(re.sub(r"<[^>]+>", "", entry.fragment)).strip()
            characters += len(text)
            if text:
                samples.append({"lugar": _where(name), "texto": text})
    return {
        "tipo": "epub", "titulo": book.title, "autor": book.author, "idioma": book.language,
        "unidades": len(book.documents), "unidad": "secciones", "caracteres": characters,
        "avisos": [], "parrafos": samples,
    }


def _where(name: str) -> str:
    return Path(name).stem


def translate(path: Path, output: Path, bilingual_output: Path | None, translator: Translator, target: str) -> dict:
    book = Book(path)
    try:
        return _translate(book, output, bilingual_output, translator, target)
    finally:
        book.zip.close()


def _translate(book: Book, output: Path, bilingual_output: Path | None, translator: Translator, target: str) -> dict:
    documents: dict[str, etree._ElementTree] = {}
    entries: list[tuple] = []
    items: list[Item] = []

    def register(entry, where: str) -> None:
        fragment = normalize(entry.fragment)
        if fragment and has_letters(re.sub(r"<[^>]+>", "", fragment)):
            item = Item(fragment=fragment, where=where)
            entries.append((entry, item))
            items.append(item)

    for name in book.documents:
        tree = _parse(book.zip.read(name))
        documents[name] = tree
        root = tree.getroot()
        body = root.find(f"{{{XHTML}}}body")
        found: list = []
        _collect(body if body is not None else root, found)
        for entry in found:
            register(entry, _where(name))
        head_title = root.find(f"{{{XHTML}}}head/{{{XHTML}}}title")
        if head_title is not None and head_title.text and has_letters(head_title.text):
            register(TextRun(head_title, "text"), _where(name))

    ncx_tree = None
    if book.ncx:
        ncx_tree = _parse(book.zip.read(book.ncx))
        for node in ncx_tree.getroot().iter(f"{{{NCX}}}text"):
            if node.text and has_letters(node.text):
                register(TextRun(node, "text"), "índice")

    opf_root = book.opf.getroot()
    for tag in ("title", "description", "subject"):
        for node in opf_root.iterfind(f".//{{{DC}}}{tag}"):
            if node.text and has_letters(node.text):
                register(TextRun(node, "text"), "metadatos")

    translator.run(items)

    bilingual_jobs: list[tuple] = []
    tag_problems = 0
    for entry, item in entries:
        saved_children = None
        saved_text = None
        if bilingual_output is not None and isinstance(entry, Block):
            saved_children = [copy.deepcopy(child) for child in entry.element]
            saved_text = entry.element.text
        problems = entry.apply(item.result)
        for problem in problems:
            tag_problems += 1
            translator.issues.append({
                "tipo": "formato", "mensaje": problem, "lugar": item.where,
                "original": item.text[:400], "traduccion": re.sub(r"<[^>]+>", "", item.result)[:400],
            })
        if bilingual_output is not None and isinstance(entry, Block):
            bilingual_jobs.append((entry, saved_children, saved_text))

    language_nodes = opf_root.findall(f".//{{{DC}}}language")
    for node in language_nodes:
        node.text = target
    for tree in documents.values():
        root = tree.getroot()
        if root.get("lang") is not None or root.get(XML_LANG) is not None:
            if root.get("lang") is not None:
                root.set("lang", target)
            if root.get(XML_LANG) is not None:
                root.set(XML_LANG, target)

    def write(destination: Path) -> None:
        replaced = {name: _serialize(tree) for name, tree in documents.items()}
        replaced[book.opf_path] = _serialize(book.opf)
        if ncx_tree is not None:
            replaced[book.ncx] = _serialize(ncx_tree)
        with zipfile.ZipFile(destination, "w") as out:
            if "mimetype" in book.zip.namelist():
                out.writestr(zipfile.ZipInfo("mimetype"), book.zip.read("mimetype"), compress_type=zipfile.ZIP_STORED)
            for info in book.zip.infolist():
                if info.filename == "mimetype" or info.is_dir():
                    continue
                out.writestr(info.filename, replaced.get(info.filename, book.zip.read(info.filename)), compress_type=zipfile.ZIP_DEFLATED)

    translator.on_stage("guardando", 0, 1)
    write(output)
    if bilingual_output is not None:
        for entry, children, text in bilingual_jobs:
            _make_bilingual(entry, children, text)
        write(bilingual_output)
    return {"problemas_formato": tag_problems}

"""Crea un ensayo de prueba en EPUB y en PDF."""
import zipfile
from pathlib import Path

import pymupdf as fitz

PARAGRAPHS = [
    "The social contract is not a historical event but a way of asking what authority could be justified to free people. In 1762, Rousseau argued that legitimate power rests on the general will.",
    "Critics replied that consent is rarely explicit. Most citizens never sign anything, and only 3 of the 12 philosophers surveyed here accepted the tacit consent argument without reservation.",
    "This essay therefore treats the social contract as a test. A law passes the test when every affected person could reasonably accept it, whatever their position in society might be.",
]
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000" "1f15c4890000000d49444154789c6360f8cf00000301010018dd8db00000000049454e44ae426082"
)


def _chapter(title: str, chapter_id: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en">
<head><title>{title}</title><link rel="stylesheet" href="estilo.css" type="text/css"/></head>
<body>
<section id="{chapter_id}" epub:type="chapter">
<h1 class="titulo">{title}</h1>
<p class="primero">{PARAGRAPHS[0].replace('social contract', '<em>social contract</em>', 1)}<a epub:type="noteref" href="notas.xhtml#n1" id="r1">1</a></p>
<p>{PARAGRAPHS[1]}&nbsp;<span class="versal">See</span> <a href="https://example.org/fuente">the <b>original</b> source</a>.</p>
<figure><img src="foto.png" alt="diagram"/><figcaption>Figure 1. The general will.</figcaption></figure>
<blockquote><p>{PARAGRAPHS[2]}</p></blockquote>
<ul><li>First premise of the argument</li><li><p>Second premise of the argument</p></li></ul>
<table><tr><th>Author</th><th>Year</th></tr><tr><td>Rousseau</td><td>1762</td></tr></table>
<pre>code stays exactly as written</pre>
</section>
</body>
</html>"""


def make_epub(path: Path) -> Path:
    opf = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">urn:uuid:8f6a7d2e-4f7b-4e33-9f8a-111111111111</dc:identifier>
<dc:title>The Social Contract Today</dc:title><dc:creator>Ana Prueba</dc:creator><dc:language>en</dc:language>
<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>
</metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
<item id="c1" href="cap1.xhtml" media-type="application/xhtml+xml"/>
<item id="c2" href="cap2.xhtml" media-type="application/xhtml+xml"/>
<item id="notas" href="notas.xhtml" media-type="application/xhtml+xml"/>
<item id="css" href="estilo.css" media-type="text/css"/>
<item id="foto" href="foto.png" media-type="image/png"/>
</manifest>
<spine toc="ncx"><itemref idref="c1"/><itemref idref="c2"/><itemref idref="notas"/></spine>
</package>"""
    nav = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en"><head><title>Contents</title></head>
<body><nav epub:type="toc"><h1>Contents</h1><ol><li><a href="cap1.xhtml">The Question of Consent</a></li><li><a href="cap2.xhtml">Reasonable Acceptance</a></li></ol></nav></body></html>"""
    ncx = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="urn:uuid:8f6a7d2e-4f7b-4e33-9f8a-111111111111"/></head>
<docTitle><text>The Social Contract Today</text></docTitle>
<navMap><navPoint id="p1" playOrder="1"><navLabel><text>The Question of Consent</text></navLabel><content src="cap1.xhtml"/></navPoint></navMap></ncx>"""
    notes = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en"><head><title>Notes</title></head>
<body><aside epub:type="footnote" id="n1"><p>See the Discourse on Inequality, written in 1755.</p></aside></body></html>"""
    with zipfile.ZipFile(path, "w") as out:
        out.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        out.writestr("META-INF/container.xml", """<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/libro.opf" media-type="application/oebps-package+xml"/></rootfiles></container>""")
        out.writestr("OEBPS/libro.opf", opf)
        out.writestr("OEBPS/nav.xhtml", nav)
        out.writestr("OEBPS/toc.ncx", ncx)
        out.writestr("OEBPS/cap1.xhtml", _chapter("The Question of Consent", "c1"))
        out.writestr("OEBPS/cap2.xhtml", _chapter("Reasonable Acceptance", "c2"))
        out.writestr("OEBPS/notas.xhtml", notes)
        out.writestr("OEBPS/estilo.css", "body{font-family:serif}.titulo{color:#336}.primero{text-indent:0}")
        out.writestr("OEBPS/foto.png", PNG)
    return path


LOREM = " ".join(PARAGRAPHS)


def make_pdf(path: Path) -> Path:
    document = fitz.open()
    for number in range(1, 4):
        page = document.new_page(width=420, height=595)
        page.insert_textbox(fitz.Rect(50, 26, 370, 42), "THE SOCIAL CONTRACT TODAY", fontsize=8, fontname="helv", align=1, color=(0.4, 0.4, 0.4))
        page.insert_textbox(fitz.Rect(50, 60, 370, 92), f"Chapter {number}: The Question of Consent", fontsize=17, fontname="tibo", color=(0.2, 0.25, 0.45))
        page.draw_rect(fitz.Rect(50, 100, 370, 180), color=(0.55, 0.7, 0.6), fill=(0.84, 0.91, 0.86))
        page.insert_textbox(fitz.Rect(60, 125, 360, 160), "Figure placed as vector art; it must survive.", fontsize=10, fontname="tiit")
        y = 195
        for paragraph in PARAGRAPHS:
            height = 76
            page.insert_textbox(fitz.Rect(50, y, 370, y + height), paragraph, fontsize=10.5, fontname="tiro", lineheight=1.25, align=3)
            y += height + 8
        if number < 3:
            page.insert_textbox(fitz.Rect(50, y, 370, y + 66), "A closing thought runs to the bottom of this page and keeps going, because long arguments do not respect the edge of the paper and the sentence", fontsize=10.5, fontname="tiro", lineheight=1.25)
        page.insert_textbox(fitz.Rect(50, 560, 370, 578), str(number), fontsize=9, fontname="tiro", align=1)
        if number > 1:
            page.insert_textbox(fitz.Rect(50, 500, 370, 540), "continues on this page in lowercase as printed books commonly do.", fontsize=10.5, fontname="tiro")
    document.set_toc([[1, "Chapter One", 1], [1, "Chapter Two", 2]])
    document.set_metadata({"title": "The Social Contract Today", "author": "Ana Prueba"})
    document.save(path)
    document.close()
    return path

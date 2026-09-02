import base64
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from xml.sax.saxutils import escape

import app
from document_formats import (
    DocumentFormatError,
    build_binary_output,
    cache_binary_source,
    delete_binary_source,
    extract_binary_text,
    load_binary_source,
)


def make_docx():
    content_types = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>'''
    relationships = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''
    document = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" mc:Ignorable="w14">
  <w:body>
    <w:p><w:r><w:t xml:space="preserve">Hello </w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>world</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Table cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    <w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>
  </w:body>
</w:document>'''
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/media/image1.png", b"fake-image-data")
    return output.getvalue()


def make_rich_docx():
    """生成手动试译用的多结构 DOCX，不依赖 Word 或第三方包。"""
    def run(text, bold=False, italic=False, code=False):
        properties = []
        if bold:
            properties.append("<w:b/>")
        if italic:
            properties.append("<w:i/>")
        if code:
            properties.extend((
                '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>',
                '<w:shd w:val="clear" w:color="auto" w:fill="F2F4F7"/>',
            ))
        prop_xml = f"<w:rPr>{''.join(properties)}</w:rPr>" if properties else ""
        return (
            f'<w:r>{prop_xml}<w:t xml:space="preserve">'
            f"{escape(text)}</w:t></w:r>"
        )

    def paragraph(inner, style=None, num_id=None, page_break=False):
        properties = []
        if style:
            properties.append(f'<w:pStyle w:val="{style}"/>')
        if num_id is not None:
            properties.append(
                f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num_id}"/></w:numPr>'
            )
        if page_break:
            properties.append('<w:pageBreakBefore/>')
        prop_xml = f"<w:pPr>{''.join(properties)}</w:pPr>" if properties else ""
        return f"<w:p>{prop_xml}{inner}</w:p>"

    def cell(text, width, header=False):
        fill = '<w:shd w:val="clear" w:color="auto" w:fill="E8EEF5"/>' if header else ""
        return (
            f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>'
            f'<w:vAlign w:val="center"/>{fill}</w:tcPr>'
            f'{paragraph(run(text, bold=header))}</w:tc>'
        )

    title = paragraph(run("Northlight Station: DOCX Translation Fixture", bold=True), "FixtureTitle")
    subtitle = paragraph(
        run("A multi-page sample for context, formatting, and container round-trip tests", italic=True),
        "Subtitle",
    )
    body = [title, subtitle]
    body.extend([
        paragraph(run("1. The Station at Northlight"), "Heading1"),
        paragraph(run(
            "Northlight Station stood on a shelf of black volcanic rock where the western sea narrowed into a restless channel. "
            "From the mainland, its tower looked like a pale needle pushed through the horizon, but up close the stone walls were broad, "
            "warm, and marked by a century of salt. Ships relied on its lamp, weather offices relied on its instruments, and the people of "
            "Greyhaven relied on it whenever conversation needed a mystery."
        )),
        paragraph(run(
            "Mara Venn arrived before sunrise carrying a leather tool case and a sealed order from the Ministry. She had repaired telegraph "
            "relays, water pumps, voting machines, and once an automatic tea dispenser that had nearly poisoned a provincial governor. "
            "The order described the present fault in one sentence: THE MERIDIAN ASSEMBLY HAS BEGUN TO DRIFT."
        )),
        paragraph(run(
            "The station keeper, Elias Rowe, met her at the eastern gate. His grey coat had been polished by wind and salt until the cloth "
            "shone at the shoulders. Instead of offering his hand, he looked at Mara's case and asked whether the Ministry had sent the "
            "calibration weights."
        )),
        paragraph(run("“They sent three screwdrivers, two meters of copper wire, and a letter explaining that failure would embarrass the Ministry,” Mara said.")),
        paragraph(run("“I assumed the weights were here.”")),
        paragraph(run("“They were.”")),
        paragraph(run(
            "Mara waited. Elias watched a line of gulls tilt over the channel as if the birds had joined the discussion and required time "
            "to consider his answer. At last he admitted that someone had removed the weights from a locked room three nights earlier. "
            "Nothing else had been touched."
        )),
        paragraph(run("2. Inspection Notes"), "Heading1"),
        paragraph(run(
            "Inside, waste heat from the generator room traveled through pipes in the stone walls. Mara recorded the following observations "
            "before opening the main housing:"
        )),
        paragraph(run("The eastern clock remained exactly seven minutes slow, regardless of the outside temperature."), num_id=1),
        paragraph(run("Every ninth mechanical beat arrived a fraction late and produced a faint vibration in the brass rail."), num_id=1),
        paragraph(run("The generator voltage was stable at 228 V, while the backup circuit reported 227.6 V."), num_id=1),
        paragraph(run("No oil leak, loose cable, scorched contact, or damaged seal was visible around the upper assembly."), num_id=1),
        paragraph(run("A handwritten maintenance card referred to the lower shaft as “the Bell,” although it did not ring."), num_id=1),
        paragraph(run("3. Proposed Repair Procedure"), "Heading1"),
        paragraph(run("Photograph every dial and record its current position before disconnecting power."), num_id=2),
        paragraph(run("Lock the flywheel with the red service pin and verify that the mirrored drum has stopped."), num_id=2),
        paragraph(run("Measure axial drift at three points, then compare the readings with specification NM-42-B."), num_id=2),
        paragraph(run("Install temporary calibration weights only after the lower shaft inspection is complete."), num_id=2),
        paragraph(run("Run a forty-minute observation cycle and document any deviation greater than 0.03 degrees."), num_id=2),
        paragraph(run("4. Terminology Matrix"), "Heading1"),
    ])

    table_rows = [
        ("Source term", "Meaning in this document", "Consistency note"),
        ("Meridian Assembly", "The complete time-comparison mechanism", "Use one stable translated term throughout"),
        ("calibration weights", "Removable reference masses", "Do not shorten this to ordinary weights"),
        ("the Bell", "Traditional name for the lower mechanism", "Preserve capitalization as a proper name"),
        ("drift", "A gradual measurement deviation", "Translate as a technical fault, not physical floating"),
        ("station keeper", "The person responsible for Northlight", "Keep distinct from engineer or lighthouse guard"),
    ]
    rows = []
    for index, values in enumerate(table_rows):
        row_props = "<w:trPr><w:tblHeader/></w:trPr>" if index == 0 else ""
        rows.append(
            f"<w:tr>{row_props}"
            + cell(values[0], 2160, index == 0)
            + cell(values[1], 3600, index == 0)
            + cell(values[2], 3600, index == 0)
            + "</w:tr>"
        )
    body.append(
        '<w:tbl><w:tblPr><w:tblW w:w="9360" w:type="dxa"/>'
        '<w:tblInd w:w="120" w:type="dxa"/><w:tblLayout w:type="fixed"/>'
        '<w:tblBorders><w:top w:val="single" w:sz="4" w:color="AAB4C0"/>'
        '<w:left w:val="single" w:sz="4" w:color="AAB4C0"/>'
        '<w:bottom w:val="single" w:sz="4" w:color="AAB4C0"/>'
        '<w:right w:val="single" w:sz="4" w:color="AAB4C0"/>'
        '<w:insideH w:val="single" w:sz="4" w:color="D6DCE3"/>'
        '<w:insideV w:val="single" w:sz="4" w:color="D6DCE3"/></w:tblBorders>'
        '<w:tblCellMar><w:top w:w="80" w:type="dxa"/><w:left w:w="120" w:type="dxa"/>'
        '<w:bottom w:w="80" w:type="dxa"/><w:right w:w="120" w:type="dxa"/></w:tblCellMar>'
        '</w:tblPr><w:tblGrid><w:gridCol w:w="2160"/><w:gridCol w:w="3600"/>'
        '<w:gridCol w:w="3600"/></w:tblGrid>' + "".join(rows) + "</w:tbl>"
    )
    body.extend([
        paragraph(run("5. Formatting and Preservation Cases"), "Heading1"),
        paragraph(
            run("This paragraph contains ")
            + run("bold text", bold=True)
            + run(", ")
            + run("italic text", italic=True)
            + run(", and the inline identifier ")
            + run("station_clock_offset", code=True)
            + run(". Each span should remain present after translation, even when the translated wording has a different length.")
        ),
        paragraph(
            run("Preserve the placeholder {{station_id}}, the version number v2.4.1, the value 15.75%, and this address: ")
            + '<w:hyperlink r:id="rIdHyperlink"><w:r><w:rPr><w:color w:val="0563C1"/><w:u w:val="single"/></w:rPr>'
              '<w:t>https://example.com/manual?id=42&amp;mode=safe</w:t></w:r></w:hyperlink>'
            + run(".")
        ),
        paragraph(run(
            "A line may contain an em dash, parentheses (including nested remarks), quoted terminology, and a colon: none of these marks "
            "should cause the sentence to be split into the wrong output position."
        )),
        paragraph(run(
            "Important note: the translation must remain complete. It must not omit a sentence merely because the next paragraph changes "
            "speaker, tense, formatting, or subject."
        )),
        paragraph(run("6. Long-Form Continuity Sample"), "Heading1", page_break=True),
        paragraph(run(
            "After the initial inspection, Mara descended the service stairs beneath the lamp room. The brass rail curved around a central "
            "well whose bottom could not be seen. With each circuit the air grew cooler, yet the patient metallic heartbeat became louder. "
            "She counted the beats under her breath and marked every ninth delay in a notebook."
        )),
        paragraph(run(
            "Elias followed several steps behind. He explained that the old keepers had never agreed on whether the lower mechanism measured "
            "time, corrected it, or merely listened to it. Mara dismissed the distinction as folklore until the eastern clock stopped for "
            "exactly seven seconds and then resumed without losing another fraction of a minute."
        )),
        paragraph(run("“When did it begin doing that?” she asked.")),
        paragraph(run("“The winter my mother disappeared,” Elias replied. “Twenty-three years ago.”")),
        paragraph(run(
            "The answer changed the meaning of his earlier silences. Mara no longer heard reluctance alone; she heard someone measuring each "
            "word against a memory he had repeated for decades. She closed the notebook, not because the technical problem had become less "
            "important, but because she understood that the missing weights were part of a longer sequence."
        )),
        paragraph(run(
            "At the lowest landing they found a circular door with no handle. Three shallow sockets were arranged around its edge, each the "
            "size of a calibration weight. Fresh scratches crossed the oldest layer of dust. Whoever had entered the locked room had not "
            "stolen the weights for their metal; they had carried them here and used them as keys."
        )),
        paragraph(run(
            "Mara placed her palm against the door. The delayed ninth beat traveled through the iron and into her wrist. On the other side, "
            "something answered with a rhythm that was almost identical, except that its missing beat came first."
        )),
        paragraph(run("7. Closing Verification"), "Heading1"),
        paragraph(
            run(
                "The translated document should keep the title hierarchy, list numbering, table geometry, page break, header, footer, hyperlink, "
                "footnote, endnote, and inline emphasis. The wording may naturally expand or contract, but every source unit must still occupy "
                "exactly one corresponding translation position."
            )
            + '<w:r><w:footnoteReference w:id="1"/></w:r>'
        ),
        paragraph(
            run(
                "This final paragraph exists to test end-of-document handling and ensure that the last visible sentence is never dropped during "
                "streaming, interruption, resume, manual editing, or complete-file retranslation."
            )
            + '<w:r><w:endnoteReference w:id="1"/></w:r>'
        ),
    ])

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<w:body>' + "".join(body)
        + '<w:sectPr><w:headerReference w:type="default" r:id="rIdHeader"/>'
          '<w:footerReference w:type="default" r:id="rIdFooter"/>'
          '<w:pgSz w:w="12240" w:h="15840"/>'
          '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
          'w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>'
        '</w:body></w:document>'
    ).encode("utf-8")

    styles = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="300" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="120" w:line="300" w:lineRule="auto"/></w:pPr><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="FixtureTitle"><w:name w:val="Fixture Title"/><w:basedOn w:val="Normal"/><w:next w:val="Subtitle"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="0" w:after="240"/></w:pPr><w:rPr><w:rFonts w:ascii="Calibri Light" w:hAnsi="Calibri Light"/><w:b/><w:color w:val="0B2545"/><w:sz w:val="48"/><w:szCs w:val="48"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:pPr><w:spacing w:after="300"/></w:pPr><w:rPr><w:i/><w:color w:val="5A6573"/><w:sz w:val="24"/><w:szCs w:val="24"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="360" w:after="200"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:color w:val="2E74B5"/><w:sz w:val="32"/><w:szCs w:val="32"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="280" w:after="140"/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:color w:val="2E74B5"/><w:sz w:val="26"/><w:szCs w:val="26"/></w:rPr></w:style>
</w:styles>'''
    numbering = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="singleLevel"/><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="&#x2022;"/><w:lvlJc w:val="left"/><w:pPr><w:tabs><w:tab w:val="num" w:pos="540"/></w:tabs><w:ind w:left="540" w:hanging="270"/><w:spacing w:after="80" w:line="300" w:lineRule="auto"/></w:pPr></w:lvl></w:abstractNum>
  <w:abstractNum w:abstractNumId="2"><w:multiLevelType w:val="singleLevel"/><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/><w:lvlJc w:val="left"/><w:pPr><w:tabs><w:tab w:val="num" w:pos="540"/></w:tabs><w:ind w:left="540" w:hanging="270"/><w:spacing w:after="80" w:line="300" w:lineRule="auto"/></w:pPr></w:lvl></w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num><w:num w:numId="2"><w:abstractNumId w:val="2"/></w:num>
</w:numbering>'''
    content_types = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
  <Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>
  <Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
  <Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/>
  <Override PartName="/word/endnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml"/>
</Types>'''
    root_relationships = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'''
    document_relationships = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rIdNumbering" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
  <Relationship Id="rIdHeader" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>
  <Relationship Id="rIdFooter" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>
  <Relationship Id="rIdFootnotes" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/>
  <Relationship Id="rIdEndnotes" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/endnotes" Target="endnotes.xml"/>
  <Relationship Id="rIdHyperlink" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.com/manual?id=42&amp;mode=safe" TargetMode="External"/>
</Relationships>'''
    header = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="4" w:color="D6DCE3"/></w:pBdr></w:pPr><w:r><w:rPr><w:color w:val="6B7280"/><w:sz w:val="18"/></w:rPr><w:t>TRANSLATION BENCH / DOCX FIXTURE</w:t></w:r></w:p></w:hdr>'''
    footer = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:pPr><w:jc w:val="right"/></w:pPr><w:r><w:rPr><w:color w:val="6B7280"/><w:sz w:val="18"/></w:rPr><w:t xml:space="preserve">Sample document  |  Page </w:t></w:r><w:fldSimple w:instr=" PAGE "><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p></w:ftr>'''
    footnotes = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:footnote w:id="-1" w:type="separator"><w:p><w:r><w:separator/></w:r></w:p></w:footnote><w:footnote w:id="1"><w:p><w:r><w:footnoteRef/><w:t xml:space="preserve"> Footnote: verify that notes are translated without being moved into the main body.</w:t></w:r></w:p></w:footnote></w:footnotes>'''
    endnotes = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:endnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:endnote w:id="-1" w:type="separator"><w:p><w:r><w:separator/></w:r></w:p></w:endnote><w:endnote w:id="1"><w:p><w:r><w:endnoteRef/><w:t xml:space="preserve"> Endnote: this is the final extracted DOCX unit and should remain present after round-trip reconstruction.</w:t></w:r></w:p></w:endnote></w:endnotes>'''

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_relationships)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", document_relationships)
        archive.writestr("word/styles.xml", styles)
        archive.writestr("word/numbering.xml", numbering)
        archive.writestr("word/header1.xml", header)
        archive.writestr("word/footer1.xml", footer)
        archive.writestr("word/footnotes.xml", footnotes)
        archive.writestr("word/endnotes.xml", endnotes)
    return output.getvalue()


def make_epub(encrypted=False, line_break=False):
    container = b'''<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>'''
    package = b'''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="book-id">test</dc:identifier><dc:title>Test</dc:title><dc:language>en</dc:language></metadata>
  <manifest>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
    <item id="second" href="second.xhtml" media-type="application/xhtml+xml"/>
    <item id="image" href="image.png" media-type="image/png"/>
  </manifest>
  <spine><itemref idref="chapter"/><itemref idref="second"/></spine>
</package>'''
    chapter = b'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Metadata title</title></head><body>
<!-- preserved comment --><h1>Chapter One</h1><p>The&nbsp;room was <em>quiet</em>.</p><p><img src="image.png" alt="cover"/>A lamp remained lit.</p>
</body></html>'''
    if line_break:
        chapter = chapter.replace(
            b"</p><p><img",
            b"<br/>Still here.</p><p><img",
            1,
        )
    second = b'''<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Second</title></head><body><p>Morning arrived.</p></body></html>'''
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr("OEBPS/content.opf", package, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr("OEBPS/chapter.xhtml", chapter, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr("OEBPS/second.xhtml", second, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr("OEBPS/image.png", b"epub-image-data", compress_type=zipfile.ZIP_STORED)
        if encrypted:
            resource = "OEBPS/chapter.xhtml" if encrypted is True else str(encrypted)
            encryption = f'''<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
<enc:EncryptedData><enc:CipherData><enc:CipherReference URI="{resource}"/></enc:CipherData></enc:EncryptedData>
</encryption>'''.encode("utf-8")
            archive.writestr("META-INF/encryption.xml", encryption)
    return output.getvalue()


class DocumentFormatTests(unittest.TestCase):
    def test_docx_round_trip_preserves_package_and_inline_style(self):
        source = make_docx()

        extracted = extract_binary_text("docx", source)
        translated = build_binary_output("docx", source, "你好世界\n表格内容")

        self.assertEqual(extracted, "Hello world\nTable cell")
        self.assertEqual(extract_binary_text("docx", translated), "你好世界\n表格内容")
        with zipfile.ZipFile(io.BytesIO(translated)) as archive:
            self.assertIsNone(archive.testzip())
            xml = archive.read("word/document.xml").decode("utf-8")
            self.assertIn("<w:b", xml)
            self.assertIn('xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"', xml)
            self.assertIn('mc:Ignorable="w14"', xml)
            self.assertEqual(archive.read("word/media/image1.png"), b"fake-image-data")

    def test_rich_docx_fixture_round_trip_preserves_extended_structure(self):
        source = make_rich_docx()
        extracted = extract_binary_text("docx", source)
        source_units = extracted.split("\n")
        translated_text = "\n".join(
            f"译文单元 {index:03d}" for index in range(1, len(source_units) + 1)
        )

        self.assertGreaterEqual(len(source_units), 40)
        self.assertIn("Northlight Station: DOCX Translation Fixture", extracted)
        self.assertIn("Footnote: verify that notes are translated", extracted)
        self.assertTrue(extracted.endswith(
            "Endnote: this is the final extracted DOCX unit and should remain present after round-trip reconstruction."
        ))

        translated = build_binary_output("docx", source, translated_text)
        self.assertEqual(extract_binary_text("docx", translated), translated_text)
        with zipfile.ZipFile(io.BytesIO(translated)) as archive:
            self.assertIsNone(archive.testzip())
            names = set(archive.namelist())
            self.assertIn("word/styles.xml", names)
            self.assertIn("word/numbering.xml", names)
            self.assertIn("word/header1.xml", names)
            self.assertIn("word/footer1.xml", names)
            relationships = archive.read("word/_rels/document.xml.rels")
            self.assertIn(b"TargetMode=\"External\"", relationships)
            document = archive.read("word/document.xml")
            self.assertIn(b"<w:tbl", document)
            self.assertIn(b"<w:hyperlink", document)
            self.assertIn(b"<w:pageBreakBefore", document)

    def test_epub_round_trip_follows_spine_and_preserves_assets(self):
        source = make_epub()
        expected = "第一章\n房间里很安静。\n一盏灯仍然亮着。\n清晨来临。"

        extracted = extract_binary_text("epub", source)
        translated = build_binary_output("epub", source, expected)

        self.assertEqual(
            extracted,
            "Chapter One\nThe room was quiet.\nA lamp remained lit.\nMorning arrived.",
        )
        self.assertEqual(extract_binary_text("epub", translated), expected)
        with zipfile.ZipFile(io.BytesIO(translated)) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.infolist()[0].filename, "mimetype")
            self.assertEqual(archive.getinfo("mimetype").compress_type, zipfile.ZIP_STORED)
            self.assertEqual(archive.read("OEBPS/image.png"), b"epub-image-data")
            chapter = archive.read("OEBPS/chapter.xhtml").decode("utf-8")
            self.assertIn("<em>", chapter)
            self.assertIn('src="image.png"', chapter)
            self.assertIn("<!DOCTYPE html>", chapter)
            self.assertIn("preserved comment", chapter)

    def test_epub_line_break_is_a_visible_text_boundary(self):
        source = make_epub(line_break=True)
        extracted = extract_binary_text("epub", source)

        self.assertIn("The room was quiet. Still here.", extracted)
        translated = build_binary_output(
            "epub",
            source,
            "第一章\n房间很安静。仍在此处。\n一盏灯仍然亮着。\n清晨来临。",
        )
        with zipfile.ZipFile(io.BytesIO(translated)) as archive:
            chapter = archive.read("OEBPS/chapter.xhtml").decode("utf-8")
            self.assertIn("<br", chapter)

    def test_damaged_non_text_zip_member_is_rejected_before_cache(self):
        source = make_epub()
        damaged = source.replace(b"epub-image-data", b"epub-Xmage-data", 1)
        with tempfile.TemporaryDirectory() as cache_dir:
            with self.assertRaisesRegex(DocumentFormatError, "损坏|无法读取"):
                cache_binary_source(cache_dir, "damaged.epub", damaged)

    def test_encrypted_epub_is_rejected(self):
        with self.assertRaisesRegex(DocumentFormatError, "加密"):
            extract_binary_text("epub", make_epub(encrypted=True))

    def test_epub_with_only_an_encrypted_asset_is_allowed(self):
        extracted = extract_binary_text(
            "epub", make_epub(encrypted="OEBPS/image.png")
        )
        self.assertIn("Chapter One", extracted)

    def test_source_cache_round_trip_and_delete(self):
        source = make_docx()
        with tempfile.TemporaryDirectory() as cache_dir:
            imported = cache_binary_source(cache_dir, "sample.docx", source)

            restored = load_binary_source(
                cache_dir,
                imported["source_id"],
                imported["source_format"],
                imported["source_sha256"],
            )

            self.assertEqual(restored, source)
            self.assertEqual(imported["content"], "Hello world\nTable cell")
            self.assertTrue(delete_binary_source(cache_dir, imported["source_id"]))
            self.assertFalse(os.path.exists(os.path.join(cache_dir, imported["source_id"])))

    def test_translation_count_must_match_container_units(self):
        with self.assertRaisesRegex(DocumentFormatError, "少于"):
            build_binary_output("docx", make_docx(), "只有一个单元")

    def test_xml_control_characters_are_rejected(self):
        with self.assertRaisesRegex(DocumentFormatError, "控制字符"):
            build_binary_output("docx", make_docx(), "非法\x0b字符\n表格")

    def test_app_saves_and_restores_binary_output_preview(self):
        source = make_docx()
        original_paths = (
            app.OUTPUTS_DIR,
            app.OUTPUT_INDEX_PATH,
            app.SOURCE_CACHE_DIR,
        )
        with tempfile.TemporaryDirectory() as root:
            try:
                app.OUTPUTS_DIR = os.path.join(root, "outputs")
                app.OUTPUT_INDEX_PATH = os.path.join(root, "state.json")
                app.SOURCE_CACHE_DIR = os.path.join(root, "sources")
                imported = cache_binary_source(
                    app.SOURCE_CACHE_DIR, "sample.docx", source
                )
                file_info = {"name": "sample.docx", **imported}

                output_name = app.save_translation_output(
                    file_info, "你好世界\n表格内容", "中文"
                )
                output_path = os.path.join(app.OUTPUTS_DIR, output_name)

                self.assertEqual(output_name, "sample.zh.docx")
                self.assertEqual(
                    app.read_output_preview(output_path, output_name),
                    "你好世界\n表格内容",
                )
                self.assertEqual(
                    app._read_output_index()[output_name]["source_sha256"],
                    imported["source_sha256"],
                )
            finally:
                app.OUTPUTS_DIR, app.OUTPUT_INDEX_PATH, app.SOURCE_CACHE_DIR = original_paths

    def test_binary_import_preview_download_and_source_delete_api(self):
        original_paths = (
            app.OUTPUTS_DIR,
            app.OUTPUT_INDEX_PATH,
            app.SOURCE_CACHE_DIR,
        )
        with tempfile.TemporaryDirectory() as root:
            server = None
            thread = None
            try:
                app.OUTPUTS_DIR = os.path.join(root, "outputs")
                app.OUTPUT_INDEX_PATH = os.path.join(root, "state.json")
                app.SOURCE_CACHE_DIR = os.path.join(root, "sources")
                server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_address[1], timeout=5
                )
                payload = json.dumps({
                    "name": "sample.docx",
                    "data_base64": base64.b64encode(make_docx()).decode("ascii"),
                }).encode("utf-8")
                connection.request(
                    "POST", "/api/import", body=payload,
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                imported = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                self.assertEqual(imported["content"], "Hello world\nTable cell")

                output_name = app.save_translation_output(
                    {"name": "sample.docx", **imported},
                    "你好世界\n表格内容",
                    "中文",
                )
                connection.request("GET", "/api/output?name=" + output_name)
                response = connection.getresponse()
                preview = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                self.assertEqual(preview["content"], "你好世界\n表格内容")

                connection.request("GET", "/api/output-file?name=" + output_name)
                response = connection.getresponse()
                downloaded = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(extract_binary_text("docx", downloaded), "你好世界\n表格内容")

                connection.request(
                    "DELETE", "/api/source?id=" + imported["source_id"]
                )
                response = connection.getresponse()
                deleted = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                self.assertTrue(deleted["deleted"])
                connection.close()
            finally:
                if server is not None:
                    server.shutdown()
                    server.server_close()
                if thread is not None:
                    thread.join(timeout=5)
                app.OUTPUTS_DIR, app.OUTPUT_INDEX_PATH, app.SOURCE_CACHE_DIR = original_paths

    def test_translate_api_rejects_duplicate_names_and_concurrent_job(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=5
        )
        try:
            duplicate_payload = json.dumps({
                "config": {},
                "files": [
                    {"name": "same.txt", "content": "One"},
                    {"name": "SAME.txt", "content": "Two"},
                ],
            })
            connection.request(
                "POST",
                "/api/translate",
                body=duplicate_payload,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 400)

            with app.JOBS_LOCK:
                app.JOBS["already-running"] = {"status": "running"}
            payload = json.dumps({
                "config": {},
                "files": [{"name": "new.txt", "content": "Source"}],
            })
            connection.request(
                "POST",
                "/api/translate",
                body=payload,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 409)
        finally:
            with app.JOBS_LOCK:
                app.JOBS.pop("already-running", None)
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

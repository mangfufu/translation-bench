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
import document_formats
from document_formats import (
    DocumentFormatError,
    build_binary_output,
    cache_binary_source,
    delete_binary_source,
    extract_binary_text,
    format_pdf_page_selection,
    load_binary_source,
    normalize_pdf_page_selection,
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


def make_nested_paragraph_docx():
    """生成包含文本框式嵌套段落的最小 DOCX。"""
    source = make_docx()
    old_paragraph = (
        b'<w:p><w:r><w:t xml:space="preserve">Hello </w:t></w:r>'
        b'<w:r><w:rPr><w:b/></w:rPr><w:t>world</w:t></w:r></w:p>'
    )
    nested_paragraph = (
        b'<w:p><w:r><w:t>Outer text.</w:t></w:r><w:txbxContent>'
        b'<w:p><w:r><w:t>Inner text.</w:t></w:r></w:p>'
        b'</w:txbxContent></w:p>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source), "r") as archive:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
            for info in archive.infolist():
                payload = archive.read(info.filename)
                if info.filename == "word/document.xml":
                    payload = payload.replace(old_paragraph, nested_paragraph, 1)
                target.writestr(info, payload)
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


def make_epub(encrypted=False, line_break=False, mixed_blocks=False):
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
    if mixed_blocks:
        chapter = b'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Metadata title</title></head><body>
<div>Intro <em>direct</em> text.<p>Nested paragraph.</p>Trailing <strong>direct text.</strong></div>
<ul><li>List prefix.<p>Nested list paragraph.</p>List suffix.</li></ul>
</body></html>'''
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


def make_pdf(text_layer=True, encrypted=False):
    """生成三页、带真实文字层的 PDF 手动试译样本。"""
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen.canvas import Canvas

    output = io.BytesIO()
    pdf = Canvas(output, pagesize=letter, invariant=1, pageCompression=1)
    pdf.setCreator("Translation Bench Test Suite")
    pdf.setTitle("PDF Text Layer Translation Fixture")

    if not text_layer:
        pdf.setStrokeColor(HexColor("#243447"))
        pdf.setFillColor(HexColor("#E7EDF3"))
        pdf.rect(72, 220, 468, 350, fill=1, stroke=1)
        pdf.circle(306, 395, 96, fill=0, stroke=1)
        pdf.showPage()
        pdf.save()
    else:
        def line(text, y, size=11, x=72, bold=False):
            pdf.setFont("Helvetica-Bold" if bold else "Helvetica", size)
            pdf.setFillColor(HexColor("#111111"))
            pdf.drawString(x, y, text)

        line("The Glass Meridian", 720, 22, bold=True)
        line("PDF TEXT-LAYER TRANSLATION FIXTURE", 690, 9, bold=True)
        line("Northlight Station stood on a shelf of black volcanic rock where the", 650)
        line("western sea narrowed into a channel. From the mainland, its tower", 635)
        line("looked like a pale needle pushed through the horizon.", 620)
        line("Ships used its lamp, weather offices used its instruments, and the", 585)
        line("people of Greyhaven used it whenever conversation failed.", 570)
        line('"Did they send you the calibration weights?" Mara asked.', 530)
        line('"They were here yesterday," Elias replied.', 510)
        line('"Were?" she prompted.', 490)
        line("A brass rail spiraled upward around the central machinery well. Far", 450)
        line("below, something struck at a steady interval: not quite a bell, not", 435)
        line("quite a hammer, but a patient metallic heartbeat.", 420)
        line("The page also contains 15.75%, v2.4.1, {{station_id}}, and", 380)
        line("https://example.com/manual?id=42&mode=safe for preservation tests.", 365)
        pdf.showPage()

        line("Inspection Notes", 720, 18, bold=True)
        line("- Confirm that every extracted item remains a separate translation unit.", 680)
        line("- Keep URLs, numbers, version strings, and placeholders unchanged.", 655)
        line("- Preserve page order even when translated text grows longer.", 630)
        line("The source includes a compact table-like region below. The translator", 585)
        line("should preserve these vector cell borders when coordinate mapping is safe,", 570)
        line("and use a clean reflow only when the translated text cannot fit.", 555)
        pdf.setStrokeColor(HexColor("#5B6773"))
        for x in (72, 210, 360, 540):
            pdf.line(x, 365, x, 505)
        for y in (365, 400, 435, 470, 505):
            pdf.line(72, y, 540, y)
        line("Term", 484, 9, 82, True)
        line("Meaning", 484, 9, 220, True)
        line("Required handling", 484, 9, 370, True)
        line("Bell", 449, 9, 82)
        line("mechanical signal", 449, 9, 220)
        line("translate consistently", 449, 9, 370)
        line("Keeper", 414, 9, 82)
        line("station operator", 414, 9, 220)
        line("retain role context", 414, 9, 370)
        line("Drift", 379, 9, 82)
        line("timing deviation", 379, 9, 220)
        line("do not summarize", 379, 9, 370)
        line("A vector diagram is present to verify that non-text artwork survives", 325)
        line("the layout-preserving translation path.", 310)
        pdf.setStrokeColor(HexColor("#243447"))
        pdf.circle(175, 210, 46, fill=0, stroke=1)
        pdf.line(221, 210, 410, 210)
        pdf.line(380, 230, 410, 210)
        pdf.line(380, 190, 410, 210)
        pdf.showPage()

        line("Long-form Continuity Sample", 720, 18, bold=True)
        paragraphs = [
            (
                "At dawn on the third day, Mara returned to the lower gallery with a "
                "new notebook and no expectation of finding an easy answer. The delayed "
                "ninth beat had become stronger overnight, yet every gauge insisted that "
                "the assembly was operating within its legal tolerance."
            ),
            (
                "Elias placed the missing calibration case on the workbench without "
                "explaining where it had been found. Its brass corners were wet with salt "
                "water, while the paper inventory inside remained perfectly dry. Mara "
                "recorded both facts before asking the question he clearly hoped to avoid."
            ),
            (
                '"If the room was locked, who carried this outside?" she asked. Elias '
                'looked toward the shaft and answered, "That depends on whether you believe '
                'the station keeps time, or whether time keeps the station."'
            ),
            (
                "The sentence sounded theatrical, but the machinery below them answered "
                "with three quick impacts and a long silence. For the first time since her "
                "arrival, the eastern clock advanced by exactly one minute."
            ),
            (
                "This final paragraph is deliberately long enough to wrap across several "
                "visual lines. The importer should reconstruct it as a coherent unit, the "
                "translator should receive neighboring context according to the selected "
                "mode, and the generated PDF should add pages instead of clipping text at "
                "the bottom margin."
            ),
        ]
        y = 680
        for paragraph in paragraphs:
            words = paragraph.split()
            current = ""
            for word in words:
                candidate = (current + " " + word).strip()
                if pdf.stringWidth(candidate, "Helvetica", 11) > 468 and current:
                    line(current, y)
                    y -= 15
                    current = word
                else:
                    current = candidate
            if current:
                line(current, y)
                y -= 15
            y -= 18
        pdf.save()

    raw = output.getvalue()
    if not encrypted:
        return raw
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(io.BytesIO(raw))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt("translation-bench-test")
    protected = io.BytesIO()
    writer.write(protected)
    return protected.getvalue()


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

    def test_docx_nested_textbox_paragraph_is_not_duplicated(self):
        source = make_nested_paragraph_docx()

        self.assertEqual(
            extract_binary_text("docx", source),
            "Outer text.\nInner text.\nTable cell",
        )
        translated = build_binary_output(
            "docx",
            source,
            "外层文字。\n内层文字。\n表格内容",
        )
        self.assertEqual(
            extract_binary_text("docx", translated),
            "外层文字。\n内层文字。\n表格内容",
        )

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

    def test_epub_mixed_parent_and_nested_block_text_round_trips(self):
        source = make_epub(mixed_blocks=True)
        extracted = extract_binary_text("epub", source)
        expected_source = (
            "Intro direct text.\nNested paragraph.\nTrailing direct text.\n"
            "List prefix.\nNested list paragraph.\nList suffix.\nMorning arrived."
        )
        translated_text = (
            "开头直接文字。\n嵌套段落。\n结尾直接文字。\n"
            "列表前缀。\n嵌套列表段落。\n列表后缀。\n清晨来临。"
        )

        self.assertEqual(extracted, expected_source)
        translated = build_binary_output("epub", source, translated_text)
        self.assertEqual(extract_binary_text("epub", translated), translated_text)
        with zipfile.ZipFile(io.BytesIO(translated)) as archive:
            chapter = archive.read("OEBPS/chapter.xhtml").decode("utf-8")
            self.assertIn("<em", chapter)
            self.assertIn("<strong", chapter)

    def test_pdf_text_layer_extracts_and_preserves_safe_layout(self):
        from pypdf import PdfReader
        from pypdf.generic import ContentStream

        source = make_pdf()
        extracted = extract_binary_text("pdf", source)
        source_units = extracted.split("\n")
        translated_text = "\n".join(
            f"Stable translated unit {index:03d}."
            for index in range(1, len(source_units) + 1)
        )

        self.assertGreaterEqual(len(source_units), 20)
        self.assertIn("The Glass Meridian", extracted)
        self.assertIn('"Were?" she prompted.', extracted)
        self.assertIn("https://example.com/manual?id=42&mode=safe", extracted)
        self.assertIn("Long-form Continuity Sample", extracted)

        translated = build_binary_output(
            "pdf",
            source,
            translated_text,
            document_title="sample.en.pdf",
            target_language="英文",
        )
        reader = PdfReader(io.BytesIO(translated))
        source_reader = PdfReader(io.BytesIO(source))
        self.assertEqual(len(reader.pages), 3)
        self.assertEqual(reader.metadata.title, "sample.en.pdf")
        self.assertEqual(
            reader.metadata.subject,
            "Layout-preserving translation generated from a text-layer PDF",
        )
        for source_page, translated_page in zip(source_reader.pages, reader.pages):
            self.assertEqual(source_page.mediabox, translated_page.mediabox)
            self.assertEqual(
                int(source_page.get("/Rotate", 0) or 0),
                int(translated_page.get("/Rotate", 0) or 0),
            )

        visible = "".join(page.extract_text() or "" for page in reader.pages)
        self.assertNotIn("The Glass Meridian", visible)
        self.assertEqual(extract_binary_text("pdf", translated), translated_text)

        # 表格和示意图的矢量线条保留，只移除原始文字显示指令。
        source_ops = ContentStream(
            source_reader.pages[1].get_contents(), source_reader
        ).operations
        translated_ops = ContentStream(
            reader.pages[1].get_contents(), reader
        ).operations
        source_lines = sum(operator == b"l" for _, operator in source_ops)
        translated_lines = sum(operator == b"l" for _, operator in translated_ops)
        self.assertGreaterEqual(source_lines, 10)
        self.assertGreaterEqual(translated_lines, source_lines)

        coordinates = []

        def capture(text, _cm, tm, _font, _size):
            if "Stable translated unit 001." in str(text or ""):
                coordinates.append((float(tm[4]), float(tm[5])))

        reader.pages[0].extract_text(visitor_text=capture)
        self.assertTrue(coordinates)
        self.assertAlmostEqual(coordinates[0][0], 72.0, delta=1.0)
        self.assertAlmostEqual(coordinates[0][1], 720.0, delta=2.0)

    def test_pdf_page_range_extracts_only_selected_pages_and_preserves_others(self):
        from pypdf import PdfReader

        source = make_pdf()
        source_reader = PdfReader(io.BytesIO(source))
        selected = extract_binary_text("pdf", source, 2, 2)
        translations = "\n".join(
            f"Selected page translation {index}."
            for index, _ in enumerate(selected.split("\n"), 1)
        )

        output = build_binary_output(
            "pdf",
            source,
            translations,
            target_language="英文",
            pdf_page_start=2,
            pdf_page_end=2,
        )
        output_reader = PdfReader(io.BytesIO(output))

        self.assertEqual(len(output_reader.pages), 3)
        self.assertEqual(
            output_reader.pages[0].extract_text(),
            source_reader.pages[0].extract_text(),
        )
        self.assertEqual(
            output_reader.pages[2].extract_text(),
            source_reader.pages[2].extract_text(),
        )
        second_page = output_reader.pages[1].extract_text() or ""
        self.assertIn("Selected page translation 1.", second_page)
        self.assertNotIn("Inspection Notes", second_page)

    def test_pdf_sparse_page_selection_skips_middle_page_without_modifying_it(self):
        from pypdf import PdfReader

        source = make_pdf()
        source_reader = PdfReader(io.BytesIO(source))
        selected = extract_binary_text(
            "pdf", source, pdf_page_selection="1,3"
        )
        self.assertIn("The Glass Meridian", selected)
        self.assertIn("Long-form Continuity Sample", selected)
        self.assertNotIn("Inspection Notes", selected)
        translations = "\n".join(
            f"Sparse translation {index}."
            for index, _ in enumerate(selected.split("\n"), 1)
        )

        output = build_binary_output(
            "pdf",
            source,
            translations,
            target_language="英文",
            pdf_page_selection="1,3",
        )
        output_reader = PdfReader(io.BytesIO(output))

        self.assertEqual(len(output_reader.pages), 3)
        self.assertIn(
            "Sparse translation 1.", output_reader.pages[0].extract_text() or ""
        )
        self.assertEqual(
            output_reader.pages[1].extract_text(),
            source_reader.pages[1].extract_text(),
        )
        self.assertIn(
            "Sparse translation", output_reader.pages[2].extract_text() or ""
        )

    def test_pdf_page_selection_is_normalized_and_strictly_validated(self):
        self.assertEqual(
            normalize_pdf_page_selection(10, "1,3-5,8"),
            (1, 3, 4, 5, 8),
        )
        self.assertEqual(
            format_pdf_page_selection((1, 3, 4, 5, 8), 10),
            "1,3-5,8",
        )
        self.assertEqual(format_pdf_page_selection(range(1, 11), 10), "all")
        for selection in ([], "1,,3", "0,2", "2-1", "1,11", "x"):
            with self.subTest(selection=selection):
                with self.assertRaises(DocumentFormatError):
                    normalize_pdf_page_selection(10, selection)

    def test_pdf_page_range_is_strictly_validated(self):
        source = make_pdf()

        with self.assertRaisesRegex(DocumentFormatError, "1–3"):
            extract_binary_text("pdf", source, 0, 2)
        with self.assertRaisesRegex(DocumentFormatError, "不能大于"):
            extract_binary_text("pdf", source, 3, 2)
        with self.assertRaisesRegex(DocumentFormatError, "必须是整数"):
            extract_binary_text("pdf", source, "1.5", 2)

    def test_pdf_cjk_output_embeds_a_covering_system_font(self):
        source = make_pdf()
        source_units = extract_binary_text("pdf", source).split("\n")
        translations = "\n".join("稳定译文。" for _ in source_units)

        try:
            output = build_binary_output(
                "pdf",
                source,
                translations,
                target_language="中文",
            )
        except DocumentFormatError as exc:
            if "CJK 字体" in str(exc) or "Noto Sans CJK" in str(exc):
                self.skipTest("当前测试系统未安装覆盖中文的 CJK 字体")
            raise
        self.assertTrue(b"/FontFile2" in output or b"/FontFile3" in output)
        self.assertEqual(extract_binary_text("pdf", output), translations)

    def test_pdf_refuses_unverifiable_cid_font_fallback(self):
        source = make_pdf()
        source_units = extract_binary_text("pdf", source).split("\n")
        old_candidates = document_formats._pdf_cjk_font_path_candidates
        try:
            document_formats._pdf_cjk_font_path_candidates = lambda _script: []
            with self.assertRaisesRegex(DocumentFormatError, "CJK 字体|Noto"):
                build_binary_output(
                    "pdf",
                    source,
                    "\n".join("稳定译文❤️😀" for _ in source_units),
                    target_language="中文",
                )
        finally:
            document_formats._pdf_cjk_font_path_candidates = old_candidates

    def test_pdf_line_direction_uses_first_strong_character(self):
        direction = document_formats._pdf_line_direction

        self.assertEqual(direction("Version 2 مثال", "阿拉伯文"), "LTR")
        self.assertEqual(direction("https://example.com مثال", "阿拉伯文"), "LTR")
        self.assertEqual(direction("مثال https://example.com", "英文"), "RTL")
        self.assertEqual(direction("2026/09/02", "英文"), "LTR")
        self.assertEqual(direction("2026/09/02", "阿拉伯文"), "RTL")

    def test_pdf_shaping_detects_thai_tibetan_and_mongolian(self):
        shaping = document_formats._pdf_contains_shaping_script

        self.assertTrue(shaping("ภาษาไทย"))
        self.assertTrue(shaping("བོད་ཡིག"))
        self.assertTrue(shaping("ᠮᠣᠩᠭᠣᠯ"))
        self.assertFalse(shaping("Plain English 2026"))

    def test_pdf_without_text_layer_and_encrypted_pdf_are_rejected(self):
        with self.assertRaisesRegex(DocumentFormatError, "文字层|OCR"):
            extract_binary_text("pdf", make_pdf(text_layer=False))
        with self.assertRaisesRegex(DocumentFormatError, "加密"):
            extract_binary_text("pdf", make_pdf(encrypted=True))

    def test_pdf_content_stream_limits_run_before_text_extraction(self):
        from pypdf import PdfReader

        source = make_pdf()
        reader = PdfReader(io.BytesIO(source))
        first_size = len(reader.pages[0].get_contents().get_data())
        old_page_limit = document_formats.MAX_PDF_PAGE_CONTENT_BYTES
        old_total_limit = document_formats.MAX_PDF_TOTAL_CONTENT_BYTES
        try:
            document_formats.MAX_PDF_PAGE_CONTENT_BYTES = first_size - 1
            with self.assertRaisesRegex(DocumentFormatError, "单页解压内容流"):
                extract_binary_text("pdf", source)

            document_formats.MAX_PDF_PAGE_CONTENT_BYTES = old_page_limit
            document_formats.MAX_PDF_TOTAL_CONTENT_BYTES = 1
            with self.assertRaisesRegex(DocumentFormatError, "合计"):
                extract_binary_text("pdf", source)
        finally:
            document_formats.MAX_PDF_PAGE_CONTENT_BYTES = old_page_limit
            document_formats.MAX_PDF_TOTAL_CONTENT_BYTES = old_total_limit

    def test_pdf_parser_limits_are_temporarily_tightened_and_restored(self):
        import pypdf.filters as pdf_filters

        names = (
            "FLATE_MAX_BUFFER_SIZE",
            "JBIG2_MAX_OUTPUT_LENGTH",
            "LZW_MAX_OUTPUT_LENGTH",
            "RUN_LENGTH_MAX_OUTPUT_LENGTH",
            "ZLIB_MAX_OUTPUT_LENGTH",
        )
        previous = {name: getattr(pdf_filters, name) for name in names}
        declared_stream_limit = getattr(
            pdf_filters, "MAX_DECLARED_STREAM_LENGTH", None
        )
        with document_formats._pdf_decode_limits():
            for name in names:
                self.assertLessEqual(
                    getattr(pdf_filters, name),
                    document_formats.MAX_PDF_PAGE_CONTENT_BYTES + 1,
                )
            self.assertEqual(
                getattr(pdf_filters, "MAX_DECLARED_STREAM_LENGTH", None),
                declared_stream_limit,
            )
        self.assertEqual(
            {name: getattr(pdf_filters, name) for name in names},
            previous,
        )

    def test_pdf_toc_rows_discard_broken_leader_glyphs(self):
        rows = document_formats._pdf_probable_toc_rows(
            "CONTENTS\n"
            "Introduction \ufffd\ufffd\ufffd\ufffd    5\n"
            "Rules \x08\x08\x08\x08    6\n"
            "Investigators             10\n"
            "Reference                 25\n"
            "Credits                   34\n"
        )

        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[1], {"title": "Introduction", "page_label": "5"})
        self.assertEqual(rows[-1], {"title": "Credits", "page_label": "34"})
        self.assertNotIn("\ufffd", "".join(row["title"] for row in rows))
        self.assertNotIn("\x08", "".join(row["title"] for row in rows))

    def test_pdf_toc_uses_line_units_and_rebuilds_leaders(self):
        from reportlab.pdfgen.canvas import Canvas
        from pypdf import PdfReader

        source_buffer = io.BytesIO()
        pdf = Canvas(source_buffer, invariant=1)
        pdf.setFont("Helvetica-Bold", 28)
        pdf.drawCentredString(306, 730, "CONTENTS")
        for index, title in enumerate(
            ("Introduction", "Rules", "Investigators", "Reference", "Credits"),
            5,
        ):
            y = 680 - (index - 5) * 28
            pdf.setFont("Helvetica", 11)
            pdf.drawString(72, y, title)
            pdf.drawRightString(540, y, str(index))
        pdf.save()
        source = source_buffer.getvalue()

        page = document_formats._pdf_document_units(source)[0]
        self.assertEqual(page.get("geometry_source"), "pdfplumber")
        self.assertEqual(len(page["units"]), 6)
        self.assertEqual(
            [unit.get("toc_page_label") for unit in page["units"]],
            ["", "5", "6", "7", "8", "9"],
        )
        translations = "\n".join((
            "Translated contents",
            "Translated introduction    5",
            "Translated rules    6",
            "Translated investigators    7",
            "Translated reference    8",
            "Translated credits    9",
        ))
        output = build_binary_output(
            "pdf", source, translations, target_language="英文"
        )
        visible = PdfReader(io.BytesIO(output)).pages[0].extract_text() or ""

        self.assertNotIn("Introduction", visible)
        self.assertIn("Translated introduction", visible)
        self.assertIn("5", visible)
        self.assertNotIn("\ufffd", visible)

    def test_pdf_strict_layout_shrinks_long_unit_without_adding_pages(self):
        from pypdf import PdfReader

        source = make_pdf()
        source_units = extract_binary_text("pdf", source).split("\n")
        translations = [
            f"Normal translation {index}." for index in range(len(source_units))
        ]
        translations[0] = (
            "A long translated unit must shrink inside its original box. " * 150
            + "END MARKER."
        )

        output = build_binary_output(
            "pdf",
            source,
            "\n".join(translations),
            target_language="英文",
        )
        reader = PdfReader(io.BytesIO(output))
        visible = "".join(page.extract_text() or "" for page in reader.pages)
        rendered_sizes = []

        def capture_size(text, _cm, _tm, _font, font_size):
            if "A long translated unit" in str(text or ""):
                rendered_sizes.append(float(font_size))

        reader.pages[0].extract_text(visitor_text=capture_size)

        self.assertEqual(len(reader.pages), 3)
        self.assertEqual(
            reader.metadata.subject,
            "Layout-preserving translation generated from a text-layer PDF",
        )
        self.assertIn("END MARKER.", visible)
        self.assertIn("Normal translation 32.", visible)
        self.assertTrue(rendered_sizes)
        self.assertLess(min(rendered_sizes), 5.5)

    def test_pdf_strict_layout_refuses_to_reflow_when_text_cannot_fit(self):
        source = make_pdf()
        source_units = extract_binary_text("pdf", source).split("\n")
        translations = ["Normal translation." for _ in source_units]
        translations[0] = "Unreasonably long translation. " * 900

        with self.assertRaisesRegex(DocumentFormatError, "第 1 页.*不会重排"):
            build_binary_output(
                "pdf",
                source,
                "\n".join(translations),
                target_language="英文",
            )

    def test_pdf_unmapped_form_text_is_preserved_without_reflowing_the_page(self):
        from reportlab.pdfgen.canvas import Canvas
        from pypdf import PdfReader

        source_buffer = io.BytesIO()
        pdf = Canvas(source_buffer, invariant=1)
        pdf.setFont("Helvetica", 12)
        pdf.drawString(72, 720, "Direct page text.")
        pdf.beginForm("label")
        pdf.setFont("Helvetica", 12)
        pdf.drawString(0, 0, "Text inside a form object.")
        pdf.endForm()
        pdf.doForm("label")
        pdf.save()

        output = build_binary_output(
            "pdf",
            source_buffer.getvalue(),
            "Translated direct text.",
            target_language="英文",
        )
        reader = PdfReader(io.BytesIO(output))
        self.assertEqual(
            reader.metadata.subject,
            "Layout-preserving translation generated from a text-layer PDF",
        )
        visible = "".join(page.extract_text() or "" for page in reader.pages)
        self.assertIn("Translated direct text.", visible)
        self.assertIn("Text inside a form object.", visible)

    def test_pdf_page_range_does_not_modify_shared_forms_on_other_pages(self):
        from reportlab.pdfgen.canvas import Canvas
        from pypdf import PdfReader

        source_buffer = io.BytesIO()
        pdf = Canvas(source_buffer, invariant=1)
        pdf.beginForm("shared")
        pdf.setFont("Helvetica", 12)
        pdf.drawString(0, 0, "Shared form label.")
        pdf.endForm()
        pdf.setFont("Helvetica", 12)
        pdf.drawString(72, 720, "First page body.")
        pdf.saveState()
        pdf.translate(72, 680)
        pdf.doForm("shared")
        pdf.restoreState()
        pdf.showPage()
        pdf.setFont("Helvetica", 12)
        pdf.drawString(72, 720, "Second page body.")
        pdf.saveState()
        pdf.translate(72, 680)
        pdf.doForm("shared")
        pdf.restoreState()
        pdf.save()
        source = source_buffer.getvalue()
        selected = extract_binary_text("pdf", source, 1, 1)
        translations = "\n".join(
            f"Selected translation {index}."
            for index, _ in enumerate(selected.split("\n"), 1)
        )

        output = build_binary_output(
            "pdf",
            source,
            translations,
            target_language="英文",
            pdf_page_start=1,
            pdf_page_end=1,
        )
        reader = PdfReader(io.BytesIO(output))
        first = reader.pages[0].extract_text() or ""
        second = reader.pages[1].extract_text() or ""

        self.assertNotIn("First page body.", first)
        self.assertIn("Selected translation 1.", first)
        self.assertIn("Second page body.", second)
        self.assertIn("Shared form label.", second)

    def test_pdf_invisible_ocr_text_requires_disabling_strict_layout(self):
        from reportlab.pdfgen.canvas import Canvas
        from pypdf import PdfReader

        source_buffer = io.BytesIO()
        pdf = Canvas(source_buffer, invariant=1)
        text = pdf.beginText(72, 720)
        text.setFont("Helvetica", 12)
        text.setTextRenderMode(3)
        text.textLine("Invisible OCR text.")
        pdf.drawText(text)
        pdf.save()

        self.assertEqual(
            extract_binary_text("pdf", source_buffer.getvalue()),
            "Invisible OCR text.",
        )
        with self.assertRaisesRegex(DocumentFormatError, "第 1 页.*不会重排"):
            build_binary_output(
                "pdf",
                source_buffer.getvalue(),
                "Visible translated text.",
                target_language="英文",
            )
        output = build_binary_output(
            "pdf", source_buffer.getvalue(), "Visible translated text.",
            target_language="英文", pdf_strict_layout=False,
        )
        reader = PdfReader(io.BytesIO(output))
        self.assertEqual(
            reader.metadata.subject,
            "Reflowed translation generated from a text-layer PDF",
        )
        self.assertEqual(reader.pages[0].extract_text().strip(), "Visible translated text.")

    def test_pdf_clip_only_art_page_is_preserved_and_not_sent_for_translation(self):
        from reportlab.pdfgen.canvas import Canvas
        from pypdf import PdfReader

        source_buffer = io.BytesIO()
        pdf = Canvas(source_buffer, pagesize=(600, 400), invariant=1)
        art = pdf.beginText(330, 240)
        art.setFont("Helvetica-Bold", 42)
        art.setTextRenderMode(7)
        art.textLine("C A L L")
        pdf.drawText(art)
        pdf.setFillColorRGB(0.15, 0.45, 0.75)
        pdf.rect(300, 180, 260, 120, fill=1, stroke=0)
        pdf.showPage()
        pdf.setFont("Helvetica", 12)
        pdf.drawString(72, 320, "Ordinary body text.")
        pdf.save()
        source = source_buffer.getvalue()

        self.assertEqual(extract_binary_text("pdf", source), "Ordinary body text.")
        output = build_binary_output(
            "pdf", source, "Translated ordinary body text.", target_language="英文"
        )
        source_reader = PdfReader(io.BytesIO(source))
        output_reader = PdfReader(io.BytesIO(output))
        self.assertEqual(len(output_reader.pages), 2)
        self.assertEqual(
            output_reader.pages[0].get_contents().get_data(),
            source_reader.pages[0].get_contents().get_data(),
        )
        self.assertIn(
            "Translated ordinary body text.", output_reader.pages[1].extract_text()
        )

    def test_pdf_box_order_resets_columns_after_a_vertical_panel_gap(self):
        def box(text, x0, x1, top, bottom):
            return {
                "text": text,
                "x0": x0,
                "x1": x1,
                "top": top,
                "bottom": bottom,
            }

        boxes = [
            box("Lower right body", 312, 530, 460, 540),
            box("Upper right continuation", 312, 530, 150, 240),
            box("Upper left opening", 72, 290, 150, 204),
            box("Lower left dice rules", 72, 290, 400, 540),
            box("Upper left story", 72, 290, 219, 340),
            box("Full-width heading", 165, 435, 100, 130),
            box("Upper right ending", 312, 530, 245, 340),
            box("Lower right heading", 354, 486, 430, 450),
        ]

        ordered = document_formats._pdfplumber_order_boxes(boxes, 600, 800)
        self.assertEqual(
            [entry["text"] for entry in ordered],
            [
                "Full-width heading",
                "Upper left opening",
                "Upper left story",
                "Upper right continuation",
                "Upper right ending",
                "Lower left dice rules",
                "Lower right heading",
                "Lower right body",
            ],
        )
        units = [
            {
                "text": entry["text"],
                "heading": entry["text"] == "Full-width heading",
                "reading": entry["_translation_reading"],
                "box": {"font_size": 10.0},
            }
            for entry in ordered
        ]
        hints = document_formats._pdf_continuation_hints([
            {"page_number": 1, "units": units}
        ])
        self.assertEqual(hints[3], "strong")
        self.assertEqual(hints[5], "separate")

    def test_pdf_cropbox_excludes_hidden_spread_half_and_keeps_original_geometry(self):
        from reportlab.pdfgen.canvas import Canvas
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import RectangleObject

        source_buffer = io.BytesIO()
        pdf = Canvas(source_buffer, pagesize=(600, 400), invariant=1)
        pdf.setFont("Helvetica", 12)
        pdf.drawString(50, 320, "Hidden back-cover paragraph.")
        pdf.drawString(350, 250, "Visible cover subtitle.")
        pdf.save()

        reader = PdfReader(io.BytesIO(source_buffer.getvalue()))
        reader.pages[0].cropbox = RectangleObject((300, 0, 600, 400))
        writer = PdfWriter()
        writer.add_page(reader.pages[0])
        cropped_buffer = io.BytesIO()
        writer.write(cropped_buffer)
        source = cropped_buffer.getvalue()

        self.assertEqual(extract_binary_text("pdf", source), "Visible cover subtitle.")
        output = build_binary_output(
            "pdf", source, "Translated visible subtitle.", target_language="英文"
        )
        output_page = PdfReader(io.BytesIO(output)).pages[0]
        self.assertEqual(tuple(output_page.mediabox), (0, 0, 600, 400))
        self.assertEqual(tuple(output_page.cropbox), (300, 0, 600, 400))
        extracted = output_page.extract_text()
        self.assertIn("Translated visible subtitle.", extracted)
        self.assertNotIn("Hidden back-cover paragraph.", extracted)

    def test_pdf_safe_layout_preserves_images_and_links(self):
        from PIL import Image
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen.canvas import Canvas
        from pypdf import PdfReader

        image_buffer = io.BytesIO()
        Image.new("RGB", (8, 8), (38, 106, 178)).save(image_buffer, format="PNG")
        image_buffer.seek(0)

        source_buffer = io.BytesIO()
        pdf = Canvas(source_buffer, invariant=1)
        pdf.setFont("Helvetica", 12)
        pdf.drawString(72, 720, "Text above a preserved image and link.")
        pdf.drawImage(ImageReader(image_buffer), 72, 560, width=120, height=90)
        pdf.linkURL("https://example.com/preserved", (72, 700, 300, 735))
        pdf.save()

        output = build_binary_output(
            "pdf",
            source_buffer.getvalue(),
            "图片和链接上方的译文。",
            target_language="中文",
        )
        reader = PdfReader(io.BytesIO(output))
        page = reader.pages[0]
        self.assertEqual(
            reader.metadata.subject,
            "Layout-preserving translation generated from a text-layer PDF",
        )
        resources = page["/Resources"].get_object()
        xobjects = resources["/XObject"].get_object()
        images = [
            reference.get_object()
            for reference in xobjects.values()
            if str(reference.get_object().get("/Subtype", "")) == "/Image"
        ]
        self.assertEqual(len(images), 1)
        annotations = page["/Annots"].get_object()
        uris = [
            str(annotation.get_object()["/A"].get_object().get("/URI", ""))
            for annotation in annotations
        ]
        self.assertIn("https://example.com/preserved", uris)

    def test_pdf_output_preview_uses_stable_state_units(self):
        source = make_pdf()
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
                    app.SOURCE_CACHE_DIR, "sample.pdf", source
                )
                translations = "\n".join(
                    f"PDF translation {index}."
                    for index, _ in enumerate(imported["content"].split("\n"), 1)
                )
                output_name = app.save_translation_output(
                    {"name": "sample.pdf", **imported}, translations, "英文"
                )
                metadata = app._read_output_index()[output_name]
                output_path = os.path.join(app.OUTPUTS_DIR, output_name)

                self.assertEqual(output_name, "sample.en.pdf")
                self.assertEqual(metadata["preview_content"], translations)
                self.assertEqual(
                    app.read_output_preview(
                        output_path, output_name, metadata["preview_content"]
                    ),
                    translations,
                )
            finally:
                app.OUTPUTS_DIR, app.OUTPUT_INDEX_PATH, app.SOURCE_CACHE_DIR = original_paths

    def test_pdf_output_metadata_keeps_selected_page_range(self):
        source = make_pdf()
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
                    app.SOURCE_CACHE_DIR, "range.pdf", source
                )
                selected = extract_binary_text("pdf", source, 2, 2)
                translations = "\n".join(
                    f"Range translation {index}."
                    for index, _ in enumerate(selected.split("\n"), 1)
                )
                file_info = {
                    "name": "range.pdf",
                    **imported,
                    "content": selected,
                    "pdf_page_start": 2,
                    "pdf_page_end": 2,
                    "pdf_page_selection": "2",
                }

                output_name = app.save_translation_output(
                    file_info, translations, "英文"
                )
                metadata = app._read_output_index()[output_name]

                self.assertEqual(metadata["pdf_page_count"], 3)
                self.assertEqual(metadata["pdf_page_start"], 2)
                self.assertEqual(metadata["pdf_page_end"], 2)
                self.assertEqual(metadata["pdf_page_selection"], "2")
                self.assertTrue(metadata["pdf_strict_layout"])
                self.assertEqual(
                    metadata["pdf_extraction_version"],
                    document_formats.PDF_EXTRACTION_VERSION,
                )
                self.assertEqual(metadata["preview_content"], translations)
            finally:
                app.OUTPUTS_DIR, app.OUTPUT_INDEX_PATH, app.SOURCE_CACHE_DIR = original_paths

    def test_pdf_import_preview_and_download_api(self):
        source = make_pdf()
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
                    "name": "sample.pdf",
                    "data_base64": base64.b64encode(source).decode("ascii"),
                }).encode("utf-8")
                connection.request(
                    "POST", "/api/import", body=payload,
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                imported = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                self.assertEqual(imported["source_format"], "pdf")
                self.assertEqual(imported["pdf_page_count"], 3)
                self.assertEqual(imported["pdf_page_start"], 1)
                self.assertEqual(imported["pdf_page_end"], 3)
                self.assertEqual(imported["pdf_page_selection"], "all")
                self.assertEqual(
                    imported["pdf_extraction_version"],
                    document_formats.PDF_EXTRACTION_VERSION,
                )

                connection.request(
                    "GET",
                    "/api/source?id=" + imported["source_id"]
                    + "&format=pdf&sha256=" + imported["source_sha256"]
                    + "&page_start=2&page_end=2",
                )
                response = connection.getresponse()
                ranged = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                self.assertEqual(ranged["pdf_page_count"], 3)
                self.assertEqual(ranged["pdf_page_start"], 2)
                self.assertEqual(ranged["pdf_page_end"], 2)
                self.assertEqual(ranged["pdf_page_selection"], "2")
                self.assertEqual(
                    ranged["pdf_extraction_version"],
                    document_formats.PDF_EXTRACTION_VERSION,
                )
                self.assertEqual(
                    ranged["content"], extract_binary_text("pdf", source, 2, 2)
                )

                connection.request(
                    "GET",
                    "/api/source?id=" + imported["source_id"]
                    + "&format=pdf&sha256=" + imported["source_sha256"]
                    + "&pages=1,3",
                )
                response = connection.getresponse()
                sparse = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                self.assertEqual(sparse["pdf_page_selection"], "1,3")
                self.assertEqual(sparse["pdf_page_start"], 1)
                self.assertEqual(sparse["pdf_page_end"], 3)
                self.assertEqual(
                    sparse["content"],
                    extract_binary_text(
                        "pdf", source, pdf_page_selection="1,3"
                    ),
                )

                connection.request(
                    "GET",
                    "/api/source?id=" + imported["source_id"]
                    + "&format=pdf&sha256=" + imported["source_sha256"]
                    + "&page_start=4&page_end=4",
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 400)

                translations = "\n".join(
                    f"API translation {index}."
                    for index, _ in enumerate(imported["content"].split("\n"), 1)
                )
                output_name = app.save_translation_output(
                    {"name": "sample.pdf", **imported}, translations, "英文"
                )
                connection.request("GET", "/api/output?name=" + output_name)
                response = connection.getresponse()
                preview = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                self.assertEqual(preview["content"], translations)

                connection.request("GET", "/api/output-file?name=" + output_name)
                response = connection.getresponse()
                downloaded = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), "application/pdf")
                self.assertTrue(downloaded.startswith(b"%PDF-"))
                connection.close()
            finally:
                if server is not None:
                    server.shutdown()
                    server.server_close()
                if thread is not None:
                    thread.join(timeout=5)
                app.OUTPUTS_DIR, app.OUTPUT_INDEX_PATH, app.SOURCE_CACHE_DIR = original_paths

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

    def test_local_http_guard_rejects_rebinding_cross_site_and_simple_post(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(
                "GET", "/api/history", headers={"Host": "malicious.example"}
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 403)

            connection.request(
                "GET", "/api/history", headers={"Sec-Fetch-Site": "cross-site"}
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 403)

            connection.request(
                "POST",
                "/api/interrupt",
                body="job=missing",
                headers={"Content-Type": "text/plain"},
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 415)

            connection.request(
                "POST",
                "/api/interrupt",
                body=b'{}',
                headers={
                    "Content-Type": "application/json",
                    "Origin": "https://malicious.example",
                },
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 403)

            connection.request(
                "POST",
                "/api/interrupt",
                body=b'{}',
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Origin": f"http://127.0.0.1:{port}",
                },
            )
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertFalse(payload["ok"])
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

"""DOCX/EPUB/PDF 二进制文档的安全导入、文本映射和输出重建。"""

import contextlib
import copy
import gc
import hashlib
import io
import json
import os
import posixpath
import re
import sys
import threading
import unicodedata
import uuid
import zipfile
import xml.etree.ElementTree as ET
from html.entities import html5 as HTML_ENTITIES
from urllib.parse import unquote


class DocumentFormatError(ValueError):
    pass


BINARY_EXTENSIONS = {".docx", ".epub", ".pdf"}
PDF_EXTRACTION_VERSION = 7
PDF_RECOGNITION_MODES = {"auto", "text", "ocr"}
MAX_ZIP_MEMBERS = 5000
MAX_ZIP_COMPRESSION_RATIO = 1000.0
MAX_PDF_PAGES = 1000
PDF_STRICT_MIN_FONT_SIZE = 1.0
PDF_OCR_CACHE_VERSION = 2
PDF_OCR_MAX_IMAGE_DIMENSION = 1800
PDF_OCR_MIN_SCORE = 0.45

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
W_P = f"{{{W_NS}}}p"
W_T = f"{{{W_NS}}}t"
W_DEL = f"{{{W_NS}}}del"
W_TAB = f"{{{W_NS}}}tab"
W_BR = f"{{{W_NS}}}br"
W_CR = f"{{{W_NS}}}cr"

ET.register_namespace("w", W_NS)
ET.register_namespace("xml", XML_NS)
FORMAT_LOCK = threading.RLock()


def document_extension(name):
    return os.path.splitext(str(name))[1].lower()


def is_binary_document(name_or_format):
    value = str(name_or_format or "").lower()
    if not value.startswith("."):
        value = "." + value
    return value in BINARY_EXTENSIONS


def normalize_pdf_recognition_mode(value):
    mode = str(value or "auto").strip().lower()
    return mode if mode in PDF_RECOGNITION_MODES else "auto"


def _binary_path(data):
    if isinstance(data, os.PathLike):
        return os.fspath(data)
    if isinstance(data, str):
        return data
    return None


def binary_digest(data):
    path = _binary_path(data)
    if path is None:
        return hashlib.sha256(data).hexdigest()
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise DocumentFormatError("无法读取二进制原文缓存") from exc
    return digest.hexdigest()


def _read_zip_member(archive, name):
    try:
        return archive.read(name)
    except (
        KeyError,
        OSError,
        RuntimeError,
        NotImplementedError,
        EOFError,
        zipfile.BadZipFile,
    ) as exc:
        raise DocumentFormatError(f"无法读取文档内部文件: {name}") from exc


def _validated_zip(data, expected_extension):
    path = _binary_path(data)
    if path is None and not isinstance(data, (bytes, bytearray)):
        raise DocumentFormatError("文档数据无效")
    if path is None and not data:
        raise DocumentFormatError("文档为空")
    try:
        if path is not None:
            if not os.path.isfile(path) or os.path.getsize(path) <= 0:
                raise DocumentFormatError("文档为空")
            archive = zipfile.ZipFile(path, "r")
        else:
            archive = zipfile.ZipFile(io.BytesIO(data), "r")
        members = archive.infolist()
    except DocumentFormatError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise DocumentFormatError("文件不是有效的 ZIP 文档容器") from exc
    if len(members) > MAX_ZIP_MEMBERS:
        archive.close()
        raise DocumentFormatError("文档内部文件数量过多")
    for member in members:
        if member.flag_bits & 0x1:
            archive.close()
            raise DocumentFormatError("暂不支持加密文档")
        compressed_size = max(1, int(member.compress_size or 0))
        if (
            member.file_size >= 1024 * 1024
            and member.file_size / compressed_size > MAX_ZIP_COMPRESSION_RATIO
        ):
            archive.close()
            raise DocumentFormatError("文档内部存在异常压缩比，可能是压缩炸弹")
    member_names = archive.namelist()
    names = set(member_names)
    if len(names) != len(member_names):
        archive.close()
        raise DocumentFormatError("文档内部存在重复路径")
    if expected_extension == ".docx":
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            archive.close()
            raise DocumentFormatError("文件不是有效的 DOCX 文档")
    elif expected_extension == ".epub":
        try:
            mimetype = _read_zip_member(archive, "mimetype").decode("ascii").strip()
        except (DocumentFormatError, UnicodeError):
            mimetype = ""
        if mimetype != "application/epub+zip" or "META-INF/container.xml" not in names:
            archive.close()
            raise DocumentFormatError("文件不是有效的 EPUB 文档")

    # 导入时读遍所有成员，让 CRC 错误、不支持的压缩算法和
    # 伪造解压大小在翻译前就失败，不要等到回写容器时才发现。
    try:
        for member in members:
            if member.is_dir():
                continue
            with archive.open(member, "r") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
    except DocumentFormatError:
        archive.close()
        raise
    except (
        OSError,
        RuntimeError,
        NotImplementedError,
        EOFError,
        zipfile.BadZipFile,
    ) as exc:
        archive.close()
        raise DocumentFormatError("文档内部文件损坏或压缩算法不受支持") from exc
    return archive


def _parse_xml(data, label):
    try:
        parser = ET.XMLParser(
            target=ET.TreeBuilder(insert_comments=True, insert_pis=True)
        )
        return ET.fromstring(data, parser=parser)
    except ET.ParseError as exc:
        raise DocumentFormatError(f"无法解析文档内部的 {label}") from exc


def _serialize_xml(root, original_data):
    """序列化已修改 XML，同时保留容器原有的前缀声明和文档头。"""
    original_root = re.search(rb"<([A-Za-z_][\w.:-]*)(?:\s|>)", original_data)
    namespace_source = b""
    if original_root:
        root_end = original_data.find(b">", original_root.start())
        if root_end >= 0:
            namespace_source = original_data[original_root.start():root_end]
    declarations = []
    for match in re.finditer(
        rb"xmlns(?::([A-Za-z_][\w.-]*))?=[\"']([^\"']+)[\"']",
        namespace_source,
    ):
        prefix = (match.group(1) or b"").decode("ascii", "ignore")
        uri = match.group(2).decode("utf-8", "replace")
        declarations.append((prefix, uri))
        if prefix != "xml":
            try:
                ET.register_namespace(prefix, uri)
            except ValueError:
                pass

    serialized = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    original_declaration = re.match(rb"\s*(<\?xml[^?]*\?>)", original_data)
    if original_declaration:
        serialized = re.sub(
            rb"^<\?xml[^?]*\?>",
            original_declaration.group(1),
            serialized,
            count=1,
        )

    root_start = re.search(rb"<([A-Za-z_][\w.:-]*)(?:\s|>)", serialized)
    if root_start:
        insertion = serialized.find(b">", root_start.start())
        if insertion >= 0:
            start_tag = serialized[root_start.start():insertion]
            missing = []
            for prefix, uri in declarations:
                if prefix == "xml":
                    continue
                marker = ("xmlns" + (":" + prefix if prefix else "") + "=").encode("ascii")
                if marker not in start_tag:
                    name = "xmlns" + (":" + prefix if prefix else "")
                    escaped_uri = (
                        uri.replace("&", "&amp;")
                        .replace('"', "&quot;")
                        .replace("<", "&lt;")
                    )
                    missing.append(f' {name}="{escaped_uri}"'.encode("utf-8"))
            if missing:
                serialized = serialized[:insertion] + b"".join(missing) + serialized[insertion:]

    doctype = re.search(rb"<!DOCTYPE\s+[^>]+>", original_data, re.IGNORECASE)
    if doctype and b"<!DOCTYPE" not in serialized.upper():
        declaration_end = serialized.find(b"?>")
        insert_at = declaration_end + 2 if declaration_end >= 0 else 0
        serialized = (
            serialized[:insert_at]
            + b"\n"
            + doctype.group(0)
            + serialized[insert_at:]
        )
    return serialized


def _normal_text(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _xml_safe_translation(text):
    value = str(text)
    for character in value:
        codepoint = ord(character)
        if not (
            codepoint in {0x9, 0xA, 0xD}
            or 0x20 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        ):
            raise DocumentFormatError("译文包含 XML 不支持的控制字符")
    return value


def _nearest_boundary(text, target, minimum):
    if target <= minimum:
        return minimum
    if target >= len(text):
        return len(text)
    boundary_chars = set(" \t\r\n,.;:!?，。！？；：、)]}）】》」』”’")
    radius = min(16, max(4, len(text) // 20))
    candidates = []
    for index in range(max(minimum + 1, target - radius), min(len(text), target + radius) + 1):
        if text[index - 1] in boundary_chars or (index < len(text) and text[index] in boundary_chars):
            candidates.append(index)
    return min(candidates, key=lambda value: abs(value - target)) if candidates else target


def _partition_translation(text, weights):
    """按原文本长度比例切分译文，尽量让现有行内样式仍覆盖相近范围。"""
    if not weights:
        return []
    if len(weights) == 1:
        return [text]
    total = max(1, sum(max(1, value) for value in weights))
    cuts = []
    cumulative = 0
    previous = 0
    for weight in weights[:-1]:
        cumulative += max(1, weight)
        target = round(len(text) * cumulative / total)
        cut = _nearest_boundary(text, target, previous)
        cuts.append(cut)
        previous = cut
    parts = []
    start = 0
    for cut in cuts:
        parts.append(text[start:cut])
        start = cut
    parts.append(text[start:])
    return parts


def _docx_parts(archive):
    names = set(archive.namelist())
    ordered = ["word/document.xml"]
    patterns = (
        r"word/header\d+\.xml$",
        r"word/footer\d+\.xml$",
        r"word/footnotes\.xml$",
        r"word/endnotes\.xml$",
    )
    for pattern in patterns:
        ordered.extend(sorted(name for name in names if re.fullmatch(pattern, name)))
    return list(dict.fromkeys(name for name in ordered if name in names))


def _docx_paragraph_nodes(paragraph):
    text_nodes = []
    visible_parts = []

    def walk(element, deleted=False):
        deleted = deleted or element.tag == W_DEL
        if element is not paragraph and element.tag == W_P:
            # 文本框等结构可以在外层段落中嵌套独立段落。内层段落会由
            # _docx_units_from_root 单独遍历，不能重复并入外层单元。
            return
        if element.tag == W_T and not deleted:
            value = element.text or ""
            visible_parts.append(value)
            if value:
                text_nodes.append(element)
            return
        if element.tag in {W_TAB, W_BR, W_CR} and not deleted:
            visible_parts.append(" ")
        for child in list(element):
            walk(child, deleted)

    walk(paragraph)
    return _normal_text("".join(visible_parts)), text_nodes


def _docx_units_from_root(root):
    units = []
    for paragraph in root.iter(W_P):
        source, nodes = _docx_paragraph_nodes(paragraph)
        if source and nodes:
            units.append((source, nodes))
    return units


def _extract_docx(data):
    archive = _validated_zip(data, ".docx")
    try:
        units = []
        for part in _docx_parts(archive):
            root = _parse_xml(_read_zip_member(archive, part), part)
            units.extend(source for source, _ in _docx_units_from_root(root))
        if not units:
            raise DocumentFormatError("DOCX 中没有找到可翻译文本")
        return "\n".join(units)
    finally:
        archive.close()


def _set_docx_text(node, text):
    node.text = text
    space_key = f"{{{XML_NS}}}space"
    if text[:1].isspace() or text[-1:].isspace():
        node.set(space_key, "preserve")
    else:
        node.attrib.pop(space_key, None)


def _build_docx(data, translated_text):
    archive = _validated_zip(data, ".docx")
    translations = str(translated_text).split("\n")
    replacements = {}
    position = 0
    try:
        for part in _docx_parts(archive):
            original_part = _read_zip_member(archive, part)
            root = _parse_xml(original_part, part)
            units = _docx_units_from_root(root)
            for _source, nodes in units:
                if position >= len(translations):
                    raise DocumentFormatError("译文单元数量少于 DOCX 原文单元")
                translation = _xml_safe_translation(translations[position])
                weights = [len(node.text or "") for node in nodes]
                pieces = _partition_translation(translation, weights)
                for node, piece in zip(nodes, pieces):
                    _set_docx_text(node, piece)
                position += 1
            replacements[part] = _serialize_xml(root, original_part)
        if position != len(translations):
            raise DocumentFormatError("译文单元数量多于 DOCX 原文单元")
        return _repack_zip(archive, replacements)
    finally:
        archive.close()


def _epub_spine_parts(archive):
    container = _parse_xml(
        _read_zip_member(archive, "META-INF/container.xml"), "META-INF/container.xml"
    )
    rootfile = container.find(".//{*}rootfile")
    opf_path = unquote(rootfile.get("full-path") or "") if rootfile is not None else ""
    opf_path = posixpath.normpath(opf_path)
    if opf_path.startswith("../") or opf_path.startswith("/"):
        raise DocumentFormatError("EPUB 的 OPF 路径无效")
    if not opf_path or opf_path not in archive.namelist():
        raise DocumentFormatError("EPUB 缺少有效的 OPF 包文件")
    package = _parse_xml(_read_zip_member(archive, opf_path), opf_path)
    manifest = {}
    for item in package.findall(".//{*}manifest/{*}item"):
        item_id = item.get("id")
        href = unquote((item.get("href") or "").split("#", 1)[0])
        if not item_id or not href:
            continue
        path = posixpath.normpath(posixpath.join(posixpath.dirname(opf_path), href))
        if path.startswith("../") or path.startswith("/"):
            continue
        manifest[item_id] = (path, item.get("media-type", ""))
    parts = []
    for itemref in package.findall(".//{*}spine/{*}itemref"):
        entry = manifest.get(itemref.get("idref"))
        if entry and entry[1] == "application/xhtml+xml" and entry[0] in archive.namelist():
            parts.append(entry[0])
    if not parts:
        raise DocumentFormatError("EPUB 阅读顺序中没有 XHTML 正文")
    return list(dict.fromkeys(parts))


def _reject_encrypted_spine(archive, spine_parts):
    if "META-INF/encryption.xml" not in archive.namelist():
        return
    encryption = _parse_xml(
        _read_zip_member(archive, "META-INF/encryption.xml"), "META-INF/encryption.xml"
    )
    protected = set()
    for reference in encryption.findall(".//{*}CipherReference"):
        uri = unquote((reference.get("URI") or "").split("#", 1)[0])
        normalized = posixpath.normpath(uri).lstrip("/")
        if normalized and not normalized.startswith("../"):
            protected.add(normalized)
    if protected.intersection(spine_parts):
        raise DocumentFormatError("暂不支持正文已加密的 EPUB")


EPUB_BLOCKS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dt", "dd",
    "figcaption", "caption", "td", "th", "div", "section", "article",
    "aside", "blockquote",
}
EPUB_IGNORED = {
    "head", "script", "style", "noscript", "svg", "math", "rt", "rp"
}


def _local_name(tag):
    if not isinstance(tag, str):
        return ""
    return str(tag).rsplit("}", 1)[-1].lower()


def _xhtml_units_from_root(root):
    units = []
    visible = []
    slots = []

    def add(owner, attribute, value):
        if value is None:
            return
        visible.append(value)
        if value.strip():
            slots.append((owner, attribute, value))

    def flush():
        source = _normal_text("".join(visible))
        if source and slots:
            units.append((source, list(slots)))
        visible.clear()
        slots.clear()

    def walk(node):
        node_name = _local_name(node.tag)
        if not node_name or node_name in EPUB_IGNORED:
            return
        add(node, "text", node.text)
        for child in list(node):
            name = _local_name(child.tag)
            if not name or name in EPUB_IGNORED:
                pass
            elif name in EPUB_BLOCKS:
                # 父容器的直接/内联文字必须先成为独立单元，再递归
                # 子块；子块 tail 随后继续归入父容器的下一个片段。
                flush()
                walk(child)
                flush()
            elif name in {"br", "hr"}:
                # br/hr 是可见边界；以空格阻止两侧文字粘连。
                visible.append(" ")
            else:
                walk(child)
            add(child, "tail", child.tail)

    walk(root)
    flush()
    return units


def _parse_xhtml(data, label):
    try:
        root = _parse_xml(data, label)
    except DocumentFormatError:
        xml_entities = {b"amp", b"lt", b"gt", b"apos", b"quot"}

        def expand(match):
            name = match.group(1)
            if name in xml_entities:
                return match.group(0)
            value = HTML_ENTITIES.get(name.decode("ascii") + ";")
            if value is None:
                return match.group(0)
            return "".join(f"&#x{ord(character):X};" for character in value).encode("ascii")

        expanded = re.sub(rb"&([A-Za-z][A-Za-z0-9]+);", expand, data)
        if expanded == data:
            raise
        root = _parse_xml(expanded, label)
    match = re.match(r"^\{([^}]+)\}", root.tag)
    if match:
        try:
            ET.register_namespace("", match.group(1))
        except ValueError:
            pass
    return root


def _extract_epub(data):
    archive = _validated_zip(data, ".epub")
    try:
        units = []
        parts = _epub_spine_parts(archive)
        _reject_encrypted_spine(archive, parts)
        for part in parts:
            root = _parse_xhtml(_read_zip_member(archive, part), part)
            units.extend(source for source, _ in _xhtml_units_from_root(root))
        if not units:
            raise DocumentFormatError("EPUB 中没有找到可翻译正文")
        return "\n".join(units)
    finally:
        archive.close()


def _build_epub(data, translated_text):
    archive = _validated_zip(data, ".epub")
    translations = str(translated_text).split("\n")
    replacements = {}
    position = 0
    try:
        parts = _epub_spine_parts(archive)
        _reject_encrypted_spine(archive, parts)
        for part in parts:
            original_part = _read_zip_member(archive, part)
            root = _parse_xhtml(original_part, part)
            units = _xhtml_units_from_root(root)
            for _source, slots in units:
                if position >= len(translations):
                    raise DocumentFormatError("译文单元数量少于 EPUB 原文单元")
                translation = _xml_safe_translation(translations[position])
                weights = [len(value) for _, _, value in slots]
                pieces = _partition_translation(translation, weights)
                for (owner, attribute, _value), piece in zip(slots, pieces):
                    setattr(owner, attribute, piece)
                position += 1
            replacements[part] = _serialize_xml(root, original_part)
        if position != len(translations):
            raise DocumentFormatError("译文单元数量多于 EPUB 原文单元")
        return _repack_zip(archive, replacements, epub=True)
    finally:
        archive.close()


def _repack_zip(archive, replacements, epub=False):
    output = io.BytesIO()
    infos = archive.infolist()
    with zipfile.ZipFile(output, "w") as target:
        target.comment = archive.comment
        written = set()
        if epub and "mimetype" in archive.namelist():
            info = archive.getinfo("mimetype")
            mime_info = copy.copy(info)
            mime_info.compress_type = zipfile.ZIP_STORED
            target.writestr(mime_info, _read_zip_member(archive, "mimetype"))
            written.add("mimetype")
        for info in infos:
            if info.filename in written:
                continue
            member_data = (
                replacements[info.filename]
                if info.filename in replacements
                else _read_zip_member(archive, info.filename)
            )
            target.writestr(info, member_data)
            written.add(info.filename)
    return output.getvalue()


def _pdf_reader(data):
    """校验 PDF 容器；密码文件必须由用户先解密。"""
    path = _binary_path(data)
    if path is None and not isinstance(data, (bytes, bytearray)):
        raise DocumentFormatError("PDF 数据无效")
    if path is None and not data:
        raise DocumentFormatError("PDF 为空")
    if path is not None and (not os.path.isfile(path) or os.path.getsize(path) <= 0):
        raise DocumentFormatError("PDF 为空")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocumentFormatError(
            "PDF 支持尚未安装，请先运行: py -3 -m pip install -r requirements.txt"
        ) from exc
    try:
        source = path if path is not None else io.BytesIO(bytes(data))
        reader = PdfReader(source, strict=False)
        if reader.is_encrypted:
            raise DocumentFormatError("暂不支持加密 PDF，请先解除密码保护")
        page_count = len(reader.pages)
    except DocumentFormatError:
        raise
    except Exception as exc:
        raise DocumentFormatError("文件不是有效或可读取的 PDF") from exc
    if page_count <= 0:
        raise DocumentFormatError("PDF 中没有页面")
    if page_count > MAX_PDF_PAGES:
        raise DocumentFormatError(f"PDF 超过 {MAX_PDF_PAGES} 页限制")
    return reader


@contextlib.contextmanager
def _pdf_decode_limits():
    """本地可信文件处理期间临时取消 pypdf 的固定字节上限。"""
    try:
        import pypdf.filters as pdf_filters
    except ImportError:
        yield
        return
    names = (
        "FLATE_MAX_BUFFER_SIZE",
        "JBIG2_MAX_OUTPUT_LENGTH",
        "ZLIB_MAX_OUTPUT_LENGTH",
        "LZW_MAX_OUTPUT_LENGTH",
        "MAX_DECLARED_STREAM_LENGTH",
        "RUN_LENGTH_MAX_OUTPUT_LENGTH",
        "MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH",
    )
    previous = {}
    for name in names:
        if not hasattr(pdf_filters, name):
            continue
        value = getattr(pdf_filters, name)
        previous[name] = value
        setattr(pdf_filters, name, sys.maxsize)
    try:
        yield
    finally:
        for name, value in previous.items():
            setattr(pdf_filters, name, value)


def _pdf_heading_like(text):
    value = str(text or "").strip()
    if not value or len(value) > 100:
        return False
    if re.search(r"[。！？.!?；;：:]\s*[\"'”’」』）)]*$", value):
        return False
    if re.match(
        r"^(?:chapter|section|part|appendix|volume|book)\b", value, re.IGNORECASE
    ):
        return True
    letters = [character for character in value if character.isalpha()]
    if letters and all(not character.islower() for character in letters):
        return True
    words = [word.strip("\"'“”‘’()[]{}") for word in value.split()]
    titled = sum(bool(word[:1].isupper()) for word in words if word)
    if 2 <= len(words) <= 12 and titled >= max(2, len(words) - 2):
        return True
    # 没有空格的短中日韩标题只能使用长度作保守判断。
    return (
        len(words) == 1
        and bool(re.search(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", value))
        and len(value) <= 16
    )


def _pdf_bullet_like(text):
    return bool(re.match(
        r"^(?:[-*•▪◦]\s+|\(?\d{1,4}[.)、]\s*|[A-Za-z][.)]\s+)",
        str(text or "").lstrip(),
    ))


def _pdf_terminal_line(text):
    return bool(re.search(
        r"[。！？.!?；;：:]\s*[\"'”’」』）)\]]*$", str(text or "").rstrip()
    ))


def _pdf_page_text(page):
    """优先取得保留视觉空行的文本；旧版 pypdf 则回退普通提取。"""
    try:
        text = page.extract_text(
            extraction_mode="layout",
            layout_mode_space_vertically=True,
            layout_mode_strip_rotated=False,
        )
    except TypeError:
        text = page.extract_text()
    except Exception as exc:
        raise DocumentFormatError("PDF 页面文字层读取失败") from exc
    # Some PDFs use a decorative font for TOC leader dots without a usable
    # ToUnicode map.  Extractors expose those glyphs as U+FFFD (or controls).
    # Keeping them would send garbage to the model and later render tofu boxes.
    # Replace them with spaces so layout gaps survive and real words stay apart.
    value = str(text or "").replace("\x00", " ").replace("\ufffd", " ")
    return "".join(
        character
        if character in "\t\r\n" or unicodedata.category(character) != "Cc"
        else " "
        for character in value
    )


def _pdf_page_units(page, text=None):
    """把 PDF 的视觉行合并成句段单元，绝不从字符中间建立定位键。"""
    if text is None:
        text = _pdf_page_text(page)
    units = []
    current = []

    def flush():
        if not current:
            return
        combined = _normal_text(" ".join(current))
        current.clear()
        if combined:
            units.append({"text": combined, "heading": _pdf_heading_like(combined)})

    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw_line.strip():
            flush()
            continue
        # layout 模式用较长空格表达表格列或并排文本。每一格单独作为
        # 单元比把两列拼成一句更安全；复杂多栏阅读顺序仍需人工复核。
        cells = [
            _normal_text(part)
            for part in re.split(r"[ \t]{4,}", raw_line.strip())
            if _normal_text(part)
        ]
        if len(cells) > 1:
            flush()
            for cell in cells:
                units.append({"text": cell, "heading": _pdf_heading_like(cell)})
            continue
        line = cells[0] if cells else _normal_text(raw_line)
        if not line:
            flush()
            continue

        existing = _normal_text(" ".join(current))
        starts_new = bool(current) and (
            _pdf_terminal_line(existing)
            or _pdf_heading_like(existing)
            or _pdf_heading_like(line)
            or _pdf_bullet_like(line)
        )
        if starts_new:
            flush()

        if current and current[-1].endswith("-") and line[:1].isalnum():
            current[-1] = current[-1][:-1] + line
        else:
            current.append(line)
    flush()
    return units


def _pdf_units_with_repaired_word_spacing(page, layout_units):
    """Prefer plain extraction when it only removes bogus kerning spaces."""
    try:
        plain_text = page.extract_text()
    except Exception:
        return layout_units
    plain_value = str(plain_text or "").replace("\x00", " ").replace("\ufffd", " ")
    plain_value = "".join(
        character
        if character in "\t\r\n" or unicodedata.category(character) != "Cc"
        else " "
        for character in plain_value
    )
    plain_value = re.sub(r"\b([A-Z])\s+([.,;:])", r"\1\2", plain_value)
    plain_units = _pdf_page_units(page, plain_value)
    if len(plain_units) != len(layout_units):
        return layout_units

    def compact(value):
        return "".join(character for character in str(value or "") if not character.isspace())

    if not all(
        compact(layout.get("text")) == compact(plain.get("text"))
        for layout, plain in zip(layout_units, plain_units)
    ):
        return layout_units
    layout_spaces = sum(unit["text"].count(" ") for unit in layout_units)
    plain_spaces = sum(unit["text"].count(" ") for unit in plain_units)
    return plain_units if plain_spaces < layout_spaces else layout_units


def _pdf_units_look_artificially_fragmented(units):
    """Detect glyph-by-glyph extraction without mistaking ordinary short labels."""
    values = [_normal_text(unit.get("text")) for unit in units]
    values = [value for value in values if value]
    if len(values) < 6:
        return False

    # Kerning bugs often split proper names as ``S andy`` / ``W illis``.
    # Exclude the legitimate English one-letter words A and I, and require
    # more than one occurrence before switching the whole page extractor.
    broken_capital_words = sum(
        len(re.findall(r"\b[B-HJ-Z]\s+[a-z]{2,}\b", value))
        for value in values
    )
    if broken_capital_words >= 2:
        return True

    alphabetic = [
        value for value in values
        if re.search(r"[A-Za-z]", value) and not re.search(r"\d", value)
    ]
    tiny = [value for value in alphabetic if len(_pdf_layout_key(value)) <= 3]
    if len(alphabetic) >= 6 and len(tiny) / len(alphabetic) >= 0.65:
        return True

    for value in values:
        tokens = re.findall(r"[A-Za-z]+", value)
        if len(tokens) >= 8 and sum(len(token) <= 2 for token in tokens) / len(tokens) >= 0.7:
            return True
    return False


_PDF_TOC_PAGE_LABEL = re.compile(
    r"^(?:[0-9]{1,4}|[ivxlcdmIVXLCDM]{1,12})(?:\s*[-–]\s*[0-9]{1,4})?$"
)


def _pdf_probable_toc_rows(text):
    """识别由标题、点线和页码组成的目录页，并返回干净的视觉行。"""
    rows = []
    numbered = 0
    value = "".join(
        character
        if character in "\t\r\n" or (
            character != "\ufffd" and unicodedata.category(character) != "Cc"
        )
        else " "
        for character in str(text or "")
    )
    for raw_line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw_line.strip():
            continue
        cells = [
            _normal_text(part)
            for part in re.split(r"[ \t]{4,}", raw_line.strip())
            if _normal_text(part)
        ]
        if not cells:
            continue
        page_label = ""
        if len(cells) >= 2 and _PDF_TOC_PAGE_LABEL.fullmatch(cells[-1]):
            page_label = cells.pop()
            numbered += 1
        title = _normal_text(" ".join(cells))
        if not title:
            continue
        rows.append({"title": title, "page_label": page_label})

    if len(rows) < 5 or numbered < max(4, (len(rows) + 1) // 2):
        return []
    return rows


def _pdf_page_size(page):
    try:
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        rotation = int(page.get("/Rotate", 0) or 0) % 360
    except (TypeError, ValueError, AttributeError):
        width, height, rotation = 612.0, 792.0, 0
    if rotation in {90, 270}:
        width, height = height, width
    # 扫描 PDF 常直接用图片像素作为 MediaBox，尺寸会明显大于普通纸张
    # 的 72dpi 点数。只拒绝明显损坏的极端值，不能把合法扫描页改成 Letter。
    if not (36 <= width <= 20000 and 36 <= height <= 20000):
        return 612.0, 792.0
    return width, height


def _normalize_pdf_page_range(page_count, page_start=None, page_end=None):
    """返回经过校验的 1-based、两端包含的 PDF 页码范围。"""
    try:
        count = int(page_count)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DocumentFormatError("PDF 页数无效") from exc
    if count < 1:
        raise DocumentFormatError("PDF 没有页面")

    def page_number(value, default, label):
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            raise DocumentFormatError(f"PDF {label}必须是整数")
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DocumentFormatError(f"PDF {label}必须是整数") from exc
        if isinstance(value, float) and not value.is_integer():
            raise DocumentFormatError(f"PDF {label}必须是整数")
        if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value.strip()):
            raise DocumentFormatError(f"PDF {label}必须是整数")
        return number

    start = page_number(page_start, 1, "起始页")
    end = page_number(page_end, count, "结束页")
    if start < 1 or end > count:
        raise DocumentFormatError(f"PDF 页码范围必须在 1–{count} 之间")
    if start > end:
        raise DocumentFormatError("PDF 起始页不能大于结束页")
    return start, end


def normalize_pdf_page_selection(
    page_count, page_selection=None, page_start=None, page_end=None,
):
    """返回按文档顺序排列的页码；兼容旧版连续起止页。"""
    try:
        count = int(page_count)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DocumentFormatError("PDF 页数无效") from exc
    if count < 1:
        raise DocumentFormatError("PDF 没有页面")

    if page_selection is None or page_selection == "":
        start, end = _normalize_pdf_page_range(count, page_start, page_end)
        return tuple(range(start, end + 1))

    if isinstance(page_selection, str):
        value = page_selection.strip().lower()
        if value == "all":
            return tuple(range(1, count + 1))
        if not value:
            raise DocumentFormatError("PDF 至少需要选择一页")
        selected = set()
        for token in value.split(","):
            token = token.strip()
            match = re.fullmatch(r"([0-9]+)(?:\s*-\s*([0-9]+))?", token)
            if not match:
                raise DocumentFormatError("PDF 页码选择格式无效")
            first = int(match.group(1))
            last = int(match.group(2) or first)
            if first < 1 or last > count:
                raise DocumentFormatError(f"PDF 页码必须在 1–{count} 之间")
            if first > last:
                raise DocumentFormatError("PDF 页码区间起点不能大于终点")
            selected.update(range(first, last + 1))
    elif isinstance(page_selection, (list, tuple, set, range)):
        selected = set()
        for value in page_selection:
            if isinstance(value, bool):
                raise DocumentFormatError("PDF 页码必须是整数")
            try:
                page_number = int(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise DocumentFormatError("PDF 页码必须是整数") from exc
            if isinstance(value, float) and not value.is_integer():
                raise DocumentFormatError("PDF 页码必须是整数")
            if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value.strip()):
                raise DocumentFormatError("PDF 页码必须是整数")
            if page_number < 1 or page_number > count:
                raise DocumentFormatError(f"PDF 页码必须在 1–{count} 之间")
            selected.add(page_number)
    else:
        raise DocumentFormatError("PDF 页码选择格式无效")

    if not selected:
        raise DocumentFormatError("PDF 至少需要选择一页")
    return tuple(sorted(selected))


def format_pdf_page_selection(page_numbers, page_count):
    pages = normalize_pdf_page_selection(page_count, page_numbers)
    if len(pages) == int(page_count):
        return "all"
    ranges = []
    start = previous = pages[0]
    for page_number in pages[1:]:
        if page_number == previous + 1:
            previous = page_number
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page_number
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


_PDF_OCR_ENGINE = None


def _pdf_ocr_engine():
    global _PDF_OCR_ENGINE
    if _PDF_OCR_ENGINE is not None:
        return _PDF_OCR_ENGINE
    try:
        from rapidocr import RapidOCR
    except ImportError as exc:
        raise DocumentFormatError(
            "扫描 PDF 需要 OCR，请先运行: py -3 -m pip install -r requirements.txt"
        ) from exc
    try:
        _PDF_OCR_ENGINE = RapidOCR()
    except Exception as exc:
        raise DocumentFormatError("无法初始化 PDF OCR 引擎") from exc
    return _PDF_OCR_ENGINE


def _pdf_ocr_cache_path(data):
    path = _binary_path(data)
    # OCR sidecar 只属于应用内部随机命名的原文缓存，绝不在用户原文件旁
    # 创建隐藏数据。
    if not path or not re.fullmatch(r"[0-9a-f]{32}\.pdf", os.path.basename(path)):
        return None
    return path + ".ocr.json"


def _pdf_load_ocr_cache(data):
    source_path = _binary_path(data)
    cache_path = _pdf_ocr_cache_path(data)
    if not source_path or not cache_path:
        return {"pages": {}}
    try:
        stat = os.stat(source_path)
        with open(cache_path, "r", encoding="utf-8") as handle:
            cached = json.load(handle)
        if (
            not isinstance(cached, dict)
            or cached.get("version") != PDF_OCR_CACHE_VERSION
            or cached.get("source_size") != stat.st_size
            or cached.get("source_mtime_ns") != stat.st_mtime_ns
            or not isinstance(cached.get("pages"), dict)
        ):
            return {"pages": {}}
        return cached
    except (OSError, ValueError, TypeError):
        return {"pages": {}}


def _pdf_save_ocr_cache(data, cache):
    source_path = _binary_path(data)
    cache_path = _pdf_ocr_cache_path(data)
    if not source_path or not cache_path:
        return
    try:
        stat = os.stat(source_path)
        payload = {
            "version": PDF_OCR_CACHE_VERSION,
            "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "pages": cache.get("pages", {}),
        }
        temporary = cache_path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, cache_path)
    except OSError:
        # OCR 结果本身仍可使用；缓存失败不应让本次翻译失败。
        with contextlib.suppress(OSError):
            os.remove(cache_path + ".tmp")


def _pdf_ocr_background_color(image, left, top, right, bottom):
    """从文字框边缘估算底色，避免用固定白块破坏彩色扫描页。"""
    try:
        import numpy as np

        image_height, image_width = image.shape[:2]
        x0 = max(0, min(image_width - 1, int(left)))
        x1 = max(x0 + 1, min(image_width, int(right) + 1))
        y0 = max(0, min(image_height - 1, int(top)))
        y1 = max(y0 + 1, min(image_height, int(bottom) + 1))
        pad = max(2, min(12, (y1 - y0) // 3))
        samples = []
        if y0 > 0:
            samples.append(image[max(0, y0 - pad):y0, x0:x1])
        if y1 < image_height:
            samples.append(image[y1:min(image_height, y1 + pad), x0:x1])
        if x0 > 0:
            samples.append(image[y0:y1, max(0, x0 - pad):x0])
        if x1 < image_width:
            samples.append(image[y0:y1, x1:min(image_width, x1 + pad)])
        pixels = [sample.reshape(-1, sample.shape[-1]) for sample in samples if sample.size]
        if not pixels:
            pixels = [image[y0:y1, x0:x1].reshape(-1, image.shape[-1])]
        median = np.median(np.concatenate(pixels, axis=0), axis=0)
        return tuple(round(float(component) / 255.0, 5) for component in median[:3])
    except Exception:
        return (1.0, 1.0, 1.0)


def _pdf_ocr_units_from_result(result, image, page_width, page_height):
    boxes = getattr(result, "boxes", None)
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    if boxes is None or texts is None:
        return []
    if scores is None:
        scores = [1.0] * len(texts)
    image_height, image_width = image.shape[:2]
    raw_lines = []
    for polygon, raw_text, score in zip(boxes, texts, scores):
        text = _normal_text(str(raw_text or ""))
        compact = re.sub(r"\s+", "", text)
        try:
            confidence = float(score)
            xs = [float(point[0]) for point in polygon]
            ys = [float(point[1]) for point in polygon]
        except (TypeError, ValueError, IndexError):
            continue
        # 孤立艺术字母通常来自封面、徽标或装饰，不作为翻译单元。
        if (
            confidence < PDF_OCR_MIN_SCORE
            or not compact
            or (len(compact) == 1 and compact.isalpha())
            or not re.search(r"[A-Za-z\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", compact)
        ):
            continue
        pixel_left, pixel_right = min(xs), max(xs)
        pixel_top, pixel_bottom = min(ys), max(ys)
        if (
            pixel_right - pixel_left < 2
            or pixel_bottom - pixel_top < 2
            or pixel_bottom - pixel_top > (pixel_right - pixel_left) * 1.8
        ):
            continue
        raw_lines.append({
            "text": text,
            "x0": max(0.0, pixel_left / image_width * page_width),
            "x1": min(page_width, pixel_right / image_width * page_width),
            "top": max(0.0, pixel_top / image_height * page_height),
            "bottom": min(page_height, pixel_bottom / image_height * page_height),
            "pixel_bounds": [pixel_left, pixel_top, pixel_right, pixel_bottom],
            "confidence": confidence,
        })
    if not raw_lines:
        return []

    ordered = _pdfplumber_order_boxes(raw_lines, page_width, page_height)
    groups = []
    for line in ordered:
        reading = line.get("_translation_reading") or {}
        previous = groups[-1] if groups else None
        previous_reading = previous.get("reading") if previous else {}
        gap = (
            float(line["top"]) - float(previous["bottom"])
            if previous else page_height
        )
        line_height = max(1.0, float(line["bottom"]) - float(line["top"]))
        same_flow = bool(previous) and (
            reading.get("region") == previous_reading.get("region")
            and reading.get("column") == previous_reading.get("column")
            and gap <= max(line_height * 1.35, page_height * 0.012)
            and abs(float(line["x0"]) - float(previous["x0"])) <= page_width * 0.06
            and not _pdf_terminal_line(previous["text"])
            and not _pdf_bullet_like(line["text"])
            and not _pdf_heading_like(line["text"])
        )
        if same_flow:
            if previous["text"].endswith("-") and line["text"][:1].isalnum():
                previous["text"] = previous["text"][:-1] + line["text"]
            else:
                previous["text"] += " " + line["text"]
            previous["x0"] = min(previous["x0"], line["x0"])
            previous["x1"] = max(previous["x1"], line["x1"])
            previous["top"] = min(previous["top"], line["top"])
            previous["bottom"] = max(previous["bottom"], line["bottom"])
            bounds = previous["pixel_bounds"]
            current = line["pixel_bounds"]
            previous["pixel_bounds"] = [
                min(bounds[0], current[0]), min(bounds[1], current[1]),
                max(bounds[2], current[2]), max(bounds[3], current[3]),
            ]
            previous["erase_pixel_bounds"].append(list(current))
            continue
        groups.append({
            "text": line["text"],
            "x0": line["x0"],
            "x1": line["x1"],
            "top": line["top"],
            "bottom": line["bottom"],
            "pixel_bounds": list(line["pixel_bounds"]),
            "erase_pixel_bounds": [list(line["pixel_bounds"])],
            "reading": dict(reading),
        })

    # 合并后重新计算区域/栏位中的位置，供跨栏续句判断使用。
    groups = _pdfplumber_order_boxes(groups, page_width, page_height)
    units = []
    for group in groups:
        pixel_left, pixel_top, pixel_right, pixel_bottom = group["pixel_bounds"]
        erase_boxes = []
        backgrounds = []
        for erase_left, erase_top, erase_right, erase_bottom in group["erase_pixel_bounds"]:
            background = _pdf_ocr_background_color(
                image, erase_left, erase_top, erase_right, erase_bottom
            )
            backgrounds.append(background)
            erase_boxes.append({
                "left": max(0.0, erase_left / image_width * page_width),
                "right": min(page_width, erase_right / image_width * page_width),
                "top": min(page_height, page_height - erase_top / image_height * page_height),
                "bottom": max(0.0, page_height - erase_bottom / image_height * page_height),
                "color": background,
            })
        background = backgrounds[0] if backgrounds else (1.0, 1.0, 1.0)
        luminance = 0.2126 * background[0] + 0.7152 * background[1] + 0.0722 * background[2]
        font_size = max(
            5.5,
            min(72.0, (float(group["bottom"]) - float(group["top"])) * 0.72),
        )
        units.append({
            "text": _normal_text(group["text"]),
            "heading": _pdf_heading_like(group["text"]),
            "box": {
                "left": max(0.0, float(group["x0"])),
                "right": min(page_width, float(group["x1"])),
                "top": min(page_height, page_height - float(group["top"])),
                "bottom": max(0.0, page_height - float(group["bottom"])),
                "font_size": font_size,
                "color": (0.06, 0.06, 0.06) if luminance >= 0.55 else (0.96, 0.96, 0.96),
                "erase_boxes": erase_boxes,
            },
            "reading": dict(group.get("_translation_reading") or {}),
            "ocr": True,
        })
    return [unit for unit in units if unit["text"]]


def _pdf_fill_ocr_pages(data, page_infos):
    targets = [page for page in page_infos if not page.get("units")]
    if not targets:
        return
    cache = _pdf_load_ocr_cache(data)
    cached_pages = cache.setdefault("pages", {})
    missing = []
    for page_info in targets:
        cached = cached_pages.get(str(page_info["page_number"]))
        if isinstance(cached, list):
            page_info["units"] = copy.deepcopy(cached)
            page_info["geometry_source"] = "ocr"
            page_info["ocr"] = True
        else:
            missing.append(page_info)
    if not missing:
        return

    try:
        import numpy as np
        import pymupdf
    except ImportError as exc:
        raise DocumentFormatError(
            "扫描 PDF 需要 OCR，请先运行: py -3 -m pip install -r requirements.txt"
        ) from exc
    path = _binary_path(data)
    try:
        document = (
            pymupdf.open(path)
            if path is not None
            else pymupdf.open(stream=bytes(data), filetype="pdf")
        )
    except Exception as exc:
        raise DocumentFormatError("无法为扫描 PDF 打开 OCR 页面") from exc
    engine = _pdf_ocr_engine()
    try:
        for page_info in missing:
            page_number = page_info["page_number"]
            image = pixmap = result = None
            try:
                source_page = document[page_number - 1]
                page_width, page_height = page_info["size"]
                scale = min(
                    3.0,
                    PDF_OCR_MAX_IMAGE_DIMENSION / max(page_width, page_height, 1.0),
                )
                pixmap = source_page.get_pixmap(
                    matrix=pymupdf.Matrix(scale, scale),
                    colorspace=pymupdf.csRGB,
                    alpha=False,
                )
                image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                    pixmap.height, pixmap.width, pixmap.n
                )
                result = engine(image)
                units = _pdf_ocr_units_from_result(
                    result, image, page_width, page_height
                )
                page_info["units"] = units
                page_info["geometry_source"] = "ocr"
                page_info["ocr"] = True
                cached_pages[str(page_number)] = copy.deepcopy(units)
                _pdf_save_ocr_cache(data, cache)
            except DocumentFormatError:
                raise
            except Exception as exc:
                raise DocumentFormatError(
                    f"PDF 第 {page_number} 页 OCR 识别失败"
                ) from exc
            finally:
                del result, image, pixmap
                gc.collect()
    finally:
        document.close()


def _pdf_has_text_layer(data):
    """只探测文字层，不渲染图片；用于决定扫描 PDF 的首次选页策略。"""
    with _pdf_decode_limits():
        reader = _pdf_reader(data)
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                continue
            if re.search(r"[\w\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text):
                return True
    return False


def _pdf_document_units(
    data, page_start=None, page_end=None, page_selection=None,
    recognition_mode="auto",
):
    recognition_mode = normalize_pdf_recognition_mode(recognition_mode)
    with _pdf_decode_limits():
        reader = _pdf_reader(data)
        selected_pages = normalize_pdf_page_selection(
            len(reader.pages), page_selection, page_start, page_end
        )
        pages = []
        try:
            for page_number in selected_pages:
                page = reader.pages[page_number - 1]
                modes = _pdf_page_text_render_modes(page)
                preserve_art_text = modes == {7}
                visible_crop = _pdf_page_has_distinct_cropbox(page)
                # Render mode 7 creates a clipping path from glyph outlines.  The
                # apparent lettering is painted by later artwork, so deleting or
                # replacing the text operators would also damage the artwork.
                # A cropped spread must likewise not be extracted with pypdf,
                # because it includes the hidden half outside CropBox.
                page_text = (
                    ""
                    if recognition_mode == "ocr" or preserve_art_text or visible_crop
                    else _pdf_page_text(page)
                )
                units = [] if not page_text else _pdf_page_units(page, page_text)
                if units:
                    units = _pdf_units_with_repaired_word_spacing(page, units)
                pages.append({
                    "page_number": page_number,
                    "size": _pdf_page_size(page),
                    "units": units,
                    "toc_source_text": page_text if _pdf_probable_toc_rows(page_text) else None,
                    "text_render_modes": tuple(sorted(modes)),
                    "preserve_art_text": preserve_art_text,
                    "visible_crop": visible_crop,
                })
        except DocumentFormatError:
            raise
        except Exception as exc:
            raise DocumentFormatError("PDF 页面结构读取失败") from exc
    # 简单页面继续使用 pypdf 的成熟文本单元；遇到 Form、多栏或内容流
    # 顺序无法可靠映射的页面，改由 pdfplumber 同时产生文本块和几何框。
    # 这样预览、翻译和回填共享同一组块，不再事后猜测位置。
    advanced_pages = []
    for page_info in pages:
        if recognition_mode == "ocr":
            continue
        source_page = reader.pages[page_info["page_number"] - 1]
        width, height = page_info["size"]
        if page_info.get("preserve_art_text"):
            continue
        if page_info.get("visible_crop"):
            advanced_pages.append(page_info)
            continue
        if page_info.get("toc_source_text"):
            advanced_pages.append(page_info)
            continue
        if not _pdf_page_origin_is_supported(source_page):
            page_info["force_reflow"] = True
            continue
        try:
            with _pdf_decode_limits():
                modes = set(page_info.get("text_render_modes") or (0,))
                annotation_text = _pdf_page_has_annotation_text(source_page)
        except _PDFLayoutFallback:
            page_info["force_reflow"] = True
            continue
        if annotation_text or any(mode not in {0, 1, 2} for mode in modes):
            page_info["force_reflow"] = True
            continue
        if _pdf_units_look_artificially_fragmented(page_info["units"]):
            advanced_pages.append(page_info)
            continue
        try:
            with _pdf_decode_limits():
                if _pdf_page_has_external_text(source_page):
                    raise _PDFLayoutFallback
                mapped = _pdf_map_positioned_units(
                    source_page, page_info["units"], width, height
                )
                _pdf_unit_boxes(mapped, width, height)
        except _PDFLayoutFallback:
            advanced_pages.append(page_info)

    if advanced_pages:
        try:
            import pdfplumber
        except ImportError as exc:
            raise DocumentFormatError(
                "复杂 PDF 版面支持尚未安装，请先运行: py -3 -m pip install -r requirements.txt"
            ) from exc
        try:
            source_path = _binary_path(data)
            plumber_source = (
                source_path if source_path is not None else io.BytesIO(bytes(data))
            )
            with pdfplumber.open(
                plumber_source,
                laparams={"boxes_flow": -0.5, "detect_vertical": True},
                unicode_norm="NFC",
            ) as plumber:
                for page_info in advanced_pages:
                    plumber_page = plumber.pages[page_info["page_number"] - 1]
                    visible_bounds = None
                    if page_info.get("visible_crop"):
                        source_page = reader.pages[page_info["page_number"] - 1]
                        visible_bounds = _pdf_page_visible_bounds(source_page)
                    replacement = None
                    if page_info.get("toc_source_text"):
                        replacement = _pdfplumber_toc_units(
                            plumber_page,
                            page_info["toc_source_text"],
                            visible_bounds=visible_bounds,
                        )
                    if not replacement:
                        replacement = _pdfplumber_page_units(
                            plumber_page, visible_bounds=visible_bounds
                        )
                    if replacement:
                        page_info["units"] = replacement
                        page_info["geometry_source"] = "pdfplumber"
                    else:
                        page_info["force_reflow"] = True
        except DocumentFormatError:
            raise
        except Exception as exc:
            raise DocumentFormatError("复杂 PDF 页面结构读取失败") from exc

    # 没有文字层的页按页 OCR。已有文字层的页继续走原生提取，避免 OCR
    # 改坏可靠文本；纯插图页识别不到文字时保持空单元并在输出中原样保留。
    ocr_pages = [] if recognition_mode == "text" else [
        page for page in pages
        if (recognition_mode == "ocr" or not page["units"])
        and (
            recognition_mode == "ocr"
            or not page.get("preserve_art_text")
        )
    ]
    if ocr_pages:
        _pdf_fill_ocr_pages(data, ocr_pages)
    return pages


def _pdf_units_text(pages):
    return "\n".join(
        unit["text"] for page in pages for unit in page["units"]
    )


def _pdf_sentence_ends(text):
    return bool(re.search(
        r"[。！？.!?…]\s*[\"'”’」』）)\]]*$", str(text or "").rstrip()
    ))


def _pdf_text_may_continue(previous_text, current_text):
    """Return a conservative linguistic hint, never a definitive boundary."""
    previous = str(previous_text or "").strip()
    current = str(current_text or "").lstrip()
    if not previous or not current or _pdf_sentence_ends(previous):
        return False
    current_word = re.sub(r"^[\"'“”‘’([{]+", "", current)
    if re.match(r"[a-z\u00e0-\u00f6\u00f8-\u00ff]", current_word):
        return True
    if previous.endswith(("-", "–", "—", ",", ";")):
        return True
    previous_words = re.findall(r"[A-Za-z]+", previous.lower())
    return bool(previous_words and previous_words[-1] in {
        "and", "or", "but", "because", "although", "while", "when", "if",
        "to", "of", "for", "from", "with", "without", "the", "a", "an",
        "this", "that", "these", "those", "very", "more", "most",
        "immediately", "directly", "particularly", "especially",
    })


def _pdf_layout_confirms_continuation(previous, current):
    """Confirm a flow from the end of one column to the top of the next."""
    previous_reading = previous.get("reading") or {}
    current_reading = current.get("reading") or {}
    if not previous_reading or not current_reading:
        return False
    try:
        same_region = (
            int(previous_reading["region"]) == int(current_reading["region"])
        )
        next_column = (
            int(current_reading["column"])
            == int(previous_reading["column"]) + 1
        )
        previous_is_last = (
            int(previous_reading["position"])
            == int(previous_reading["column_units"]) - 1
        )
        current_is_first = int(current_reading["position"]) == 0
        multi_column = min(
            int(previous_reading["column_count"]),
            int(current_reading["column_count"]),
        ) >= 2
        previous_size = float((previous.get("box") or {})["font_size"])
        current_size = float((current.get("box") or {})["font_size"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    size_difference = abs(previous_size - current_size) / max(
        previous_size, current_size, 1.0
    )
    return bool(
        same_region and next_column and previous_is_last and current_is_first
        and multi_column and size_difference <= 0.18
    )


def _pdf_continuation_hints(pages):
    """Return one internal boundary hint per flattened PDF translation unit."""
    hints = []
    previous = None
    previous_page_number = None
    for page in pages:
        page_number = int(page.get("page_number") or 0)
        for current in page.get("units", []):
            hint = "separate"
            if previous is not None:
                consecutive_page = (
                    page_number == previous_page_number
                    or page_number == previous_page_number + 1
                )
                structural_break = (
                    current.get("heading") or previous.get("heading")
                    or current.get("toc_page_label") is not None
                    or previous.get("toc_page_label") is not None
                    or _pdf_bullet_like(current.get("text"))
                )
                previous_reading = previous.get("reading") or {}
                current_reading = current.get("reading") or {}
                different_regions = (
                    page_number == previous_page_number
                    and previous_reading and current_reading
                    and previous_reading.get("region") != current_reading.get("region")
                )
                if (
                    consecutive_page and not structural_break
                    and not different_regions
                    and not _pdf_sentence_ends(previous.get("text"))
                    and _pdf_layout_confirms_continuation(previous, current)
                ):
                    hint = "strong"
                elif (
                    consecutive_page and not structural_break
                    and not different_regions
                    and _pdf_text_may_continue(
                        previous.get("text"), current.get("text")
                    )
                ):
                    hint = "possible"
            hints.append(hint)
            previous = current
            previous_page_number = page_number
    return hints


def extract_pdf_translation_data(
    data, page_start=None, page_end=None, page_selection=None,
    recognition_mode="auto",
):
    """Extract PDF text plus internal, line-aligned continuation hints."""
    recognition_mode = normalize_pdf_recognition_mode(recognition_mode)
    pages = _pdf_document_units(
        data, page_start, page_end, page_selection, recognition_mode
    )
    return {
        "content": _pdf_units_text(pages),
        "continuation_hints": _pdf_continuation_hints(pages),
        "used_ocr": any(page.get("geometry_source") == "ocr" for page in pages),
        "recognition_mode": recognition_mode,
    }


def _extract_pdf(
    data, page_start=None, page_end=None, page_selection=None,
    recognition_mode="auto",
):
    return extract_pdf_translation_data(
        data, page_start, page_end, page_selection, recognition_mode
    )["content"]


def pdf_page_count(data):
    """读取并校验 PDF 页数，不解码各页内容流。"""
    with _pdf_decode_limits():
        return len(_pdf_reader(data).pages)


_PDF_REGISTERED_FONTS = {}


def _pdf_target_script(text, target_language):
    target = str(target_language or "")
    value = str(text or "")
    if target == "日文" or re.search(r"[\u3040-\u30ff]", value):
        return "japanese"
    if target == "韩文" or re.search(r"[\uac00-\ud7af]", value):
        return "korean"
    if target in {"繁体中文", "粤语"}:
        return "traditional_chinese"
    if target == "中文" or re.search(r"[\u3400-\u9fff]", value):
        return "simplified_chinese"
    return "unicode"


def _pdf_contains_rtl(text):
    return any(unicodedata.bidirectional(character) in {"R", "AL"} for character in text)


def _pdf_line_direction(text, target_language=""):
    """按首个强方向字符决定单行方向；纯数字行才回退目标语言。"""
    for character in str(text or ""):
        bidi = unicodedata.bidirectional(character)
        if bidi == "L":
            return "LTR"
        if bidi in {"R", "AL"}:
            return "RTL"
    if str(target_language or "") in {
        "乌尔都文", "阿拉伯文", "希伯来文", "波斯文", "维吾尔文"
    }:
        return "RTL"
    return "LTR"


def _pdf_contains_shaping_script(text):
    return bool(re.search(
        r"[\u0600-\u08ff\u0900-\u0dff\u0e00-\u0fff"
        r"\u1000-\u109f\u1780-\u18af]",
        str(text or ""),
    ))


def _pdf_cjk_font_path_candidates(script):
    windows = os.environ.get("WINDIR", r"C:\Windows")
    fonts = os.path.join(windows, "Fonts")
    names = {
        "simplified_chinese": (
            "NotoSansSC-VF.ttf", "msyh.ttc", "simhei.ttf", "simsun.ttc",
            "SimsunExtG.ttf",
        ),
        "traditional_chinese": (
            "NotoSansTC-VF.ttf", "NotoSansHK-VF.ttf", "msjh.ttc",
            "mingliu.ttc",
        ),
        "japanese": (
            "NotoSansJP-VF.ttf", "YuGothM.ttc", "meiryo.ttc", "msgothic.ttc",
        ),
        "korean": (
            "NotoSansKR-VF.ttf", "malgun.ttf", "gulim.ttc",
        ),
    }
    candidates = [os.path.join(fonts, name) for name in names.get(script, ())]
    linux_names = {
        "simplified_chinese": ("NotoSansCJKsc-Regular.otf", "NotoSansSC-Regular.otf"),
        "traditional_chinese": ("NotoSansCJKtc-Regular.otf", "NotoSansTC-Regular.otf"),
        "japanese": ("NotoSansCJKjp-Regular.otf", "NotoSansJP-Regular.otf"),
        "korean": ("NotoSansCJKkr-Regular.otf", "NotoSansKR-Regular.otf"),
    }
    for directory in (
        "/usr/share/fonts/opentype/noto",
        "/usr/share/fonts/truetype/noto",
        "/usr/local/share/fonts",
    ):
        candidates.extend(
            os.path.join(directory, name) for name in linux_names.get(script, ())
        )
        candidates.append(os.path.join(directory, "NotoSansCJK-Regular.ttc"))
    mac_names = {
        "simplified_chinese": ("PingFang.ttc",),
        "traditional_chinese": ("PingFang.ttc",),
        "japanese": ("Hiragino Sans GB.ttc",),
        "korean": ("AppleSDGothicNeo.ttc",),
    }
    for directory in ("/System/Library/Fonts", "/Library/Fonts"):
        candidates.extend(
            os.path.join(directory, name) for name in mac_names.get(script, ())
        )
    return list(dict.fromkeys(candidates))


def _pdf_font_path_candidates(complex_script=False, target_language=""):
    windows = os.environ.get("WINDIR", r"C:\Windows")
    fonts = os.path.join(windows, "Fonts")
    target_windows = {
        "泰文": ("LeelawUI.ttf", "tahoma.ttf"),
        "藏文": ("himalaya.ttf",),
        "蒙古文": ("monbaiti.ttf",),
        "缅甸文": ("mmrtext.ttf",),
        "阿拉伯文": ("tahoma.ttf", "arial.ttf"),
        "波斯文": ("tahoma.ttf", "arial.ttf"),
        "乌尔都文": ("Nirmala.ttf", "Nirmala.ttc"),
        "维吾尔文": ("tahoma.ttf", "arial.ttf"),
        "希伯来文": ("arial.ttf", "segoeui.ttf"),
    }
    complex_candidates = [
        os.path.join(fonts, "Nirmala.ttf"),
        os.path.join(fonts, "Nirmala.ttc"),
        os.path.join(fonts, "segoeui.ttf"),
        os.path.join(fonts, "tahoma.ttf"),
    ]
    common_candidates = [
        os.path.join(fonts, "segoeui.ttf"),
        os.path.join(fonts, "arial.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ]
    target_candidates = [
        os.path.join(fonts, name)
        for name in target_windows.get(str(target_language or ""), ())
    ]
    noto_names = {
        "泰文": ("NotoSansThai-Regular.ttf",),
        "藏文": ("NotoSansTibetan-Regular.ttf",),
        "蒙古文": ("NotoSansMongolian-Regular.ttf",),
        "缅甸文": ("NotoSansMyanmar-Regular.ttf",),
        "阿拉伯文": ("NotoNaskhArabic-Regular.ttf", "NotoSansArabic-Regular.ttf"),
        "波斯文": ("NotoNaskhArabic-Regular.ttf", "NotoSansArabic-Regular.ttf"),
        "乌尔都文": ("NotoNaskhArabic-Regular.ttf",),
        "维吾尔文": ("NotoNaskhArabic-Regular.ttf",),
        "希伯来文": ("NotoSansHebrew-Regular.ttf",),
    }
    for directory in (
        "/usr/share/fonts/truetype/noto",
        "/usr/share/fonts/opentype/noto",
        "/usr/local/share/fonts",
    ):
        target_candidates.extend(
            os.path.join(directory, name)
            for name in noto_names.get(str(target_language or ""), ())
        )
    return list(dict.fromkeys(
        target_candidates
        + (complex_candidates if complex_script else [])
        + common_candidates
    ))


def _pdf_register_ttf(path, pdfmetrics, TTFont):
    key = ("ttf", os.path.realpath(path), 0)
    name = _PDF_REGISTERED_FONTS.get(key)
    if name:
        return name
    name = "TranslationBenchFont" + str(len(_PDF_REGISTERED_FONTS) + 1)
    pdfmetrics.registerFont(TTFont(name, path, subfontIndex=0))
    _PDF_REGISTERED_FONTS[key] = name
    return name


def _pdf_font_covers(font, text):
    glyphs = getattr(getattr(font, "face", None), "charToGlyph", {})
    if not glyphs:
        return False
    return all(
        character.isspace()
        or ord(character) <= 31
        or bool(glyphs.get(ord(character), 0))
        for character in text
    )


def _pdf_output_font(text, target_language):
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except ImportError as exc:
        raise DocumentFormatError(
            "PDF 输出支持尚未安装，请先运行: py -3 -m pip install -r requirements.txt"
        ) from exc

    script = _pdf_target_script(text, target_language)
    if script in {
        "simplified_chinese", "traditional_chinese", "japanese", "korean"
    }:
        for path in _pdf_cjk_font_path_candidates(script):
            if not os.path.isfile(path):
                continue
            try:
                embedded_name = _pdf_register_ttf(path, pdfmetrics, TTFont)
            except Exception:
                continue
            if _pdf_font_covers(pdfmetrics.getFont(embedded_name), text):
                return embedded_name
        raise DocumentFormatError(
            "系统中没有覆盖全部译文字符的 CJK 字体，无法生成无缺字 PDF；"
            "请安装对应语言的 Noto Sans CJK 字体"
        )

    complex_script = _pdf_contains_shaping_script(text)
    for path in _pdf_font_path_candidates(complex_script, target_language):
        if not os.path.isfile(path):
            continue
        key = os.path.realpath(path)
        try:
            name = _pdf_register_ttf(key, pdfmetrics, TTFont)
        except Exception:
            continue
        if _pdf_font_covers(pdfmetrics.getFont(name), text):
            return name
    try:
        str(text or "").encode("cp1252")
    except UnicodeEncodeError:
        pass
    else:
        return "Helvetica"
    raise DocumentFormatError(
        "系统中没有覆盖当前译文字符的字体，无法生成可读 PDF；请安装对应语言的 Noto 字体"
    )


def _pdf_output_emphasis_font(text, target_language, regular_font):
    """尽量为标题选择可嵌入的粗体；缺失时安全回退正文字体。"""
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except ImportError:
        return regular_font

    windows = os.environ.get("WINDIR", r"C:\Windows")
    fonts = os.path.join(windows, "Fonts")
    script = _pdf_target_script(text, target_language)
    names = {
        "simplified_chinese": ("msyhbd.ttc", "simhei.ttf"),
        "traditional_chinese": ("msjhbd.ttc", "msjh.ttc"),
        "japanese": ("meiryob.ttc", "YuGothB.ttc"),
        "korean": ("malgunbd.ttf", "malgun.ttf"),
    }.get(script, ("seguisb.ttf", "arialbd.ttf"))
    candidates = [os.path.join(fonts, name) for name in names]
    candidates.extend((
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSans-Bold.ttf",
    ))
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            name = _pdf_register_ttf(path, pdfmetrics, TTFont)
            if _pdf_font_covers(pdfmetrics.getFont(name), text):
                return name
        except Exception:
            continue
    if regular_font == "Helvetica":
        return "Helvetica-Bold"
    return regular_font


def _pdf_wrap_line(text, font_name, font_size, width, pdfmetrics):
    value = str(text or "").strip()
    if not value:
        return [""]
    tokens = re.findall(r"\S+\s*", value) or [value]
    lines = []
    current = ""

    def split_oversized(token):
        pieces = []
        piece = ""
        for character in token:
            candidate = piece + character
            if piece and pdfmetrics.stringWidth(candidate, font_name, font_size) > width:
                pieces.append(piece.rstrip())
                piece = character.lstrip()
            else:
                piece = candidate
        if piece:
            pieces.append(piece.rstrip())
        return pieces or [""]

    for token in tokens:
        candidate = current + token
        if current and pdfmetrics.stringWidth(candidate, font_name, font_size) > width:
            lines.append(current.rstrip())
            current = ""
        if pdfmetrics.stringWidth(token, font_name, font_size) > width:
            pieces = split_oversized(token)
            if current:
                lines.append(current.rstrip())
            lines.extend(pieces[:-1])
            current = pieces[-1]
        else:
            current += token
    if current or not lines:
        lines.append(current.rstrip())
    return lines


class _PDFLayoutFallback(Exception):
    """原位替换无法被严格验证；是否允许重排由调用方决定。"""


_PDF_TEXT_SHOW_OPERATORS = {b"Tj", b"TJ", b"'", b'"'}


def _pdf_matrix_multiply(left, right):
    """合并 PDF 的文本矩阵与当前变换矩阵。"""
    return (
        left[0] * right[0] + left[1] * right[2],
        left[0] * right[1] + left[1] * right[3],
        left[2] * right[0] + left[3] * right[2],
        left[2] * right[1] + left[3] * right[3],
        left[4] * right[0] + left[5] * right[2] + right[4],
        left[4] * right[1] + left[5] * right[3] + right[5],
    )


def _pdf_layout_key(text):
    value = _normal_text(str(text or "").replace("\u00ad", ""))
    return "".join(character for character in value if not character.isspace())


def _pdf_join_fragment_text(fragments):
    combined = ""
    for fragment in fragments:
        value = fragment["text"]
        if combined.endswith("-") and value[:1].isalnum():
            combined = combined[:-1] + value
        elif combined:
            combined += " " + value
        else:
            combined = value
    return _normal_text(combined)


def _pdf_positioned_fragments(page, width, height):
    fragments = []
    fill_color = [0.0, 0.0, 0.0]
    color_stack = []

    def operand_before(operator, operands, _cm_matrix, _tm_matrix):
        nonlocal fill_color
        try:
            if operator == b"Tr" and operands and int(operands[0]) != 0:
                # 描边、隐形或裁剪文字可能是 OCR/特效层，不能当作
                # 普通可见文字直接覆盖。
                raise _PDFLayoutFallback
            if operator == b"Ts" and operands and abs(float(operands[0])) > 0.01:
                # 上下标的视觉坐标不能由 visitor 矩阵稳定还原。
                raise _PDFLayoutFallback
            if operator == b"q":
                color_stack.append(tuple(fill_color))
            elif operator == b"Q":
                fill_color = list(color_stack.pop()) if color_stack else [0.0, 0.0, 0.0]
            elif operator == b"g" and operands:
                gray = min(1.0, max(0.0, float(operands[0])))
                fill_color = [gray, gray, gray]
            elif operator == b"rg" and len(operands) >= 3:
                fill_color = [
                    min(1.0, max(0.0, float(component)))
                    for component in operands[:3]
                ]
            elif operator == b"k" and len(operands) >= 4:
                cyan, magenta, yellow, black = (
                    min(1.0, max(0.0, float(component)))
                    for component in operands[:4]
                )
                fill_color = [
                    1.0 - min(1.0, cyan + black),
                    1.0 - min(1.0, magenta + black),
                    1.0 - min(1.0, yellow + black),
                ]
        except _PDFLayoutFallback:
            raise
        except (TypeError, ValueError, IndexError, OverflowError):
            # 颜色指令不标准时仍可以依靠默认黑色输出，不影响定位安全性。
            fill_color = [0.0, 0.0, 0.0]

    def visit(text, cm_matrix, tm_matrix, _font, font_size):
        value = _normal_text(str(text or "").replace("\x00", ""))
        if not value:
            return
        try:
            matrix = _pdf_matrix_multiply(tm_matrix, cm_matrix)
            x, y = float(matrix[4]), float(matrix[5])
            x_scale = (float(matrix[0]) ** 2 + float(matrix[1]) ** 2) ** 0.5
            y_scale = (float(matrix[2]) ** 2 + float(matrix[3]) ** 2) ** 0.5
            size = abs(float(font_size)) * max(y_scale, 0.01)
        except (TypeError, ValueError, IndexError, OverflowError) as exc:
            raise _PDFLayoutFallback from exc
        # 仅对水平、未镜像的文字做原位替换。旋转或斜切文字交给重排版，
        # 以免看似成功却实际覆盖错位。
        tolerance = max(x_scale, y_scale, 1.0) * 1e-4
        if (
            matrix[0] <= 0
            or matrix[3] <= 0
            or abs(matrix[1]) > tolerance
            or abs(matrix[2]) > tolerance
            or not (0.0 <= x <= width and -size <= y <= height + size)
            or not (0.5 <= size <= 200.0)
        ):
            raise _PDFLayoutFallback
        fragments.append({
            "text": value,
            "x": x,
            "y": y,
            "size": size,
            "color": tuple(fill_color),
        })

    try:
        page.extract_text(visitor_operand_before=operand_before, visitor_text=visit)
    except _PDFLayoutFallback:
        raise
    except Exception as exc:
        raise _PDFLayoutFallback from exc
    return fragments


def _pdf_map_positioned_units(page, page_units, width, height):
    fragments = _pdf_positioned_fragments(page, width, height)
    mapped = []
    cursor = 0
    for unit in page_units:
        target = _pdf_layout_key(unit["text"])
        group = []
        candidate = ""
        while cursor < len(fragments):
            group.append(fragments[cursor])
            cursor += 1
            candidate = _pdf_layout_key(_pdf_join_fragment_text(group))
            if candidate == target:
                break
            if not target.startswith(candidate):
                raise _PDFLayoutFallback
        if not target or candidate != target:
            raise _PDFLayoutFallback
        mapped.append({"unit": unit, "fragments": group})
    if cursor != len(fragments):
        raise _PDFLayoutFallback
    return mapped


def _pdf_direct_object(value):
    try:
        return value.get_object()
    except AttributeError:
        return value


def _pdf_object_identity(value):
    reference = getattr(value, "indirect_reference", None)
    if reference is None and hasattr(value, "idnum"):
        reference = value
    if reference is not None and hasattr(reference, "idnum"):
        return (int(reference.idnum), int(getattr(reference, "generation", 0)))
    return ("direct", id(value))


def _pdf_stream_contains_text(stream, pdf):
    try:
        from pypdf.generic import ContentStream

        parsed = ContentStream(stream, pdf)
        return any(operator in _PDF_TEXT_SHOW_OPERATORS for _, operator in parsed.operations)
    except Exception as exc:
        raise _PDFLayoutFallback from exc


def _pdf_resources_contain_external_text(resources, pdf, seen):
    resources = _pdf_direct_object(resources)
    if not resources:
        return False
    for resource_name in ("/XObject", "/Pattern"):
        collection = _pdf_direct_object(resources.get(resource_name))
        if not collection:
            continue
        for reference in collection.values():
            identity = _pdf_object_identity(reference)
            if identity in seen:
                continue
            seen.add(identity)
            item = _pdf_direct_object(reference)
            if not hasattr(item, "get_data"):
                continue
            subtype = str(item.get("/Subtype", ""))
            if resource_name == "/XObject" and subtype != "/Form":
                continue
            if _pdf_stream_contains_text(item, pdf):
                return True
            if _pdf_resources_contain_external_text(item.get("/Resources"), pdf, seen):
                return True
    return False


def _pdf_appearance_contains_text(value, pdf, seen):
    identity = _pdf_object_identity(value)
    if identity in seen:
        return False
    seen.add(identity)
    item = _pdf_direct_object(value)
    if hasattr(item, "get_data"):
        if _pdf_stream_contains_text(item, pdf):
            return True
        return _pdf_resources_contain_external_text(item.get("/Resources"), pdf, seen)
    if hasattr(item, "values"):
        return any(
            _pdf_appearance_contains_text(child, pdf, seen)
            for child in item.values()
        )
    return False


def _pdf_page_has_external_text(page):
    pdf = getattr(page, "pdf", None)
    seen = set()
    if _pdf_resources_contain_external_text(page.get("/Resources"), pdf, seen):
        return True
    annotations = _pdf_direct_object(page.get("/Annots")) or []
    for reference in annotations:
        annotation = _pdf_direct_object(reference)
        appearance = annotation.get("/AP") if hasattr(annotation, "get") else None
        if appearance and _pdf_appearance_contains_text(appearance, pdf, seen):
            return True
    return False


def _pdf_page_has_annotation_text(page):
    pdf = getattr(page, "pdf", None)
    seen = set()
    annotations = _pdf_direct_object(page.get("/Annots")) or []
    for reference in annotations:
        annotation = _pdf_direct_object(reference)
        appearance = annotation.get("/AP") if hasattr(annotation, "get") else None
        if appearance and _pdf_appearance_contains_text(appearance, pdf, seen):
            return True
    return False


def _pdf_page_text_render_modes(page):
    """Return render modes actually used by text-show operations on the page."""
    modes = set()
    current = 0
    stack = []
    try:
        contents = page.get_contents()
        if contents is None:
            return {0}
        for operands, operator in contents.operations:
            if operator == b"q":
                stack.append(current)
            elif operator == b"Q":
                current = stack.pop() if stack else 0
            elif operator == b"Tr" and operands:
                try:
                    current = int(operands[0])
                except (TypeError, ValueError, IndexError, OverflowError):
                    current = -1
            elif operator in _PDF_TEXT_SHOW_OPERATORS:
                modes.add(current)
    except Exception as exc:
        raise _PDFLayoutFallback from exc
    return modes or {0}


def _pdf_block_text(value):
    value = "".join(
        character
        if character in "\t\r\n" or (
            character != "\ufffd" and unicodedata.category(character) != "Cc"
        )
        else " "
        for character in str(value or "")
    )
    lines = [
        _normal_text(line)
        for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if _normal_text(line)
    ]
    combined = ""
    for line in lines:
        if combined.endswith("-") and line[:1].isalnum():
            combined = combined[:-1] + line
        elif combined:
            combined += " " + line
        else:
            combined = line
    return _normal_text(combined.replace("\u00ad", ""))


def _pdf_rgb_color(value):
    try:
        components = [float(component) for component in value]
    except (TypeError, ValueError, OverflowError):
        return (0.08, 0.08, 0.08)
    if len(components) == 1:
        gray = min(1.0, max(0.0, components[0]))
        return (gray, gray, gray)
    if len(components) == 3:
        return tuple(min(1.0, max(0.0, component)) for component in components)
    if len(components) == 4:
        cyan, magenta, yellow, black = (
            min(1.0, max(0.0, component)) for component in components
        )
        return (
            1.0 - min(1.0, cyan + black),
            1.0 - min(1.0, magenta + black),
            1.0 - min(1.0, yellow + black),
        )
    return (0.08, 0.08, 0.08)


def _pdfplumber_chars_in_box(page, box):
    width, height = float(page.width), float(page.height)
    return [
        char for char in page.chars
        if float(char.get("x0", 0)) >= float(box.get("x0", 0)) - 0.5
        and float(char.get("x1", 0)) <= float(box.get("x1", width)) + 0.5
        and float(char.get("top", 0)) >= float(box.get("top", 0)) - 0.5
        and float(char.get("bottom", height)) <= float(box.get("bottom", height)) + 0.5
    ]


def _pdfplumber_dominant_color(chars):
    colors = {}
    for char in chars:
        color = _pdf_rgb_color(char.get("non_stroking_color"))
        key = tuple(round(component, 4) for component in color)
        colors[key] = colors.get(key, 0) + 1
    if not colors:
        return (0.08, 0.08, 0.08)
    return max(colors.items(), key=lambda item: item[1])[0]


def _pdfplumber_unit_box(page, source):
    width, height = float(page.width), float(page.height)
    chars = _pdfplumber_chars_in_box(page, source)
    sizes = sorted(
        float(char.get("size", 10.5)) for char in chars
        if 0.5 <= float(char.get("size", 0) or 0) <= 200
    )
    font_size = sizes[len(sizes) // 2] if sizes else 10.5
    left = max(0.0, float(source.get("x0", 0)))
    right = min(width, float(source.get("x1", width)))
    top = min(height, height - float(source.get("top", 0)) + font_size * 0.2)
    bottom = max(
        0.0, height - float(source.get("bottom", height)) - font_size * 0.35
    )
    if right - left < 3 or top - bottom < 4:
        return None
    return {
        "left": left,
        "right": right,
        "top": top,
        "bottom": bottom,
        "font_size": min(36.0, max(5.5, font_size)),
        # A textbox may start with a coloured heading but contain mostly dark
        # body text.  The dominant colour is a much better representation than
        # blindly copying the first glyph's colour.
        "color": _pdfplumber_dominant_color(chars),
    }


def _pdfplumber_order_boxes(boxes, width, height):
    footers = []
    regular = []
    for box in boxes:
        text = _pdf_block_text(box.get("text"))
        if (
            float(box.get("top", 0)) > height * 0.88
            and len(text) <= 8
            and not re.search(r"[A-Za-z\u3400-\u9fff]", text)
        ):
            footers.append(box)
        else:
            regular.append(box)

    spanning = [
        box for box in regular
        if (
            float(box.get("x0", 0)) < width * 0.45
            and float(box.get("x1", width)) > width * 0.55
            and float(box.get("x1", width)) - float(box.get("x0", 0)) > width * 0.18
        )
    ]
    spanning_ids = {id(box) for box in spanning}
    remaining = [box for box in regular if id(box) not in spanning_ids]

    region_sequence = 0

    def column_order(items, region):
        if not items:
            return []
        clusters = []
        for box in sorted(items, key=lambda item: float(item.get("x0", 0))):
            x = float(box.get("x0", 0))
            if not clusters or abs(x - clusters[-1][0]) > width * 0.12:
                clusters.append([x, [box]])
            else:
                clusters[-1][1].append(box)
                clusters[-1][0] = sum(
                    float(item.get("x0", 0)) for item in clusters[-1][1]
                ) / len(clusters[-1][1])
        ordered = []
        column_count = len(clusters)
        for column, (_, cluster) in enumerate(clusters):
            column_items = sorted(
                cluster,
                key=lambda item: (
                    float(item.get("top", 0)), float(item.get("x0", 0))
                ),
            )
            for position, item in enumerate(column_items):
                item["_translation_reading"] = {
                    "region": region,
                    "column": column,
                    "column_count": column_count,
                    "position": position,
                    "column_units": len(column_items),
                }
            ordered.extend(column_items)
        return ordered

    def regional_order(items):
        """Split vertically separated panels before applying column reading order."""
        nonlocal region_sequence
        if not items:
            return []
        gap_tolerance = max(6.0, height * 0.015)
        bands = []
        current = []
        current_bottom = None
        for box in sorted(
            items,
            key=lambda item: (
                float(item.get("top", 0)), float(item.get("x0", 0))
            ),
        ):
            top = float(box.get("top", 0))
            bottom = max(top, float(box.get("bottom", top)))
            if (
                current
                and current_bottom is not None
                and top > current_bottom + gap_tolerance
            ):
                bands.append(current)
                current = []
                current_bottom = None
            current.append(box)
            current_bottom = (
                bottom if current_bottom is None else max(current_bottom, bottom)
            )
        if current:
            bands.append(current)

        ordered = []
        for band in bands:
            ordered.extend(column_order(band, region_sequence))
            region_sequence += 1
        return ordered

    ordered = []
    for span in sorted(spanning, key=lambda item: float(item.get("top", 0))):
        before = [
            box for box in remaining
            if float(box.get("top", 0)) < float(span.get("top", 0))
        ]
        ordered.extend(regional_order(before))
        before_ids = {id(box) for box in before}
        remaining = [box for box in remaining if id(box) not in before_ids]
        span["_translation_reading"] = {
            "region": region_sequence,
            "column": 0,
            "column_count": 1,
            "position": 0,
            "column_units": 1,
            "spanning": True,
        }
        region_sequence += 1
        ordered.append(span)
    ordered.extend(regional_order(remaining))
    ordered_footers = sorted(
        footers,
        key=lambda item: (float(item.get("top", 0)), float(item.get("x0", 0))),
    )
    for position, footer in enumerate(ordered_footers):
        footer["_translation_reading"] = {
            "region": region_sequence,
            "column": 0,
            "column_count": 1,
            "position": position,
            "column_units": len(ordered_footers),
            "footer": True,
        }
    ordered.extend(ordered_footers)
    return ordered


def _pdfplumber_box_is_visible(box, visible_bounds):
    if visible_bounds is None:
        return True
    left, top, right, bottom = visible_bounds
    try:
        center_x = (float(box.get("x0", 0)) + float(box.get("x1", 0))) / 2.0
        center_y = (float(box.get("top", 0)) + float(box.get("bottom", 0))) / 2.0
    except (TypeError, ValueError, OverflowError):
        return False
    return left <= center_x <= right and top <= center_y <= bottom


def _pdfplumber_page_units(page, visible_bounds=None):
    width, height = float(page.width), float(page.height)
    boxes = [
        box for box in page.objects.get("textboxhorizontal", [])
        if _pdfplumber_box_is_visible(box, visible_bounds)
    ]
    units = []
    for box in _pdfplumber_order_boxes(boxes, width, height):
        text = _pdf_block_text(box.get("text"))
        if not text:
            continue
        unit_box = _pdfplumber_unit_box(page, box)
        if unit_box is None:
            continue
        units.append({
            "text": text,
            "heading": _pdf_heading_like(text),
            "box": unit_box,
            "reading": dict(box.get("_translation_reading") or {}),
        })
    return units


def _pdfplumber_toc_units(page, source_text, visible_bounds=None):
    """用 pypdf 的干净目录文字配对 pdfplumber 的逐行坐标。"""
    rows = _pdf_probable_toc_rows(source_text)
    if not rows:
        return []
    raw_line_boxes = sorted(
        (
            source for source in page.objects.get("textlinehorizontal", [])
            if _pdfplumber_box_is_visible(source, visible_bounds)
        ),
        key=lambda item: (
            float(item.get("top", 0)), float(item.get("x0", 0))
        ),
    )
    line_boxes = []
    for source in raw_line_boxes:
        if (
            line_boxes
            and abs(float(source.get("top", 0)) - line_boxes[-1]["top"]) <= 2.0
        ):
            current = line_boxes[-1]
            current["x0"] = min(current["x0"], float(source.get("x0", 0)))
            current["x1"] = max(current["x1"], float(source.get("x1", 0)))
            current["top"] = min(current["top"], float(source.get("top", 0)))
            current["bottom"] = max(
                current["bottom"], float(source.get("bottom", 0))
            )
        else:
            line_boxes.append({
                "x0": float(source.get("x0", 0)),
                "x1": float(source.get("x1", 0)),
                "top": float(source.get("top", 0)),
                "bottom": float(source.get("bottom", 0)),
            })
    if len(line_boxes) != len(rows):
        return []

    units = []
    for row, source in zip(rows, line_boxes):
        unit_box = _pdfplumber_unit_box(page, source)
        if unit_box is None:
            return []
        page_label = row["page_label"]
        text = row["title"]
        if page_label:
            # Keep the number visible in the source/translation comparison.  It
            # is removed from model output during drawing and the trusted source
            # number is placed separately at the original right edge.
            text += "    " + page_label
        unit = {
            "text": text,
            "heading": not bool(page_label),
            "box": unit_box,
            "toc_page_label": page_label,
            "toc_emphasis": unit_box["font_size"] >= 13.0,
        }
        if not page_label:
            unit["toc_centered"] = True
        units.append(unit)
    return units


def _pdf_page_has_distinct_cropbox(page):
    try:
        media = page.mediabox
        crop = page.cropbox
        return any(
            abs(float(a) - float(b)) > 0.01
            for a, b in zip(
                (media.left, media.bottom, media.right, media.top),
                (crop.left, crop.bottom, crop.right, crop.top),
            )
        )
    except (TypeError, ValueError, AttributeError):
        return True


def _pdf_page_visible_bounds(page):
    """Return the CropBox in pdfplumber's top-origin media-box coordinates."""
    try:
        media = page.mediabox
        crop = page.cropbox
        return (
            float(crop.left) - float(media.left),
            float(media.top) - float(crop.top),
            float(crop.right) - float(media.left),
            float(media.top) - float(crop.bottom),
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise _PDFLayoutFallback from exc


def _pdf_page_origin_is_supported(page):
    try:
        media = page.mediabox
        crop = page.cropbox
        crop_is_inside_media = (
            float(crop.left) >= float(media.left) - 0.01
            and float(crop.bottom) >= float(media.bottom) - 0.01
            and float(crop.right) <= float(media.right) + 0.01
            and float(crop.top) <= float(media.top) + 0.01
            and float(crop.right) - float(crop.left) >= 1.0
            and float(crop.top) - float(crop.bottom) >= 1.0
        )
        rotation = int(page.get("/Rotate", 0) or 0) % 360
        user_unit = float(page.get("/UserUnit", 1) or 1)
    except (TypeError, ValueError, AttributeError):
        return False
    return (
        rotation == 0
        and abs(user_unit - 1.0) <= 0.001
        and crop_is_inside_media
        and abs(float(media.left)) <= 0.01
        and abs(float(media.bottom)) <= 0.01
    )


def _pdf_unit_boxes(mapped, width, height):
    boxes = []
    margin = max(18.0, min(72.0, width * 0.08, height * 0.08))
    for entry in mapped:
        fragments = entry["fragments"]
        sizes = sorted(fragment["size"] for fragment in fragments)
        preferred = sizes[len(sizes) // 2]
        boxes.append({
            "entry": entry,
            "left": min(fragment["x"] for fragment in fragments),
            "right": width - margin,
            "top": max(
                fragment["y"] + fragment["size"] * 0.9
                for fragment in fragments
            ),
            "bottom": min(
                fragment["y"] - fragment["size"] * 0.35
                for fragment in fragments
            ),
            "font_size": min(36.0, max(5.5, preferred)),
            "color": tuple(fragments[0]["color"]),
        })

    # 同一视觉行中的多个单元（常见于表格）以右侧单元作为边界。
    for box in boxes:
        candidates = []
        for other in boxes:
            if other is box or other["left"] <= box["left"] + 1.0:
                continue
            row_tolerance = max(
                2.0, max(box["font_size"], other["font_size"]) * 0.35
            )
            if abs(other["top"] - box["top"]) <= row_tolerance:
                candidates.append(other["left"])
        if candidates:
            box["right"] = min(box["right"], min(candidates) - 4.0)
        if (
            box["left"] < 0
            or box["right"] > width + 0.01
            or box["bottom"] < 0
            or box["top"] > height + 0.01
            or box["right"] - box["left"] < 18.0
            or box["top"] - box["bottom"] < 4.0
        ):
            raise _PDFLayoutFallback
    return boxes


def _pdf_boxes_from_units(page_units, width, height):
    boxes = []
    for unit in page_units:
        source = unit.get("box")
        if not isinstance(source, dict):
            raise _PDFLayoutFallback
        try:
            box = {
                "entry": {"unit": unit, "fragments": []},
                "left": float(source["left"]),
                "right": float(source["right"]),
                "top": float(source["top"]),
                "bottom": float(source["bottom"]),
                "font_size": float(source["font_size"]),
                "color": tuple(source["color"]),
            }
            if "background_color" in source:
                box["background_color"] = tuple(source["background_color"])
            if isinstance(source.get("erase_boxes"), list):
                box["erase_boxes"] = [
                    {
                        "left": float(item["left"]),
                        "right": float(item["right"]),
                        "top": float(item["top"]),
                        "bottom": float(item["bottom"]),
                        "color": tuple(item["color"]),
                    }
                    for item in source["erase_boxes"]
                ]
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise _PDFLayoutFallback from exc
        if (
            box["left"] < 0
            or box["right"] > width + 0.01
            or box["bottom"] < 0
            or box["top"] > height + 0.01
            or box["right"] - box["left"] < 3.0
            or box["top"] - box["bottom"] < 4.0
        ):
            raise _PDFLayoutFallback
        boxes.append(box)
    return boxes


def _pdf_fit_translation(
    text, box, font_name, pdfmetrics, strict_layout=False,
):
    width = box["right"] - box["left"]
    height = box["top"] - box["bottom"]
    preferred = max(PDF_STRICT_MIN_FONT_SIZE, float(box["font_size"]))
    minimum = (
        PDF_STRICT_MIN_FONT_SIZE
        if strict_layout else max(5.5, preferred * 0.55)
    )

    def fitted(size):
        lines = _pdf_wrap_line(text, font_name, size, width, pdfmetrics)
        leading = size * 1.2
        required = size * 1.15 + max(0, len(lines) - 1) * leading
        if required <= height + 0.01:
            return size, leading, lines
        return None

    result = fitted(preferred)
    if result:
        return result
    result = fitted(minimum)
    if not result:
        raise _PDFLayoutFallback("译文缩小后仍无法放入原文字框")

    # 在最小字号与原字号间二分，取能够放入原框的最大字号。相比固定
    # 0.25pt 递减更快，也不会因为步长跳过本来可以使用的字号。
    lower, upper = minimum, preferred
    best = result
    for _ in range(16):
        middle = (lower + upper) / 2.0
        candidate = fitted(middle)
        if candidate:
            best = candidate
            lower = middle
        else:
            upper = middle
    return best


def _pdf_toc_translation_title(text, page_label):
    value = _normal_text(text)
    label = str(page_label or "").strip()
    if not label:
        return value
    match = re.search(rf"(?:\s+|[.·…]+){re.escape(label)}\s*$", value)
    if match:
        candidate = value[:match.start()].rstrip(" .·…")
        if candidate:
            return candidate
    return value


def _pdf_strip_page_text(page):
    try:
        contents = page.get_contents()
        if contents is None:
            return
        contents.operations = [
            (operands, operator)
            for operands, operator in contents.operations
            if operator not in _PDF_TEXT_SHOW_OPERATORS
        ]
        page.replace_contents(contents)
    except Exception as exc:
        raise _PDFLayoutFallback from exc


def _pdf_stream_without_text(stream, pdf):
    try:
        from pypdf.generic import ContentStream, DecodedStreamObject

        parsed = ContentStream(stream, pdf)
        parsed.operations = [
            (operands, operator)
            for operands, operator in parsed.operations
            if operator not in _PDF_TEXT_SHOW_OPERATORS
        ]
        replacement = DecodedStreamObject()
        for key, value in stream.items():
            if str(key) not in {"/Length", "/Filter", "/DecodeParms"}:
                replacement[key] = value
        replacement.set_data(parsed.get_data())
        return replacement
    except Exception as exc:
        raise _PDFLayoutFallback from exc


def _pdf_strip_resource_text(resources, writer, replacements=None):
    """在单页私有副本中递归去掉 Form / Pattern 里的文字。"""
    resources = _pdf_direct_object(resources)
    if not resources:
        return
    replacements = replacements if replacements is not None else {}
    for resource_name in ("/XObject", "/Pattern"):
        collection = _pdf_direct_object(resources.get(resource_name))
        if not collection:
            continue
        for name, reference in list(collection.items()):
            identity = _pdf_object_identity(reference)
            if identity in replacements:
                collection[name] = replacements[identity]
                continue
            item = _pdf_direct_object(reference)
            if not hasattr(item, "get_data"):
                continue
            if resource_name == "/XObject" and str(item.get("/Subtype", "")) != "/Form":
                continue
            replacement = _pdf_stream_without_text(item, writer)
            replacement_reference = writer._add_object(replacement)
            replacements[identity] = replacement_reference
            collection[name] = replacement_reference
            _pdf_strip_resource_text(
                replacement.get("/Resources"), writer, replacements
            )


def _build_pdf_layout_page(
    source_page,
    page,
    translations,
    font_name,
    emphasis_font_name,
    pdfmetrics,
    canvas,
    target_language,
    strict_layout=False,
):
    """只处理一页；失败时由调用方决定报错或仅降级该页。"""
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise _PDFLayoutFallback from exc

    width, height = page["size"]
    if page.get("force_reflow") or not _pdf_page_origin_is_supported(source_page):
        raise _PDFLayoutFallback
    if page.get("geometry_source") in {"pdfplumber", "ocr"}:
        boxes = _pdf_boxes_from_units(page["units"], width, height)
    else:
        if _pdf_page_has_external_text(source_page):
            raise _PDFLayoutFallback
        mapped = _pdf_map_positioned_units(
            source_page, page["units"], width, height
        )
        boxes = _pdf_unit_boxes(mapped, width, height)
    if len(boxes) != len(translations):
        raise _PDFLayoutFallback

    page_plan = []
    for box, translation in zip(boxes, translations):
        unit = box["entry"]["unit"]
        draw_font = (
            emphasis_font_name
            if unit.get("heading") or unit.get("toc_emphasis")
            else font_name
        )
        page_label = str(unit.get("toc_page_label") or "")
        fitted_box = dict(box)
        fitted_text = translation
        if page_label:
            fitted_text = _pdf_toc_translation_title(translation, page_label)
            reserved = (
                pdfmetrics.stringWidth(page_label, draw_font, box["font_size"])
                + max(10.0, box["font_size"])
            )
            fitted_box["right"] -= reserved
            if fitted_box["right"] - fitted_box["left"] < 18.0:
                raise _PDFLayoutFallback
        size, leading, lines = _pdf_fit_translation(
            fitted_text,
            fitted_box,
            draw_font,
            pdfmetrics,
            strict_layout=strict_layout,
        )
        page_plan.append({
            "box": box,
            "fit_box": fitted_box,
            "size": size,
            "leading": leading,
            "lines": lines,
            "page_label": page_label,
            "centered": bool(unit.get("toc_centered")),
            "font_name": draw_font,
        })

    overlay_bytes = io.BytesIO()
    overlay = canvas.Canvas(
        overlay_bytes,
        pagesize=(width, height),
        pageCompression=1,
        invariant=1,
    )
    for plan in page_plan:
        box = plan["box"]
        fitted_box = plan["fit_box"]
        size = plan["size"]
        leading = plan["leading"]
        lines = plan["lines"]
        page_label = plan["page_label"]
        draw_font = plan["font_name"]
        erase_boxes = box.get("erase_boxes") or []
        background = box.get("background_color")
        if erase_boxes or background is not None:
            # 扫描页的原文属于背景图片，不能像文字层那样删除操作符。
            # 用文字框边缘采样的底色覆盖原字，再在同一框内绘制译文。
            padding = max(1.0, min(6.0, size * 0.15))
            regions = erase_boxes or [{
                "left": box["left"], "right": box["right"],
                "bottom": box["bottom"], "top": box["top"],
                "color": background,
            }]
            for region in regions:
                left = max(0.0, region["left"] - padding)
                right = min(width, region["right"] + padding)
                bottom = max(0.0, region["bottom"] - padding)
                top = min(height, region["top"] + padding)
                overlay.saveState()
                overlay.setFillColorRGB(*region["color"])
                overlay.rect(left, bottom, right - left, top - bottom, fill=1, stroke=0)
                overlay.restoreState()
        overlay.setFont(draw_font, size)
        overlay.setFillColorRGB(*box["color"])
        baseline = box["top"] - size * 0.9
        for line_index, line in enumerate(lines):
            y = baseline - line_index * leading
            direction = _pdf_line_direction(line, target_language)
            shaping = _pdf_contains_shaping_script(line)
            if plan["centered"]:
                overlay.drawCentredString(
                    (box["left"] + box["right"]) / 2.0,
                    y,
                    line,
                    direction=direction,
                    shaping=shaping,
                )
            elif direction == "RTL":
                overlay.drawRightString(
                    fitted_box["right"], y, line,
                    direction="RTL", shaping=shaping,
                )
            else:
                overlay.drawString(
                    fitted_box["left"], y, line,
                    direction="LTR", shaping=shaping,
                )
        if page_label:
            overlay.drawRightString(box["right"], baseline, page_label)
            if len(lines) == 1 and _pdf_line_direction(lines[0], target_language) != "RTL":
                title_end = fitted_box["left"] + pdfmetrics.stringWidth(
                    lines[0], draw_font, size
                )
                label_left = box["right"] - pdfmetrics.stringWidth(
                    page_label, draw_font, size
                )
                leader_start = title_end + max(3.0, size * 0.45)
                leader_end = label_left - max(3.0, size * 0.45)
                if leader_end > leader_start:
                    overlay.saveState()
                    overlay.setStrokeColorRGB(*box["color"])
                    overlay.setLineWidth(max(0.35, size * 0.04))
                    overlay.setLineCap(1)
                    overlay.setDash(max(0.4, size * 0.06), max(1.8, size * 0.22))
                    overlay.line(
                        leader_start,
                        baseline + size * 0.18,
                        leader_end,
                        baseline + size * 0.18,
                    )
                    overlay.restoreState()
    overlay.save()

    overlay_reader = PdfReader(io.BytesIO(overlay_bytes.getvalue()))
    writer = PdfWriter()
    output_page = writer.add_page(source_page)
    _pdf_strip_page_text(output_page)
    # Text inside Form/Pattern resources is not necessarily represented by the
    # verified page-unit geometry (running headers and logo lettering are common
    # examples).  Removing every resource text operator would silently erase
    # such page furniture.  Preserve those resources until they can be mapped as
    # explicit translation units; direct page text is still replaced above.
    output_page.merge_page(overlay_reader.pages[0], over=True)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _build_pdf_reflow(
    pages,
    translations,
    font_name,
    emphasis_font_name,
    pdfmetrics,
    canvas,
    document_title,
    target_language,
):
    try:
        output = io.BytesIO()
        initial_size = pages[0]["size"]
        pdf = canvas.Canvas(
            output,
            pagesize=initial_size,
            pageCompression=1,
            invariant=1,
        )
    except Exception as exc:
        raise DocumentFormatError("无法初始化 PDF 输出") from exc
    pdf.setCreator("Translation Bench")
    pdf.setAuthor("")
    pdf.setTitle(str(document_title or "Translated document"))
    pdf.setSubject("Reflowed translation generated from a text-layer PDF")

    translation_index = 0
    for page_index, page in enumerate(pages):
        width, height = page["size"]
        if page_index:
            pdf.showPage()
        pdf.setPageSize((width, height))
        margin = max(30.0, min(54.0, width * 0.08, height * 0.08))
        available_width = max(120.0, width - margin * 2)
        y = height - margin

        for unit in page["units"]:
            translation = translations[translation_index]
            translation_index += 1
            draw_font = emphasis_font_name if unit["heading"] else font_name
            font_size = 14.0 if unit["heading"] else 10.5
            leading = font_size * (1.55 if unit["heading"] else 1.45)
            lines = _pdf_wrap_line(
                translation, draw_font, font_size, available_width, pdfmetrics
            )
            opening = min(2, max(1, len(lines))) * leading
            if y - opening < margin and y < height - margin:
                pdf.showPage()
                pdf.setPageSize((width, height))
                y = height - margin
            for line in lines:
                if y - leading < margin:
                    pdf.showPage()
                    pdf.setPageSize((width, height))
                    y = height - margin
                pdf.setFont(draw_font, font_size)
                pdf.setFillColorRGB(0.08, 0.08, 0.08)
                y -= leading
                line_direction = _pdf_line_direction(line, target_language)
                line_shaping = _pdf_contains_shaping_script(line)
                if line_direction == "RTL":
                    pdf.drawRightString(
                        width - margin, y, line,
                        direction="RTL", shaping=line_shaping,
                    )
                else:
                    pdf.drawString(
                        margin, y, line,
                        direction="LTR", shaping=line_shaping,
                    )
            y -= 8.0 if unit["heading"] else 6.0
    pdf.save()
    return output.getvalue()


def _build_pdf(
    data,
    translated_text,
    document_title="",
    target_language="",
    page_start=None,
    page_end=None,
    page_selection=None,
    strict_layout=True,
    recognition_mode="auto",
):
    pages = _pdf_document_units(
        data, page_start, page_end, page_selection, recognition_mode
    )
    translations = str(translated_text).split("\n")
    source_units = [unit for page in pages for unit in page["units"]]
    if len(translations) < len(source_units):
        raise DocumentFormatError("译文单元数量少于 PDF 原文单元")
    if len(translations) > len(source_units):
        raise DocumentFormatError("译文单元数量多于 PDF 原文单元")
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfgen import canvas
        from reportlab.pdfgen.textobject import rtlSupport
    except ImportError as exc:
        raise DocumentFormatError(
            "PDF 输出支持尚未安装，请先运行: py -3 -m pip install -r requirements.txt"
        ) from exc

    joined = "\n".join(translations)
    has_rtl = any(
        _pdf_line_direction(translation, target_language) == "RTL"
        for translation in translations
    )
    shaping = _pdf_contains_shaping_script(joined)
    if has_rtl and not rtlSupport:
        raise DocumentFormatError(
            "当前 ReportLab 环境缺少 RTL 排版组件，暂不能生成阿拉伯文或希伯来文 PDF"
        )
    if shaping:
        try:
            import uharfbuzz  # noqa: F401
        except ImportError as exc:
            raise DocumentFormatError(
                "当前译文需要复杂文字塑形；请安装 uharfbuzz 后再生成 PDF"
            ) from exc

    font_name = _pdf_output_font(joined, target_language)
    emphasis_font_name = _pdf_output_emphasis_font(
        joined, target_language, font_name
    )
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise DocumentFormatError(
            "PDF 输出支持尚未安装，请先运行: py -3 -m pip install -r requirements.txt"
        ) from exc

    # 未选页面不做文字解析或改写。严格模式下所选页面只允许原位替换，
    # 任何定位或容纳失败都会停止生成；关闭严格模式才允许逐页重排。
    with _pdf_decode_limits():
        reader = _pdf_reader(data)
        selected_pages = set(normalize_pdf_page_selection(
            len(reader.pages), page_selection, page_start, page_end
        ))
        page_map = {page["page_number"]: page for page in pages}
        page_translations = {}
        translation_index = 0
        for page in pages:
            count = len(page["units"])
            page_translations[page["page_number"]] = translations[
                translation_index:translation_index + count
            ]
            translation_index += count

        writer = PdfWriter()
        layout_pages = 0
        reflow_pages = 0
        for page_number, source_page in enumerate(reader.pages, 1):
            if page_number not in selected_pages:
                writer.add_page(source_page)
                continue
            page = page_map[page_number]
            current_translations = page_translations[page_number]
            if not page["units"]:
                writer.add_page(source_page)
                continue
            try:
                rendered = _build_pdf_layout_page(
                    source_page,
                    page,
                    current_translations,
                    font_name,
                    emphasis_font_name,
                    pdfmetrics,
                    canvas,
                    target_language,
                    strict_layout=strict_layout,
                )
                rendered_reader = PdfReader(io.BytesIO(rendered))
                writer.add_page(rendered_reader.pages[0])
                layout_pages += 1
            except (DocumentFormatError, _PDFLayoutFallback) as exc:
                if strict_layout:
                    detail = str(exc).strip()
                    suffix = f"：{detail}" if detail else ""
                    raise DocumentFormatError(
                        f"PDF 第 {page_number} 页无法严格原位替换{suffix}。"
                        "该模式不会重排页面；可缩短对应译文、取消选择该页，"
                        "或关闭严格保持版式后重试"
                    ) from exc
                rendered = _build_pdf_reflow(
                    [page],
                    current_translations,
                    font_name,
                    emphasis_font_name,
                    pdfmetrics,
                    canvas,
                    document_title,
                    target_language,
                )
                rendered_reader = PdfReader(io.BytesIO(rendered))
                for rendered_page in rendered_reader.pages:
                    writer.add_page(rendered_page)
                reflow_pages += 1
            except Exception as exc:
                if strict_layout:
                    raise DocumentFormatError(
                        f"PDF 第 {page_number} 页原位替换失败。"
                        "该模式不会重排页面；可取消选择该页，"
                        "或关闭严格保持版式后重试"
                    ) from exc
                rendered = _build_pdf_reflow(
                    [page],
                    current_translations,
                    font_name,
                    emphasis_font_name,
                    pdfmetrics,
                    canvas,
                    document_title,
                    target_language,
                )
                rendered_reader = PdfReader(io.BytesIO(rendered))
                for rendered_page in rendered_reader.pages:
                    writer.add_page(rendered_page)
                reflow_pages += 1

        source_kind = "scanned PDF" if any(page.get("ocr") for page in pages) else "text-layer PDF"
        if reflow_pages and layout_pages:
            subject = f"Mixed layout-preserving and reflowed translation from a {source_kind}"
        elif reflow_pages:
            subject = f"Reflowed translation generated from a {source_kind}"
        else:
            subject = f"Layout-preserving translation generated from a {source_kind}"
        writer.metadata = {
            "/Title": str(document_title or "Translated document"),
            "/Author": "",
            "/Creator": "Translation Bench",
            "/Subject": subject,
        }
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()


def extract_binary_text(
    document_format,
    data,
    pdf_page_start=None,
    pdf_page_end=None,
    pdf_page_selection=None,
    pdf_recognition_mode="auto",
):
    with FORMAT_LOCK:
        fmt = str(document_format or "").lower().lstrip(".")
        if fmt == "docx":
            return _extract_docx(data)
        if fmt == "epub":
            return _extract_epub(data)
        if fmt == "pdf":
            return _extract_pdf(
                data, pdf_page_start, pdf_page_end, pdf_page_selection,
                pdf_recognition_mode,
            )
        raise DocumentFormatError("不支持的二进制文档格式")


def build_binary_output(
    document_format,
    source_data,
    translated_text,
    document_title="",
    target_language="",
    pdf_page_start=None,
    pdf_page_end=None,
    pdf_page_selection=None,
    pdf_strict_layout=True,
    pdf_recognition_mode="auto",
):
    with FORMAT_LOCK:
        fmt = str(document_format or "").lower().lstrip(".")
        if fmt == "docx":
            return _build_docx(source_data, translated_text)
        if fmt == "epub":
            return _build_epub(source_data, translated_text)
        if fmt == "pdf":
            try:
                return _build_pdf(
                    source_data,
                    translated_text,
                    document_title=document_title,
                    target_language=target_language,
                    page_start=pdf_page_start,
                    page_end=pdf_page_end,
                    page_selection=pdf_page_selection,
                    strict_layout=bool(pdf_strict_layout),
                    recognition_mode=pdf_recognition_mode,
                )
            except DocumentFormatError:
                raise
            except Exception as exc:
                raise DocumentFormatError("生成 PDF 失败") from exc
        raise DocumentFormatError("不支持的二进制文档格式")


def _safe_source_id(source_id):
    value = str(source_id or "")
    if not re.fullmatch(r"[0-9a-f]{32}\.(?:docx|epub|pdf)", value):
        raise DocumentFormatError("二进制原文缓存标识无效")
    return value


def _cached_source_result(
    destination, source_id, extension, digest, pdf_recognition_mode="auto",
):
    with FORMAT_LOCK:
        pdf_page_count_value = None
        pdf_page_selection = None
        pdf_ocr = False
        if extension == ".pdf":
            pdf_recognition_mode = normalize_pdf_recognition_mode(
                pdf_recognition_mode
            )
            pdf_page_count_value = pdf_page_count(destination)
            has_text_layer = _pdf_has_text_layer(destination)
            pdf_ocr = (
                pdf_recognition_mode == "ocr"
                or (pdf_recognition_mode == "auto" and not has_text_layer)
            )
            # 扫描 PDF 先识别一页，让导入尽快完成并立即交给用户多选页；
            # 普通文字层 PDF 保持原有的默认全文行为。
            pdf_page_selection = "1" if pdf_ocr else "all"
            pages = _pdf_document_units(
                destination,
                page_selection=pdf_page_selection,
                recognition_mode=pdf_recognition_mode,
            )
            content = _pdf_units_text(pages)
        else:
            content = extract_binary_text(extension, destination)
    result = {
        "source_id": source_id,
        "source_format": extension.lstrip("."),
        "source_sha256": digest,
        "content": content,
    }
    if pdf_page_count_value is not None:
        selected_pages = normalize_pdf_page_selection(
            pdf_page_count_value, pdf_page_selection
        )
        result.update({
            "pdf_page_count": pdf_page_count_value,
            "pdf_page_start": selected_pages[0],
            "pdf_page_end": selected_pages[-1],
            "pdf_page_selection": pdf_page_selection,
            "pdf_extraction_version": PDF_EXTRACTION_VERSION,
            "pdf_ocr": pdf_ocr,
            "pdf_recognition_mode": pdf_recognition_mode,
        })
    return result


def _remove_cached_source_files(destination):
    for candidate in (
        destination, destination + ".ocr.json", destination + ".ocr.json.tmp",
    ):
        with contextlib.suppress(OSError):
            os.remove(candidate)


def cache_binary_source(
    cache_dir, filename, data, pdf_recognition_mode="auto",
):
    extension = document_extension(filename)
    if extension not in BINARY_EXTENSIONS:
        raise DocumentFormatError("不支持的二进制文档格式")
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise DocumentFormatError("文档数据无效")
    os.makedirs(cache_dir, exist_ok=True)
    source_id = uuid.uuid4().hex + extension
    destination = os.path.join(cache_dir, source_id)
    temporary = destination + ".tmp"
    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
        os.replace(temporary, destination)
        return _cached_source_result(
            destination, source_id, extension, binary_digest(data),
            pdf_recognition_mode,
        )
    except Exception:
        _remove_cached_source_files(destination)
        raise
    finally:
        with contextlib.suppress(OSError):
            os.remove(temporary)


def cache_binary_source_stream(
    cache_dir, filename, source_stream, content_length, chunk_size=1024 * 1024,
    pdf_recognition_mode="auto",
):
    """把 HTTP 请求体分块写入内部缓存，不在浏览器或 Python 中复制整份文件。"""
    extension = document_extension(filename)
    if extension not in BINARY_EXTENSIONS:
        raise DocumentFormatError("不支持的二进制文档格式")
    try:
        remaining = int(content_length)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DocumentFormatError("导入文件长度无效") from exc
    if remaining <= 0:
        raise DocumentFormatError("文档为空")
    os.makedirs(cache_dir, exist_ok=True)
    source_id = uuid.uuid4().hex + extension
    destination = os.path.join(cache_dir, source_id)
    temporary = destination + ".tmp"
    digest = hashlib.sha256()
    try:
        with open(temporary, "wb") as handle:
            while remaining:
                chunk = source_stream.read(
                    min(max(4096, int(chunk_size)), remaining)
                )
                if not chunk:
                    raise DocumentFormatError("文档上传中断，请重新添加文件")
                handle.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
        os.replace(temporary, destination)
        return _cached_source_result(
            destination, source_id, extension, digest.hexdigest(),
            pdf_recognition_mode,
        )
    except Exception:
        _remove_cached_source_files(destination)
        raise
    finally:
        with contextlib.suppress(OSError):
            os.remove(temporary)


def load_binary_source_path(
    cache_dir, source_id, expected_format="", expected_sha256="",
):
    safe_id = _safe_source_id(source_id)
    if expected_format and not safe_id.endswith(
        "." + str(expected_format).lower().lstrip(".")
    ):
        raise DocumentFormatError("二进制原文格式与缓存不一致")
    path = os.path.join(cache_dir, safe_id)
    if not os.path.isfile(path):
        raise DocumentFormatError("二进制原文缓存不存在，请重新添加文件")
    digest = binary_digest(path)
    if expected_sha256 and digest != expected_sha256:
        raise DocumentFormatError("二进制原文缓存校验失败，请重新添加文件")
    return path


def load_binary_source(cache_dir, source_id, expected_format="", expected_sha256=""):
    path = load_binary_source_path(
        cache_dir, source_id, expected_format, expected_sha256
    )
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise DocumentFormatError("无法读取二进制原文缓存") from exc


def delete_binary_source(cache_dir, source_id):
    safe_id = _safe_source_id(source_id)
    path = os.path.join(cache_dir, safe_id)
    existed = os.path.isfile(path)
    try:
        for candidate in (path, path + ".ocr.json", path + ".ocr.json.tmp"):
            try:
                os.remove(candidate)
            except FileNotFoundError:
                pass
    except OSError as exc:
        raise DocumentFormatError("无法删除二进制原文缓存") from exc
    return existed

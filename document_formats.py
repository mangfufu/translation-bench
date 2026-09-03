"""DOCX/EPUB/PDF 二进制文档的安全导入、文本映射和输出重建。"""

import contextlib
import copy
import hashlib
import io
import os
import posixpath
import re
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
MAX_BINARY_BYTES = 50 * 1024 * 1024
MAX_ZIP_MEMBERS = 5000
MAX_ZIP_MEMBER_BYTES = 50 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 250 * 1024 * 1024
MAX_PDF_PAGES = 1000
MAX_PDF_TEXT_CHARS = 20 * 1024 * 1024
MAX_PDF_PAGE_CONTENT_BYTES = 16 * 1024 * 1024
MAX_PDF_TOTAL_CONTENT_BYTES = 64 * 1024 * 1024

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


def binary_digest(data):
    return hashlib.sha256(data).hexdigest()


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
    if not isinstance(data, (bytes, bytearray)):
        raise DocumentFormatError("文档数据无效")
    if not data or len(data) > MAX_BINARY_BYTES:
        raise DocumentFormatError("文档为空或超过 50MB 限制")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data), "r")
        members = archive.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise DocumentFormatError("文件不是有效的 ZIP 文档容器") from exc
    if len(members) > MAX_ZIP_MEMBERS:
        archive.close()
        raise DocumentFormatError("文档内部文件数量过多")
    total = 0
    for member in members:
        if member.flag_bits & 0x1:
            archive.close()
            raise DocumentFormatError("暂不支持加密文档")
        if member.file_size > MAX_ZIP_MEMBER_BYTES:
            archive.close()
            raise DocumentFormatError("文档内部存在过大的文件")
        total += member.file_size
        if total > MAX_ZIP_TOTAL_BYTES:
            archive.close()
            raise DocumentFormatError("文档解压后超过 250MB 限制")
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
    actual_total = 0
    try:
        for member in members:
            if member.is_dir():
                continue
            member_total = 0
            with archive.open(member, "r") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    member_total += len(chunk)
                    actual_total += len(chunk)
                    if member_total > MAX_ZIP_MEMBER_BYTES:
                        raise DocumentFormatError("文档内部存在过大的文件")
                    if actual_total > MAX_ZIP_TOTAL_BYTES:
                        raise DocumentFormatError("文档解压后超过 250MB 限制")
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
    if not isinstance(data, (bytes, bytearray)):
        raise DocumentFormatError("PDF 数据无效")
    if not data or len(data) > MAX_BINARY_BYTES:
        raise DocumentFormatError("PDF 为空或超过 50MB 限制")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocumentFormatError(
            "PDF 支持尚未安装，请先运行: py -3 -m pip install -r requirements.txt"
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(bytes(data)), strict=False)
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
    """临时收紧 pypdf 的流解压上限，避免检查长度前已展开巨量数据。"""
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
    hard_limit = MAX_PDF_PAGE_CONTENT_BYTES + 1
    for name in names:
        if not hasattr(pdf_filters, name):
            continue
        value = getattr(pdf_filters, name)
        previous[name] = value
        if not isinstance(value, int) or value <= 0 or value > hard_limit:
            setattr(pdf_filters, name, hard_limit)
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
    value = str(text or "").replace("\x00", "")
    return "".join(
        character
        for character in value
        if character in "\t\r\n" or unicodedata.category(character) != "Cc"
    )


def _pdf_page_content_size(page):
    """在文字解释器运行前检查解压后的页面指令流大小。"""
    try:
        contents = page.get_contents()
        if contents is None:
            return 0
        size = len(contents.get_data())
    except Exception as exc:
        raise DocumentFormatError("PDF 页面内容流无法安全解压") from exc
    if size > MAX_PDF_PAGE_CONTENT_BYTES:
        raise DocumentFormatError("PDF 单页解压内容流超过 16MB 安全限制")
    return size


def _pdf_page_units(page):
    """把 PDF 的视觉行合并成句段单元，绝不从字符中间建立定位键。"""
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


def _pdf_page_size(page):
    try:
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        rotation = int(page.get("/Rotate", 0) or 0) % 360
    except (TypeError, ValueError, AttributeError):
        width, height, rotation = 612.0, 792.0, 0
    if rotation in {90, 270}:
        width, height = height, width
    if not (216 <= width <= 2000 and 216 <= height <= 2000):
        return 612.0, 792.0
    return width, height


def _pdf_document_units(data):
    with _pdf_decode_limits():
        reader = _pdf_reader(data)
        pages = []
        total_characters = 0
        total_content_bytes = 0
        try:
            for page in reader.pages:
                total_content_bytes += _pdf_page_content_size(page)
                if total_content_bytes > MAX_PDF_TOTAL_CONTENT_BYTES:
                    raise DocumentFormatError("PDF 解压内容流合计超过 64MB 安全限制")
                units = _pdf_page_units(page)
                total_characters += sum(len(unit["text"]) for unit in units)
                if total_characters > MAX_PDF_TEXT_CHARS:
                    raise DocumentFormatError("PDF 可提取文字超过 20MB 限制")
                pages.append({"size": _pdf_page_size(page), "units": units})
        except DocumentFormatError:
            raise
        except Exception as exc:
            raise DocumentFormatError("PDF 页面结构读取失败") from exc
    if not any(page["units"] for page in pages):
        raise DocumentFormatError(
            "PDF 没有可用文字层；扫描件或图片 PDF 请先使用 OCR"
        )
    return pages


def _extract_pdf(data):
    pages = _pdf_document_units(data)
    return "\n".join(
        unit["text"] for page in pages for unit in page["units"]
    )


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
    """原位替换无法被严格验证时，内部切换到重排版。"""


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


def _pdf_page_origin_is_supported(page):
    try:
        media = page.mediabox
        crop = page.cropbox
        values = (
            float(media.left), float(media.bottom), float(crop.left), float(crop.bottom)
        )
        boxes_equal = all(
            abs(float(a) - float(b)) <= 0.01
            for a, b in zip(
                (media.left, media.bottom, media.right, media.top),
                (crop.left, crop.bottom, crop.right, crop.top),
            )
        )
        rotation = int(page.get("/Rotate", 0) or 0) % 360
        user_unit = float(page.get("/UserUnit", 1) or 1)
    except (TypeError, ValueError, AttributeError):
        return False
    return (
        rotation == 0
        and abs(user_unit - 1.0) <= 0.001
        and boxes_equal
        and all(abs(value) <= 0.01 for value in values)
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


def _pdf_fit_translation(text, box, font_name, pdfmetrics):
    width = box["right"] - box["left"]
    height = box["top"] - box["bottom"]
    preferred = box["font_size"]
    minimum = max(5.5, preferred * 0.55)
    size = preferred
    while size + 1e-6 >= minimum:
        lines = _pdf_wrap_line(text, font_name, size, width, pdfmetrics)
        leading = size * 1.2
        required = size * 1.15 + max(0, len(lines) - 1) * leading
        if required <= height + 0.01:
            return size, leading, lines
        size -= 0.25
    raise _PDFLayoutFallback


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


def _build_pdf_layout_preserving(
    data,
    pages,
    translations,
    font_name,
    pdfmetrics,
    canvas,
    document_title,
    target_language,
):
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise _PDFLayoutFallback from exc

    with _pdf_decode_limits():
        reader = _pdf_reader(data)
        if len(reader.pages) != len(pages):
            raise _PDFLayoutFallback
        overlay_bytes = io.BytesIO()
        overlay = canvas.Canvas(
            overlay_bytes,
            pagesize=pages[0]["size"],
            pageCompression=1,
            invariant=1,
        )
        plans = []
        translation_index = 0
        for page_index, source_page in enumerate(reader.pages):
            width, height = pages[page_index]["size"]
            if not _pdf_page_origin_is_supported(source_page):
                raise _PDFLayoutFallback
            if _pdf_page_has_external_text(source_page):
                raise _PDFLayoutFallback
            mapped = _pdf_map_positioned_units(
                source_page, pages[page_index]["units"], width, height
            )
            boxes = _pdf_unit_boxes(mapped, width, height)
            page_plan = []
            for box in boxes:
                translation = translations[translation_index]
                translation_index += 1
                size, leading, lines = _pdf_fit_translation(
                    translation, box, font_name, pdfmetrics
                )
                page_plan.append((box, size, leading, lines))
            plans.append(page_plan)

        # 先完成全文匹配和容量验证，再生成覆盖层，保证不会产生半成品。
        for page_index, page_plan in enumerate(plans):
            if page_index:
                overlay.showPage()
            width, height = pages[page_index]["size"]
            overlay.setPageSize((width, height))
            for box, size, leading, lines in page_plan:
                overlay.setFont(font_name, size)
                overlay.setFillColorRGB(*box["color"])
                baseline = box["top"] - size * 0.9
                for line_index, line in enumerate(lines):
                    y = baseline - line_index * leading
                    direction = _pdf_line_direction(line, target_language)
                    shaping = _pdf_contains_shaping_script(line)
                    if direction == "RTL":
                        overlay.drawRightString(
                            box["right"], y, line,
                            direction="RTL", shaping=shaping,
                        )
                    else:
                        overlay.drawString(
                            box["left"], y, line,
                            direction="LTR", shaping=shaping,
                        )
        overlay.save()

        overlay_reader = PdfReader(io.BytesIO(overlay_bytes.getvalue()))
        if len(overlay_reader.pages) != len(reader.pages):
            raise _PDFLayoutFallback
        writer = PdfWriter()
        for page_index, source_page in enumerate(reader.pages):
            output_page = writer.add_page(source_page)
            _pdf_strip_page_text(output_page)
            output_page.merge_page(overlay_reader.pages[page_index], over=True)
        writer.metadata = {
            "/Title": str(document_title or "Translated document"),
            "/Author": "",
            "/Creator": "Translation Bench",
            "/Subject": "Layout-preserving translation generated from a text-layer PDF",
        }
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()


def _build_pdf_reflow(
    pages,
    translations,
    font_name,
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
            font_size = 14.0 if unit["heading"] else 10.5
            leading = font_size * (1.55 if unit["heading"] else 1.45)
            lines = _pdf_wrap_line(
                translation, font_name, font_size, available_width, pdfmetrics
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
                pdf.setFont(font_name, font_size)
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


def _build_pdf(data, translated_text, document_title="", target_language=""):
    pages = _pdf_document_units(data)
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
    try:
        return _build_pdf_layout_preserving(
            data,
            pages,
            translations,
            font_name,
            pdfmetrics,
            canvas,
            document_title,
            target_language,
        )
    except _PDFLayoutFallback:
        return _build_pdf_reflow(
            pages,
            translations,
            font_name,
            pdfmetrics,
            canvas,
            document_title,
            target_language,
        )
    except DocumentFormatError:
        raise
    except Exception:
        return _build_pdf_reflow(
            pages,
            translations,
            font_name,
            pdfmetrics,
            canvas,
            document_title,
            target_language,
        )


def extract_binary_text(document_format, data):
    with FORMAT_LOCK:
        fmt = str(document_format or "").lower().lstrip(".")
        if fmt == "docx":
            return _extract_docx(data)
        if fmt == "epub":
            return _extract_epub(data)
        if fmt == "pdf":
            return _extract_pdf(data)
        raise DocumentFormatError("不支持的二进制文档格式")


def build_binary_output(
    document_format,
    source_data,
    translated_text,
    document_title="",
    target_language="",
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


def cache_binary_source(cache_dir, filename, data):
    extension = document_extension(filename)
    if extension not in BINARY_EXTENSIONS:
        raise DocumentFormatError("不支持的二进制文档格式")
    content = extract_binary_text(extension, data)
    os.makedirs(cache_dir, exist_ok=True)
    source_id = uuid.uuid4().hex + extension
    destination = os.path.join(cache_dir, source_id)
    temporary = destination + ".tmp"
    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
        os.replace(temporary, destination)
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass
    return {
        "source_id": source_id,
        "source_format": extension.lstrip("."),
        "source_sha256": binary_digest(data),
        "content": content,
    }


def load_binary_source(cache_dir, source_id, expected_format="", expected_sha256=""):
    safe_id = _safe_source_id(source_id)
    if expected_format and not safe_id.endswith("." + str(expected_format).lower().lstrip(".")):
        raise DocumentFormatError("二进制原文格式与缓存不一致")
    path = os.path.join(cache_dir, safe_id)
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_BINARY_BYTES + 1)
    except FileNotFoundError as exc:
        raise DocumentFormatError("二进制原文缓存不存在，请重新添加文件") from exc
    if len(data) > MAX_BINARY_BYTES:
        raise DocumentFormatError("二进制原文缓存超过 50MB 限制")
    digest = binary_digest(data)
    if expected_sha256 and digest != expected_sha256:
        raise DocumentFormatError("二进制原文缓存校验失败，请重新添加文件")
    return data


def delete_binary_source(cache_dir, source_id):
    safe_id = _safe_source_id(source_id)
    path = os.path.join(cache_dir, safe_id)
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False

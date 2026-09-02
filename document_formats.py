"""DOCX/EPUB 二进制文档的安全导入、文本映射和原格式写回。"""

import copy
import hashlib
import io
import os
import posixpath
import re
import threading
import uuid
import zipfile
import xml.etree.ElementTree as ET
from html.entities import html5 as HTML_ENTITIES
from urllib.parse import unquote


class DocumentFormatError(ValueError):
    pass


BINARY_EXTENSIONS = {".docx", ".epub"}
MAX_BINARY_BYTES = 50 * 1024 * 1024
MAX_ZIP_MEMBERS = 5000
MAX_ZIP_MEMBER_BYTES = 50 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 250 * 1024 * 1024

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
EPUB_IGNORED = {"script", "style", "noscript", "svg", "math", "rt", "rp"}


def _local_name(tag):
    return str(tag).rsplit("}", 1)[-1].lower()


def _has_descendant_block(element):
    return any(
        descendant is not element and _local_name(descendant.tag) in EPUB_BLOCKS
        for descendant in element.iter()
    )


def _xhtml_text_slots(element):
    slots = []
    visible = []

    def add(owner, attribute, value):
        if value is None:
            return
        visible.append(value)
        if value.strip():
            slots.append((owner, attribute, value))

    def walk(node):
        if _local_name(node.tag) in EPUB_IGNORED:
            return
        add(node, "text", node.text)
        for child in list(node):
            # br/hr 是可见的文本边界。它们没有 text，若不显式
            # 加分隔，Hello<br/>world 会被错误提取成 Helloworld。
            if _local_name(child.tag) in {"br", "hr"}:
                visible.append(" ")
            else:
                walk(child)
            add(child, "tail", child.tail)

    walk(element)
    return _normal_text("".join(visible)), slots


def _xhtml_units_from_root(root):
    units = []

    def visit(element):
        if _local_name(element.tag) in EPUB_IGNORED:
            return
        if _local_name(element.tag) in EPUB_BLOCKS and not _has_descendant_block(element):
            source, slots = _xhtml_text_slots(element)
            if source and slots:
                units.append((source, slots))
            return
        for child in list(element):
            visit(child)

    visit(root)
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


def extract_binary_text(document_format, data):
    with FORMAT_LOCK:
        fmt = str(document_format or "").lower().lstrip(".")
        if fmt == "docx":
            return _extract_docx(data)
        if fmt == "epub":
            return _extract_epub(data)
        raise DocumentFormatError("不支持的二进制文档格式")


def build_binary_output(document_format, source_data, translated_text):
    with FORMAT_LOCK:
        fmt = str(document_format or "").lower().lstrip(".")
        if fmt == "docx":
            return _build_docx(source_data, translated_text)
        if fmt == "epub":
            return _build_epub(source_data, translated_text)
        raise DocumentFormatError("不支持的二进制文档格式")


def _safe_source_id(source_id):
    value = str(source_id or "")
    if not re.fullmatch(r"[0-9a-f]{32}\.(?:docx|epub)", value):
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

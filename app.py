#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地翻译工作台 · 后端

调用本地 OpenAI 兼容翻译模型（如 llama-server / Hy-MT2），提供网页拖拽翻译。

用法:
  python app.py [port]        # 默认 9000
  浏览器打开 http://localhost:9000
"""
import base64
import io
import hashlib
import http.client
import json
import os
import queue
import re
import socket
import sys
import time
import uuid
import threading
import zipfile
from urllib.parse import parse_qs, quote, unquote, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from document_formats import (
    BINARY_EXTENSIONS,
    PDF_EXTRACTION_VERSION,
    DocumentFormatError,
    binary_digest,
    build_binary_output,
    cache_binary_source,
    cache_binary_source_stream,
    delete_binary_source,
    document_extension,
    extract_binary_text,
    extract_pdf_translation_data,
    format_pdf_page_selection,
    is_binary_document,
    load_binary_source,
    load_binary_source_path,
    normalize_pdf_page_selection,
    pdf_page_count,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
OUTPUTS_DIR = os.path.join(BASE_DIR, "outputs")
OUTPUT_INDEX_PATH = os.path.join(BASE_DIR, ".translation-state.json")
SOURCE_CACHE_DIR = os.path.join(BASE_DIR, ".translation-sources")

JOBS = {}
JOBS_LOCK = threading.Lock()
OUTPUT_INDEX_LOCK = threading.Lock()
MODEL_LOG_LOCK = threading.Lock()
MODEL_LOG_SEQUENCE = 0
JOB_TTL_SECONDS = 24 * 60 * 60
MAX_FINISHED_JOBS = 50
MAX_FINISHED_JOB_TEXT_CHARS = 32 * 1024 * 1024
SSE_QUEUE_MAX_EVENTS = 256


def load_cached_source(cache_dir, source_id, source_format="", source_sha256=""):
    """PDF 保持为磁盘路径，避免大文件在每个接口中整体复制进内存。"""
    if str(source_format or "").lower().lstrip(".") == "pdf":
        return load_binary_source_path(
            cache_dir, source_id, source_format, source_sha256
        )
    return load_binary_source(cache_dir, source_id, source_format, source_sha256)


def next_model_log_id():
    global MODEL_LOG_SEQUENCE
    with MODEL_LOG_LOCK:
        MODEL_LOG_SEQUENCE += 1
        return MODEL_LOG_SEQUENCE


def terminal_model_log(title, body=""):
    """原子写入终端，避免后台任务的长提示词互相穿插。"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    block = f"\n{'=' * 24} {title} · {stamp} {'=' * 24}\n"
    if body:
        block += str(body).rstrip() + "\n"
    block += "=" * 72
    try:
        with MODEL_LOG_LOCK:
            print(block, flush=True)
    except (OSError, UnicodeError):
        # 终端显示是诊断功能，绝不能反过来让翻译任务失败。
        pass

DEFAULT_CONFIG = {
    "server": "http://127.0.0.1:8081",
    "api_key": "local-qwen",
    "model": "hy-mt2-7b",
    "context_size": 65536,        # 服务端槽位上下文(用于预算 max_tokens)
    "temperature": 0.7,
    "top_k": 20,
    "top_p": 0.6,
    "repeat_penalty": 1.05,
    "enable_thinking": False,     # 翻译默认关闭思考模式，避免额外推理和输出污染
    "pdf_strict_layout": True,    # PDF 默认只原位替换，禁止自动重排页面
    "context_mode": "full",      # full=尽量全文，neighbor=邻近上下文，unit=独立单元
    "context_units": 12,          # 最近确认译文单元；长文窗口也用作前向重叠
    "future_context_units": 6,    # 长文固定窗口向后的完整单元重叠
    "max_retries": 5,
    "src_lang": "自动判断",
    "tgt_lang": "中文",
    "translation_style": "自动判断",
    "system_prompt": "",          # 用户补充要求；硬性规则由程序内部固定
}

BASE_SYSTEM_PROMPT = (
    "你是专业文档翻译引擎。指令优先级严格为：硬性规则 > 用户补充要求 > 风格偏好；"
    "低优先级内容与高优先级冲突时，必须服从高优先级内容。\n"
    "【硬性规则】\n"
    "1. 忠实、完整地翻译，不得省略、概括、捏造内容或额外解释。\n"
    "2. 只输出当前待译单元的译文，不输出说明、标签、定位键或原文。\n"
    "3. 当前单元可能在句中截断。不得补写前后文；承接上文时不得重复已有内容；"
    "相邻译文拼接后应连贯。单元内部不要自行换行。\n"
    "4. 结合参考上下文统一术语、专名、称谓和语气，但不得重复输出参考内容。\n"
    "5. 原样保留 URL、行内代码、变量、占位符、数字、Markdown 行内标记及字幕内联标签。"
)

STYLE_PROMPTS = {
    "自动判断": "根据当前文本判断合适风格：剧情重视情景、人物口吻和自然表达；专业内容重视准确、直接和通俗易懂；其他内容保持自然清晰。",
    "通用自然": "表达自然流畅、清楚易读，不过度书面化，也不额外润色或扩写。",
    "剧情文学": "保持情节、事实、视角和人物设定不变，可适度润色措辞、节奏、氛围与人物口吻，使译文具有自然的叙事感；不得新增情节。",
    "专业技术": "术语准确一致，表达直截了当、逻辑清楚且通俗易懂；避免文学化修饰、含混措辞和不必要的复杂句。",
    "对话字幕": "语言口语化、简洁自然，保留人物语气、情绪和身份差异；控制句长，不添加对白。",
    "正式商务": "使用正式、克制、清晰的商务表达，信息明确，措辞礼貌，术语和称谓一致。",
    "学术论文": "使用准确、客观、中性的学术表达，保持论证关系、限定条件、术语、数据和引文含义。",
    "轻小说网文": "保持剧情与人物设定，语言轻快顺畅、富有画面感，对话自然，可适度本地化措辞但不得改写情节。",
}

_SIMP = set("们国说时对这为点让认识还样从实开会现道起吗个子于后体气无门问题关东长发马鱼鸟车银间队阶阳阴风飞见亲观欢边进过")
_TRAD = set("們國說時對這為點讓認識還樣從實開會現道起嗎個子於後體氣無門問題關東長髮馬魚鳥車銀間隊階陽陰風飛見親觀歡邊進過")

LANG_SUFFIXES = {
    "中文": "zh", "繁体中文": "zh-hant", "粤语": "yue", "藏文": "bo",
    "英文": "en", "法文": "fr", "西班牙文": "es", "葡萄牙文": "pt",
    "德文": "de", "意大利文": "it", "荷兰文": "nl", "瑞典文": "sv",
    "波兰文": "pl", "捷克文": "cs", "斯洛伐克文": "sk", "斯洛文尼亚文": "sl",
    "克罗地亚文": "hr", "塞尔维亚文": "sr", "罗马尼亚文": "ro", "匈牙利文": "hu",
    "保加利亚文": "bg", "乌克兰文": "uk", "希腊文": "el", "立陶宛文": "lt",
    "俄文": "ru", "韩文": "ko", "泰文": "th", "越南文": "vi",
    "马来文": "ms", "印尼文": "id", "菲律宾文": "fil", "印地文": "hi",
    "孟加拉文": "bn", "乌尔都文": "ur", "泰米尔文": "ta", "泰卢固文": "te",
    "马拉地文": "mr", "古吉拉特文": "gu", "阿拉伯文": "ar", "希伯来文": "he",
    "波斯文": "fa", "高棉文": "km", "缅甸文": "my", "哈萨克文": "kk",
    "蒙古文": "mn", "维吾尔文": "ug", "日文": "ja",
}


def output_filename(filename, target_language):
    """生成安全的输出文件名，后缀跟随目标语言。"""
    clean_name = os.path.basename(str(filename).replace("\\", "/")) or "translated.md"
    base, ext = os.path.splitext(clean_name)
    suffix = LANG_SUFFIXES.get(target_language, "translated")
    return f"{base}.{suffix}{ext or '.md'}"


def source_digest(content):
    normalized, _, _ = normalize_text_content(content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_text_content(content):
    """统一内部文本为无 BOM 的 LF，同时记住原文的换行和 BOM。"""
    value = str(content or "")
    has_bom = value.startswith("\ufeff")
    if has_bom:
        value = value[1:]
    crlf_count = value.count("\r\n")
    without_crlf = value.replace("\r\n", "")
    cr_count = without_crlf.count("\r")
    lf_count = without_crlf.count("\n")
    if crlf_count >= max(cr_count, lf_count) and crlf_count:
        eol = "crlf"
    elif cr_count > lf_count:
        eol = "cr"
    else:
        eol = "lf"
    return value.replace("\r\n", "\n").replace("\r", "\n"), eol, has_bom


def _text_eol(value, fallback="lf"):
    return (
        value
        if isinstance(value, str) and value in {"lf", "crlf", "cr"}
        else fallback
    )


def encode_text_output(content, eol="lf", bom=False):
    normalized, _, _ = normalize_text_content(content)
    separator = {"crlf": "\r\n", "cr": "\r"}.get(_text_eol(eol), "\n")
    rendered = normalized.replace("\n", separator)
    return (("\ufeff" if bom else "") + rendered).encode("utf-8")


def enqueue_job_event(job, event):
    """有界 SSE 队列：慢客户端只会丢旧的预览事件，不会拖垮翻译。"""
    event_queue = job["q"]
    try:
        event_queue.put_nowait(event)
        return
    except queue.Full:
        pass
    try:
        event_queue.get_nowait()
    except queue.Empty:
        pass
    try:
        event_queue.put_nowait(event)
    except queue.Full:
        # 并发消费时极少可能再次满；/status 仍可恢复真实状态。
        pass


def save_translation_output(
    file_info, translated_content, target_language, pdf_strict_layout=True,
):
    """按源格式保存译文；二进制容器从缓存原文重建。"""
    name = file_info["name"]
    output_name = output_filename(name, target_language)
    output_path = os.path.join(OUTPUTS_DIR, output_name)
    source_format = str(file_info.get("source_format") or "text").lower()
    source_content = file_info.get("content", "")
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    os.makedirs(SOURCE_CACHE_DIR, exist_ok=True)

    extension = document_extension(name)
    if extension in BINARY_EXTENSIONS and source_format != extension.lstrip("."):
        raise DocumentFormatError("缺少二进制原文，请重新添加文件")
    if is_binary_document(source_format) and extension != "." + source_format:
        raise DocumentFormatError("文件扩展名与二进制原文格式不一致")
    if is_binary_document(source_format):
        source_data = load_cached_source(
            SOURCE_CACHE_DIR,
            file_info.get("source_id"),
            source_format,
            file_info.get("source_sha256", ""),
        )
        output_data = build_binary_output(
            source_format,
            source_data,
            translated_content,
            document_title=output_name,
            target_language=target_language,
            pdf_page_start=file_info.get("pdf_page_start"),
            pdf_page_end=file_info.get("pdf_page_end"),
            pdf_page_selection=file_info.get("pdf_page_selection"),
            pdf_strict_layout=pdf_strict_layout,
        )
        digest = binary_digest(source_data)
    else:
        output_data = encode_text_output(
            translated_content,
            file_info.get("text_eol", "lf"),
            bool(file_info.get("text_bom", False)),
        )
        digest = source_digest(source_content)

    temporary = output_path + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(temporary, "wb") as handle:
            handle.write(output_data)
        os.replace(temporary, output_path)
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass

    metadata = {
        "source_name": os.path.basename(str(name).replace("\\", "/")),
        "source_sha256": digest,
        "source_format": source_format,
        "target_language": target_language,
        "output_name": output_name,
    }
    if source_format == "pdf":
        # PDF 视觉换行不等于翻译单元边界。把无格式预览集中放在状态
        # 文件中，恢复页面时仍可与原文稳定一一对应。
        metadata["preview_content"] = translated_content
        count = int(file_info.get("pdf_page_count") or pdf_page_count(source_data))
        selected_pages = normalize_pdf_page_selection(
            count,
            file_info.get("pdf_page_selection"),
            file_info.get("pdf_page_start"),
            file_info.get("pdf_page_end"),
        )
        metadata["pdf_page_count"] = count
        metadata["pdf_page_start"] = selected_pages[0]
        metadata["pdf_page_end"] = selected_pages[-1]
        metadata["pdf_page_selection"] = format_pdf_page_selection(
            selected_pages, count
        )
        metadata["pdf_extraction_version"] = PDF_EXTRACTION_VERSION
        metadata["pdf_strict_layout"] = bool(pdf_strict_layout)
    save_output_metadata(metadata)
    return output_name


def read_output_preview(output_path, output_name, preview_content=None):
    extension = document_extension(output_name)
    if extension == ".pdf" and isinstance(preview_content, str):
        return normalize_text_content(preview_content)[0]
    if extension in BINARY_EXTENSIONS:
        with open(output_path, "rb") as handle:
            return extract_binary_text(extension, handle.read())
    with open(output_path, "rb") as handle:
        content = handle.read().decode("utf-8-sig")
    return normalize_text_content(content)[0]


def _read_output_index():
    try:
        with open(OUTPUT_INDEX_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_output_index(index):
    temp_path = OUTPUT_INDEX_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=2)
    os.replace(temp_path, OUTPUT_INDEX_PATH)


def save_output_metadata(metadata):
    """把所有译文状态集中保存在一个内部索引，不污染 outputs 目录。"""
    output_name = metadata.get("output_name")
    if not output_name or output_name != os.path.basename(output_name):
        raise ValueError("无效的译文文件名")
    with OUTPUT_INDEX_LOCK:
        index = _read_output_index()
        index[output_name] = dict(metadata)
        _write_output_index(index)


def remove_output_metadata(output_name):
    with OUTPUT_INDEX_LOCK:
        index = _read_output_index()
        if index.pop(output_name, None) is not None:
            _write_output_index(index)


def migrate_legacy_metadata():
    """一次性合并旧版逐文件 sidecar，然后移除这些程序生成的旧文件。"""
    if not os.path.isdir(OUTPUTS_DIR):
        return
    legacy_paths = [
        os.path.join(OUTPUTS_DIR, name)
        for name in os.listdir(OUTPUTS_DIR)
        if name.endswith(".meta.json")
    ]
    if not legacy_paths:
        return
    with OUTPUT_INDEX_LOCK:
        index = _read_output_index()
        for meta_path in legacy_paths:
            try:
                with open(meta_path, "r", encoding="utf-8") as fh:
                    metadata = json.load(fh)
                output_name = metadata.get("output_name")
                if (
                    isinstance(output_name, str)
                    and output_name == os.path.basename(output_name)
                    and os.path.isfile(os.path.join(OUTPUTS_DIR, output_name))
                ):
                    index[output_name] = metadata
            except (OSError, ValueError, AttributeError):
                continue
        _write_output_index(index)
    for meta_path in legacy_paths:
        try:
            os.remove(meta_path)
        except OSError:
            pass


def prune_output_metadata():
    """移除已经没有对应译文文件的状态项，避免索引无界增长。"""
    with OUTPUT_INDEX_LOCK:
        index = _read_output_index()
        kept = {
            name: metadata
            for name, metadata in index.items()
            if (
                isinstance(name, str)
                and name == os.path.basename(name)
                and os.path.isfile(os.path.join(OUTPUTS_DIR, name))
            )
        }
        if kept == index:
            return 0
        removed = len(index) - len(kept)
        if kept:
            _write_output_index(kept)
        else:
            try:
                os.remove(OUTPUT_INDEX_PATH)
            except FileNotFoundError:
                pass
        return removed


def clear_output_cache():
    """清除恢复状态和旧 sidecar；实际译文文件始终保留。"""
    with OUTPUT_INDEX_LOCK:
        cached = len(_read_output_index())
        for path in (OUTPUT_INDEX_PATH, OUTPUT_INDEX_PATH + ".tmp"):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    legacy_removed = 0
    if os.path.isdir(OUTPUTS_DIR):
        for name in os.listdir(OUTPUTS_DIR):
            if not name.endswith(".meta.json"):
                continue
            try:
                os.remove(os.path.join(OUTPUTS_DIR, name))
                legacy_removed += 1
            except FileNotFoundError:
                pass
    return cached, legacy_removed


def detect_lang(text):
    """按字符集自动判断语言, 返回下拉框里的语言名。拉丁语系归为英文(模型会按内容理解)。"""
    if re.search(r'[぀-ヿ]', text):          # 平假名/片假名
        return "日文"
    if re.search(r'[가-힯]', text):          # 韩文谚文
        return "韩文"
    if re.search(r'[א-ת]', text):           # 希伯来文
        return "希伯来文"
    if re.search(r'[؀-ۿ]', text):          # 阿拉伯文
        return "阿拉伯文"
    if re.search(r'[ༀ-࿿]', text):          # 藏文
        return "藏文"
    if re.search(r'[ঀ-৿]', text):          # 孟加拉文
        return "孟加拉文"
    if re.search(r'[઀-૿]', text):          # 古吉拉特文
        return "古吉拉特文"
    if re.search(r'[ऀ-ॿ]', text):          # 天城文(印地文等)
        return "印地文"
    if re.search(r'[஀-௿]', text):          # 泰米尔文
        return "泰米尔文"
    if re.search(r'[ఀ-౿]', text):          # 泰卢固文
        return "泰卢固文"
    if re.search(r'[฀-๿]', text):          # 泰文
        return "泰文"
    if re.search(r'[က-႟]', text):          # 缅甸文
        return "缅甸文"
    if re.search(r'[ក-៿]', text):          # 高棉文
        return "高棉文"
    if re.search(r'[᠀-᢯]', text):          # 蒙古文
        return "蒙古文"
    if re.search(r'[Ͱ-Ͽ]', text):          # 希腊文
        return "希腊文"
    if re.search(r'[Ѐ-ӿ]', text):          # 西里尔(俄文/乌克兰文等)
        return "俄文"
    cjk = re.findall(r'[一-鿿]', text)
    if cjk:
        simp = sum(1 for c in cjk if c in _SIMP)
        trad = sum(1 for c in cjk if c in _TRAD)
        if trad and trad > simp:
            return "繁体中文"
        return "中文"
    if re.search(r'[a-zA-Z]', text):
        return "英文"
    return "中文"      # 兜底


# ---------------------------------------------------------------------------
# 翻译引擎
# ---------------------------------------------------------------------------

TRANSIENT_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class ModelServiceError(RuntimeError):
    def __init__(self, status, detail, retry_after=None):
        super().__init__(f"模型服务 HTTP {status}: {detail}")
        self.status = status
        self.transient = status in TRANSIENT_HTTP_STATUS
        self.retry_after = retry_after


class OutputLengthError(RuntimeError):
    """模型输出已占满当前上下文允许的全部生成空间。"""


class Translator:
    def __init__(self, config):
        self.cfg = {**DEFAULT_CONFIG, **(config or {})}
        self._conn = None
        self._server = None
        self._interrupt_event = threading.Event()
        self._last_completion_error = None

    def _server_parts(self):
        if self._server is None:
            parsed = urlsplit(str(self.cfg["server"]).rstrip("/"))
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError("服务地址必须是有效的 http:// 或 https:// 地址")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            prefix = parsed.path.rstrip("/")
            self._server = (parsed.scheme, parsed.hostname, port, prefix)
        return self._server

    def _connection(self):
        if self._interrupt_event.is_set():
            raise InterruptedError("任务已中断")
        if self._conn is None:
            scheme, host, port, _ = self._server_parts()
            cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            self._conn = cls(host, port, timeout=180)
        return self._conn

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    @property
    def interrupted(self):
        return self._interrupt_event.is_set()

    def interrupt(self):
        """立即终止当前模型请求，并阻止该翻译器继续重试。"""
        self._interrupt_event.set()
        conn = self._conn
        sock = getattr(conn, "sock", None) if conn is not None else None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.close()

    @staticmethod
    def _read_stream_response(resp, on_delta=None):
        """读取 OpenAI 兼容 SSE，返回与非流式响应相同的最小结构。"""
        chunks = []
        finish_reason = None
        while True:
            raw_line = resp.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", "replace").strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                # 读完 chunked 终止块，再主动关闭本次本地连接。
                resp.read()
                break
            event = json.loads(data)
            choices = event.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if piece is None:
                piece = choice.get("text", "")
            if piece:
                chunks.append(str(piece))
                if on_delta:
                    on_delta(str(piece), "".join(chunks))
            if choice.get("finish_reason") is not None:
                finish_reason = choice.get("finish_reason")
        return {
            "choices": [{
                "message": {"content": "".join(chunks)},
                "finish_reason": finish_reason,
            }]
        }

    def _post_json(self, payload, on_delta=None):
        if self._interrupt_event.is_set():
            raise InterruptedError("任务已中断")
        _, _, _, prefix = self._server_parts()
        path = prefix + "/v1/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            # 当前 Windows llama-server 在流式响应结束后会关闭套接字。
            # 明确一请求一连接，避免下一行先命中失效 keep-alive 再重试。
            "Connection": "close",
        }
        api_key = str(self.cfg.get("api_key", "") or "").strip()
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        try:
            conn = self._connection()
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            if not 200 <= resp.status < 300:
                data = resp.read()
                detail = data.decode("utf-8", "replace")[:500]
                raw_retry_after = resp.getheader("Retry-After")
                try:
                    retry_after = max(0.0, min(60.0, float(raw_retry_after)))
                except (TypeError, ValueError):
                    retry_after = None
                raise ModelServiceError(resp.status, detail, retry_after)
            content_type = str(resp.getheader("Content-Type", "")).lower()
            if payload.get("stream") and "text/event-stream" in content_type:
                result = self._read_stream_response(resp, on_delta)
            else:
                data = resp.read()
                result = json.loads(data.decode("utf-8"))
                if on_delta:
                    content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                    if content:
                        on_delta(content, content)
            self.close()
            return result
        except Exception:
            self.close()
            raise

    def _system_prompt(self):
        """组合固定规则、用户补充要求和风格偏好。"""
        cfg = self.cfg
        extra_prompt = str(cfg.get("system_prompt", "") or "").strip()
        style = cfg.get("translation_style", "自动判断")
        style_prompt = STYLE_PROMPTS.get(style, STYLE_PROMPTS["自动判断"])
        prompt_parts = [BASE_SYSTEM_PROMPT]
        if extra_prompt:
            prompt_parts.append(f"【用户补充要求】\n{extra_prompt}")
        if style_prompt:
            prompt_parts.append(f"【风格偏好（{style}）】\n{style_prompt}")
        return "\n\n".join(prompt_parts)

    @staticmethod
    def _estimate_tokens(text):
        """在没有模型 tokenizer 时保守估算 token；非 ASCII 文字按更高密度计算。"""
        text = str(text or "")
        non_ascii = sum(1 for char in text if ord(char) > 127)
        ascii_chars = len(text) - non_ascii
        return max(1, int(non_ascii * 1.2 + ascii_chars / 3.2) + 1)

    def _output_token_budget(self, text):
        """为完整译文留出足够空间，也限制模型跑偏时的无限生成。"""
        # 拉丁字母语言译成中日韩文字时，源文 token 密度较低，而译文
        # token 密度明显更高。1.5 倍在长 PDF 文本块上容易正常翻到一半
        # 就命中 length；这里宁可多预留一些，本地推理只按实际生成量运行。
        return min(80000, max(384, int(self._estimate_tokens(text) * 2.25) + 384))

    def _complete_prompt(
        self, prompt, max_tokens, on_stream=None, retry_on_length=True,
    ):
        """发送一次聊天补全；传入的流回调接收当前累计文本。"""
        cfg = self.cfg
        sys_prompt = self._system_prompt()
        payload = {
            "model": cfg["model"],
            "messages": ([{"role": "system", "content": sys_prompt}] if sys_prompt else [])
                       + [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": cfg.get("temperature", 0.7),
            "top_k": cfg.get("top_k", 20),
            "top_p": cfg.get("top_p", 0.6),
            "repeat_penalty": cfg.get("repeat_penalty", 1.05),
            "chat_template_kwargs": {
                "enable_thinking": bool(cfg.get("enable_thinking", False)),
            },
            "stream": bool(on_stream),
        }
        last = None
        self._last_completion_error = None
        retryable = True
        attempts = max(1, int(cfg.get("max_retries", 5)))
        context_size = max(2048, int(cfg.get("context_size", 65536)))
        estimated_prompt = (
            self._estimate_tokens(sys_prompt)
            + self._estimate_tokens(prompt)
            + 128
        )
        max_tokens_limit = max(
            int(max_tokens), context_size - estimated_prompt - 96
        )

        for attempt in range(attempts):
            request_id = next_model_log_id()
            terminal_model_log(
                f"模型请求 #{request_id}（尝试 {attempt + 1}/{attempts}）",
                "POST /v1/chat/completions\n"
                + json.dumps(payload, ensure_ascii=False, indent=2),
            )
            response_accumulated = ""
            response_logged = False

            def handle_stream_delta(_piece, accumulated):
                nonlocal response_accumulated
                response_accumulated = accumulated
                if self._interrupt_event.is_set():
                    raise InterruptedError("任务已中断")
                on_stream(accumulated)

            try:
                if on_stream:
                    on_stream("")
                result = self._post_json(
                    payload,
                    handle_stream_delta if on_stream else None,
                )
                choice = result["choices"][0]
                output = choice["message"]["content"]
                finish_reason = choice.get("finish_reason")
                terminal_model_log(
                    f"模型回复 #{request_id}（finish_reason={finish_reason or '未知'}）",
                    output if output else "（模型返回空正文）",
                )
                response_logged = True
                if choice.get("finish_reason") == "length" and retry_on_length:
                    current_budget = int(payload["max_tokens"])
                    expanded_budget = min(
                        max_tokens_limit,
                        max(current_budget + 256, int(current_budget * 1.75)),
                    )
                    if attempt + 1 < attempts and expanded_budget > current_budget:
                        payload["max_tokens"] = expanded_budget
                        terminal_model_log(
                            f"模型请求 #{request_id} 输出空间不足",
                            f"max_tokens 将从 {current_budget} 提高到 {expanded_budget} 后立即重试",
                        )
                        continue
                    raise OutputLengthError(
                        f"译文达到 max_tokens={current_budget}，且没有更多上下文空间可用于完整输出"
                    )
                return True, output
            except Exception as exc:
                last = exc
                self._last_completion_error = exc
                if not response_logged:
                    terminal_model_log(
                        f"模型回复 #{request_id}（请求未完成）",
                        response_accumulated or "（未收到可用回复正文）",
                    )
                terminal_model_log(
                    f"模型请求 #{request_id} 错误",
                    f"{type(exc).__name__}: {exc}",
                )
                if self._interrupt_event.is_set():
                    return False, "任务已中断"
                retryable = (
                    not isinstance(exc, OutputLengthError)
                    and (not isinstance(exc, ModelServiceError) or exc.transient)
                )
                if not retryable or attempt + 1 >= attempts:
                    break
                retry_after = getattr(exc, "retry_after", None)
                delay = retry_after if retry_after is not None else min(20.0, 1.5 * (2 ** attempt))
                self.close()
                if self._interrupt_event.wait(delay):
                    return False, "任务已中断"
        suffix = f"（已尝试 {attempts} 次）" if attempts > 1 and retryable else ""
        return False, str(last) + suffix

    def translate_text(
        self, text, context_before="", context_after="",
        previous_translation="", retranslate=False, on_stream=None,
        source_language=None, reference_text="", reference_kind="全文原文",
        continuation_mode="",
    ):
        """翻译一段文本, 返回 (成功, 结果/错误)。"""
        cfg = self.cfg
        sys_prompt = self._system_prompt()
        est_in = sum(self._estimate_tokens(part) for part in (
            text, context_before, context_after, previous_translation, sys_prompt,
            reference_text,
        )) + 384
        ctx = max(2048, int(cfg.get("context_size", 65536)))
        # 输出约为输入量级; 限制 max_tokens 防止模型跑偏时无限生成导致卡死
        available = ctx - est_in - 96
        if available < 256:
            return False, f"输入超出上下文预算（估算输入 {est_in} tokens，上下文 {ctx}）"
        max_tokens = min(self._output_token_budget(text), available)
        src = source_language or cfg.get("src_lang", "自动判断")
        tgt = cfg.get("tgt_lang", "中文")
        if src == "自动判断":
            src = detect_lang(text)
        if continuation_mode == "strong":
            continuation_instruction = (
                "【跨单元衔接提示】\n"
                "版面结构表明当前原文是上一单元同一句话的后半段。只译当前实际出现的内容，"
                "直接承接上一段译文；不要重复上一段已有的主语、动作或修饰语，也不要补成独立句。\n\n"
            )
        elif continuation_mode == "possible":
            continuation_instruction = (
                "【单元边界判断】\n"
                "当前原文可能是上一单元的后半句，也可能只是没有规范标点的新段落。"
                "请结合最近原文和已确认译文判断：若为续句，只译当前实际内容并直接衔接，"
                "不得重复上文；若为新段落，则正常完整翻译。不得仅因缺少标点而省略主语或信息。\n\n"
            )
        else:
            continuation_instruction = ""
        if reference_text or context_before or context_after or retranslate:
            references = []
            if reference_text:
                references.append(
                    f"【固定{reference_kind}（只供理解，禁止翻译或输出）】\n"
                    + reference_text
                    + f"\n【固定{reference_kind}结束】"
                )
            if context_before:
                references.append(
                    "【最近已确认译文（用于统一术语、称谓和语气）】\n"
                    + context_before
                )
            if context_after:
                references.append(
                    "【邻近下文参考（尚未翻译的原文）】\n" + context_after
                )
            context_text = "\n\n".join(references)
            if retranslate:
                previous_block = (
                    f"\n\n【当前旧译文（仅供检查，不得盲目照抄）】\n{previous_translation}"
                    if previous_translation else ""
                )
                retranslation_instruction = (
                    "请对照原文检查旧译文中的错译、漏译、增译、称谓、术语、语气和不自然表达，"
                    "然后完整地重新翻译当前文本。旧译文可能有错，只能作为纠错参考。\n"
                    if previous_translation else
                    "请忽略旧版译文，基于原文和本轮已经确认的新译文，完整地重新翻译当前文本。\n"
                )
                prompt = (
                    "这是重译任务。参考内容只用于理解语境、消除歧义并统一术语、称谓和语气。\n"
                    + retranslation_instruction
                    + "只输出修正后的完整译文，不得输出分析、修改说明、原文、旧译文或前后文；"
                    "单段译文内部不要自行换行。\n\n"
                    + (context_text + "\n\n" if context_text else "")
                    + previous_block
                    + ("\n\n" + continuation_instruction.rstrip() if continuation_instruction else "")
                    + f"\n\n【当前待译文本（{src} → {tgt}）】\n{text}"
                )
            else:
                prompt = (
                    "以下固定参考和已确认译文只用于理解语境、消除歧义并统一术语、称谓和语气。\n"
                    "只翻译【当前待译文本】，不得翻译、续写或输出任何前文和下文参考。\n\n"
                    + context_text
                    + ("\n\n" + continuation_instruction.rstrip() if continuation_instruction else "")
                    + f"\n\n【当前待译文本（{src} → {tgt}）】\n{text}"
                )
        else:
            prompt = (
                "只翻译【当前待译文本】，只输出对应译文，不得输出说明、标签或原文；"
                "单元内部不要自行换行。"
                + ("\n\n" + continuation_instruction.rstrip() if continuation_instruction else "")
                + f"\n\n【当前待译文本（{src} → {tgt}）】\n{text}"
            )
        return self._complete_prompt(prompt, max_tokens, on_stream)

    @staticmethod
    def _one_line(text):
        """模型偶尔自行断行；压回一行以保持源文的行结构。"""
        return re.sub(r"\s*\r?\n+\s*", " ", str(text).strip())

    def _context_token_caps(self):
        """单元上下文最多使用约15%的模型窗口，前文占三分之二。"""
        context_size = max(2048, int(self.cfg.get("context_size", 65536)))
        total = max(256, min(8192, int(context_size * 0.15)))
        before = max(128, int(total * 2 / 3))
        return before, max(128, total - before)

    def _document_reference_budget(self, sources):
        """计算可分给固定全文/窗口参考的安全 token 预算。"""
        context_size = max(2048, int(self.cfg.get("context_size", 65536)))
        source_units = [
            unit
            for source in sources
            for unit in self._semantic_units(source)
            if unit.strip()
        ]
        if source_units:
            largest_input = max(self._estimate_tokens(unit) for unit in source_units)
            # 参考窗口规划保留一个常规译文预算即可；真正请求若遇到
            # length 会动态扩容。直接按最坏输出预留会让小上下文模式
            # 完全失去前后文。
            largest_output = max(
                max(256, int(self._estimate_tokens(unit) * 1.5) + 256)
                for unit in source_units
            )
        else:
            largest_input = 1
            largest_output = 256
        recent_translation_budget, _ = self._context_token_caps()
        fixed_prompt = self._estimate_tokens(self._system_prompt()) + 512
        safety = max(256, min(4096, int(context_size * 0.06)))
        return max(
            0,
            context_size
            - fixed_prompt
            - recent_translation_budget
            - largest_input
            - largest_output
            - safety,
        )

    @staticmethod
    def _is_reference_boundary(lines, translatable, position):
        """判断某个待译单元之前是否适合作为稳定窗口边界。"""
        if position <= 0 or position >= len(translatable):
            return True
        previous_line = translatable[position - 1][0]
        current_line = translatable[position][0]
        if current_line - previous_line > 1:
            return True
        current = lines[current_line]
        return bool(re.match(r"^\s{0,3}#{1,6}[ \t]+", current))

    @staticmethod
    def _reference_excerpt(lines, translatable, start, end):
        """按原文件顺序生成参考片段，并保留段落间的空行。"""
        selected = translatable[start:end]
        if not selected:
            return ""
        parts = []
        previous_line = None
        for line_index, _ in selected:
            if previous_line is not None and line_index - previous_line > 1:
                parts.append("")
            parts.append(lines[line_index])
            previous_line = line_index
        return "\n".join(parts).strip()

    def _document_reference_plan(self, content, translatable, filename=""):
        """选择可缓存的全文参考或稳定语义窗口。"""
        if not translatable:
            return []
        sources = [text for _, text in translatable]
        budget = self._document_reference_budget(sources)
        if budget <= 0:
            return [
                {"text": "", "kind": "邻近原文参考", "window": 0}
                for _ in sources
            ]

        lines = str(content).split("\n")
        extension = os.path.splitext(str(filename))[1].lower()
        full_reference = (
            self._reference_excerpt(lines, translatable, 0, len(translatable))
            if extension in {".srt", ".vtt"}
            else str(content).strip()
        )
        if full_reference and self._estimate_tokens(full_reference) <= budget:
            entry = {"text": full_reference, "kind": "全文原文参考", "window": 0}
            return [entry] * len(translatable)

        core_budget = max(128, int(budget * 0.68))
        groups = []
        start = 0
        total = len(translatable)
        while start < total:
            end = start
            used = 0
            last_boundary = None
            while end < total:
                line_text = lines[translatable[end][0]]
                cost = self._estimate_tokens(line_text) + 2
                if end > start and used + cost > core_budget:
                    break
                used += cost
                end += 1
                if self._is_reference_boundary(lines, translatable, end):
                    last_boundary = end
            if end < total and last_boundary and last_boundary > start:
                if last_boundary - start >= max(1, (end - start) // 3):
                    end = last_boundary
            if end <= start:
                end = start + 1
            groups.append((start, end))
            start = end

        previous_overlap = max(0, int(self.cfg.get("context_units", 12)))
        future_overlap = max(0, int(self.cfg.get("future_context_units", 6)))
        plan = [None] * total
        for window_index, (core_start, core_end) in enumerate(groups):
            group_start, group_end = core_start, core_end
            left = max(0, core_start - previous_overlap)
            right = min(total, core_end + future_overlap)
            reference = self._reference_excerpt(lines, translatable, left, right)

            while reference and self._estimate_tokens(reference) > budget:
                if left < core_start:
                    left += 1
                elif right > core_end:
                    right -= 1
                elif core_end - core_start > 1:
                    core_end -= 1
                else:
                    reference = ""
                    break
                reference = self._reference_excerpt(
                    lines, translatable, left, right
                )

            entry = {
                "text": reference,
                "kind": "当前语义窗口参考",
                "window": window_index,
            }
            for position in range(group_start, group_end):
                plan[position] = entry

        fallback = {"text": "", "kind": "邻近原文参考", "window": -1}
        return [entry or fallback for entry in plan]

    def _context_mode(self):
        """返回有效上下文模式；旧配置和非法值均回退到默认模式。"""
        mode = str(self.cfg.get("context_mode", "full")).strip().lower()
        return mode if mode in {"full", "neighbor", "unit"} else "full"

    def _active_reference_plan(self, content, translatable, filename=""):
        """只有“尽量全文”模式才构建全文或固定语义窗口参考。"""
        if self._context_mode() == "full":
            return self._document_reference_plan(content, translatable, filename)
        kind = (
            "邻近原文参考"
            if self._context_mode() == "neighbor"
            else "独立单元"
        )
        return [
            {"text": "", "kind": kind, "window": -1}
            for _ in translatable
        ]

    def _semantic_units(self, text):
        """只在超长物理行中按完整句末拆分；普通行原样作为一个单元。"""
        text = str(text)
        context_size = max(2048, int(self.cfg.get("context_size", 65536)))
        max_tokens = max(256, min(2048, int(context_size * 0.05)))
        if self._estimate_tokens(text) <= max_tokens:
            return [text]

        sentences = [
            match.group(0)
            for match in re.finditer(
                r".+?(?:[。！？.!?]+[”’\"」』）)\]]*\s*|$)",
                text,
                re.DOTALL,
            )
            if match.group(0)
        ]
        if not sentences:
            sentences = [text]

        units, current = [], ""
        for sentence in sentences:
            candidate = current + sentence
            if current and self._estimate_tokens(candidate) > max_tokens:
                units.append(current.strip())
                current = sentence
            else:
                current = candidate

            # 极端情况下单句本身也可能超过预算；优先在空白处拆分，
            # 找不到空白时才按估算比例硬切，避免请求直接超出上下文。
            while self._estimate_tokens(current) > max_tokens:
                approximate = max(1, int(len(current) * max_tokens / self._estimate_tokens(current)))
                split_at = current.rfind(" ", max(1, approximate // 2), approximate + 1)
                if split_at <= 0:
                    split_at = approximate
                units.append(current[:split_at].strip())
                current = current[split_at:].lstrip()
        if current.strip():
            units.append(current.strip())
        return units or [text]

    def _join_translated_units(self, parts):
        """子单元重新并回原物理行；中日韩目标语言不额外插入空格。"""
        target = self.cfg.get("tgt_lang", "中文")
        separator = "" if target in {"中文", "繁体中文", "粤语", "日文", "韩文"} else " "
        return separator.join(part.strip() for part in parts if part.strip())

    @staticmethod
    def _text_continuation_mode(previous_source, current_source):
        """文字线索只能表示可能衔接，不能替版面结构作出确认。"""
        previous = str(previous_source or "").strip()
        current = str(current_source or "").lstrip()
        if not previous or not current:
            return ""
        current = re.sub(r"^[\"'“”‘’([{]+", "", current)
        previous_core = re.sub(r"[\s\"'”’」』）)\]]+$", "", previous)
        if re.search(r"[.!?。！？…]$", previous_core):
            return ""
        if re.match(r"[a-z\u00e0-\u00f6\u00f8-\u00ff]", current):
            return "possible"
        if previous.endswith(("-", "–", "—", ",", ";")):
            return "possible"
        return ""

    @staticmethod
    def _continuation_hint_for_line(hints, line_index):
        """Read a trusted line-aligned hint without accepting arbitrary values."""
        value = None
        if isinstance(hints, (list, tuple)):
            if 0 <= int(line_index) < len(hints):
                value = hints[int(line_index)]
        elif isinstance(hints, dict):
            value = hints.get(str(line_index), hints.get(int(line_index)))
        return value if value in {"strong", "possible", "separate"} else None

    @staticmethod
    def _plain_unit(line):
        """保留纯文本行首尾空白，把中间正文作为可翻译内容。"""
        plain = re.match(r"^(\s*)(.*?)([ \t]*)$", line)
        prefix, text, suffix = plain.groups()
        if not text:
            return {"translate": False, "original": line}
        return {"translate": True, "prefix": prefix, "text": text, "suffix": suffix}

    @classmethod
    def _subtitle_units(cls, content, extension):
        """解析 SRT/WebVTT 物理行，锁住编号、时间轴和控制区块。"""
        lines = normalize_text_content(content)[0].split("\n")
        units = [cls._plain_unit(line) for line in lines]
        timestamp = re.compile(
            r"^\s*(?:\d{1,3}:)?\d{2}:\d{2}[,.]\d{3}\s+-->\s+"
            r"(?:\d{1,3}:)?\d{2}:\d{2}[,.]\d{3}(?:\s+.*)?$"
        )

        def preserve(index):
            units[index] = {"translate": False, "original": lines[index]}

        if extension == ".vtt" and lines:
            first = lines[0].lstrip("\ufeff").strip()
            if first.startswith("WEBVTT"):
                index = 0
                while index < len(lines):
                    preserve(index)
                    if index > 0 and not lines[index].strip():
                        break
                    index += 1

        block = False
        for index, line in enumerate(lines):
            stripped = line.strip()
            if extension == ".vtt":
                if re.match(r"^(?:NOTE(?:\s|$)|STYLE$|REGION$)", stripped):
                    block = True
                if block:
                    preserve(index)
                    if not stripped:
                        block = False
                    continue
            if not stripped:
                preserve(index)
                continue
            if timestamp.match(line):
                preserve(index)
                # SRT 在时间轴前只有数字序号；VTT 还允许任意 cue id。
                if index > 0 and lines[index - 1].strip():
                    previous = lines[index - 1].strip()
                    if extension == ".vtt" or previous.isdigit():
                        preserve(index - 1)
        return units

    @classmethod
    def _document_units(cls, content, filename=""):
        """按文件格式返回与物理行一一对应的翻译单元。"""
        content = normalize_text_content(content)[0]
        extension = os.path.splitext(str(filename))[1].lower()
        if extension in {".srt", ".vtt"}:
            return cls._subtitle_units(content, extension)
        if extension in BINARY_EXTENSIONS:
            return [cls._plain_unit(line) for line in content.split("\n")]
        units = []
        in_fence = False
        for line in content.split("\n"):
            unit, in_fence = cls._markdown_unit(line, in_fence)
            units.append(unit)
        return units

    def _context_text(self, history):
        """选择最近的完整 (原文, 译文) 单元，不从字符中间截断。"""
        if self._context_mode() == "unit":
            return ""
        unit_limit = max(0, int(self.cfg.get("context_units", 12)))
        if not history or unit_limit == 0:
            return ""
        token_limit, _ = self._context_token_caps()
        blocks = []
        used_tokens = 0
        for source, translated in reversed(history[-unit_limit:]):
            block = f"[原文]\n{source}\n[译文]\n{translated}"
            block_tokens = self._estimate_tokens(block)
            if used_tokens + block_tokens > token_limit:
                break
            blocks.append(block)
            used_tokens += block_tokens
        return "\n\n".join(reversed(blocks))

    def _future_context_text(self, sources):
        """选择接下来的完整原文单元，不从句子中间截断。"""
        if self._context_mode() == "unit":
            return ""
        unit_limit = max(0, int(self.cfg.get("future_context_units", 6)))
        if not sources or unit_limit == 0:
            return ""
        _, token_limit = self._context_token_caps()
        blocks = []
        used_tokens = 0
        for source in sources:
            for unit in self._semantic_units(source):
                unit = unit.strip()
                if not unit:
                    continue
                unit_tokens = self._estimate_tokens(unit)
                if len(blocks) >= unit_limit or used_tokens + unit_tokens > token_limit:
                    return "\n\n".join(blocks)
                blocks.append(unit)
                used_tokens += unit_tokens
        return "\n\n".join(blocks)

    def translate_batch(
        self, paras, history=None, future=None, previous_translations=None,
        preview=None, source_language=None, reference_text="",
        reference_kind="全文原文参考", continuation_hint=None,
    ):
        """逐段翻译；每个模型请求只对应一个由程序确定的位置。"""
        history = history if isinstance(history, list) else list(history or [])
        future = list(future or [])
        is_retranslation = previous_translations is not None
        previous_translations = list(previous_translations or [])
        if not paras:
            return True, []
        res = []
        for pos, p in enumerate(paras):
            source_units = self._semantic_units(p)
            translated_units = []
            for unit_pos, source_unit in enumerate(source_units):
                unit_future = source_units[unit_pos + 1:] + paras[pos + 1:] + future
                previous_source = history[-1][0] if history else ""
                if unit_pos == 0 and continuation_hint in {
                    "strong", "possible", "separate",
                }:
                    continuation_mode = (
                        "" if continuation_hint == "separate" else continuation_hint
                    )
                else:
                    continuation_mode = self._text_continuation_mode(
                        previous_source, source_unit
                    )

                def stream_unit(accumulated, current_pos=pos):
                    if accumulated and preview:
                        preview(
                            current_pos,
                            self._join_translated_units(
                                translated_units + [self._one_line(accumulated)]
                            ),
                        )

                previous = (
                    previous_translations[pos]
                    if len(source_units) == 1 and pos < len(previous_translations)
                    else ""
                )
                context_before = self._context_text(history)
                if continuation_mode and not context_before and history:
                    previous_source_text, previous_translated_text = history[-1]
                    context_before = (
                        f"[原文]\n{previous_source_text}\n"
                        f"[译文]\n{previous_translated_text}"
                    )
                ok2, o2 = self.translate_text(
                    source_unit,
                    context_before,
                    "" if reference_text else self._future_context_text(unit_future),
                    previous,
                    is_retranslation,
                    stream_unit if preview else None,
                    source_language,
                    reference_text,
                    reference_kind,
                    continuation_mode,
                )
                if not ok2:
                    return False, o2
                translated_unit = self._one_line(o2)
                if not translated_unit:
                    return False, "模型返回了空译文"
                translated_units.append(translated_unit)
                history.append((source_unit, translated_unit))
            res.append(self._join_translated_units(translated_units))
        return True, res

    @staticmethod
    def _markdown_unit(line, in_fence):
        """分离 Markdown 结构前缀与可翻译文本。"""
        fence = re.match(r"^(\s*)(```|~~~)", line)
        if fence:
            return {"translate": False, "original": line}, not in_fence
        if in_fence or not line.strip() or re.match(r"^(?: {4}|\t)", line):
            return {"translate": False, "original": line}, in_fence
        if re.match(r"^\s{0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$", line):
            return {"translate": False, "original": line}, in_fence

        heading = re.match(r"^(\s{0,3}#{1,6}[ \t]+)(.*?)([ \t]*)$", line)
        if heading:
            prefix, text, suffix = heading.groups()
        else:
            task = re.match(
                r"^(\s*(?:>\s*)*(?:[-+*]|\d+[.)])\s+\[[ xX]\]\s+)(.*?)([ \t]*)$",
                line,
            )
            if task:
                prefix, text, suffix = task.groups()
            else:
                listing = re.match(
                    r"^(\s*(?:>\s*)*(?:[-+*]|\d+[.)])\s+)(.*?)([ \t]*)$",
                    line,
                )
                if listing:
                    prefix, text, suffix = listing.groups()
                else:
                    quote = re.match(r"^(\s*(?:>\s*)+)(.*?)([ \t]*)$", line)
                    if quote:
                        prefix, text, suffix = quote.groups()
                    else:
                        plain = re.match(r"^(\s*)(.*?)([ \t]*)$", line)
                        prefix, text, suffix = plain.groups()
        if not text:
            return {"translate": False, "original": line}, in_fence
        return {"translate": True, "prefix": prefix, "text": text, "suffix": suffix}, in_fence

    @staticmethod
    def _translated_unit_text(unit, translated_line):
        """去掉已保留的 Markdown 前后缀，提取可作为上文参考的译文正文。"""
        text = str(translated_line)
        prefix = unit.get("prefix", "")
        suffix = unit.get("suffix", "")
        if prefix and text.startswith(prefix):
            text = text[len(prefix):]
        if suffix and text.endswith(suffix):
            text = text[:-len(suffix)]
        return text.strip()

    def retranslate_line(
        self, content, translated_content, line_index, filename="",
        continuation_hints=None,
    ):
        """使用文件上下文重译一个物理行，返回 (新行, 更新后的完整译文)。"""
        content = normalize_text_content(content)[0]
        translated_content = normalize_text_content(translated_content)[0]
        source_lines = content.split("\n")
        try:
            line_index = int(line_index)
        except (TypeError, ValueError):
            raise ValueError("行号无效")
        if line_index < 0 or line_index >= len(source_lines):
            raise ValueError("行号超出文件范围")

        units = self._document_units(content, filename)
        target = units[line_index]
        if not target.get("translate"):
            raise ValueError("该行是空行、代码或 Markdown 结构，不能单独重译")

        translated_lines = translated_content.split("\n")
        if len(translated_lines) < len(source_lines):
            translated_lines.extend(source_lines[len(translated_lines):])

        history = []
        for idx, unit in enumerate(units[:line_index]):
            if not unit.get("translate") or idx >= len(translated_lines):
                continue
            translated = self._translated_unit_text(unit, translated_lines[idx])
            if translated:
                history.append((unit["text"], translated))

        future = [
            unit["text"] for unit in units[line_index + 1:]
            if unit.get("translate")
        ]
        previous_translation = (
            self._translated_unit_text(target, translated_lines[line_index])
            if line_index < len(translated_lines) else ""
        )
        translatable = [
            (idx, unit["text"])
            for idx, unit in enumerate(units)
            if unit.get("translate")
        ]
        target_position = next(
            pos for pos, (idx, _) in enumerate(translatable) if idx == line_index
        )
        reference = self._active_reference_plan(
            content, translatable, filename
        )[target_position]
        ok, translated = self.translate_batch(
            [target["text"]], history, future, [previous_translation],
            source_language=(
                detect_lang(content)
                if self.cfg.get("src_lang", "自动判断") == "自动判断"
                else self.cfg.get("src_lang")
            ),
            reference_text=reference["text"],
            reference_kind=reference["kind"],
            continuation_hint=self._continuation_hint_for_line(
                continuation_hints, line_index
            ),
        )
        if not ok:
            return False, translated

        output_line = target["prefix"] + translated[0] + target["suffix"]
        translated_lines[line_index] = output_line
        return True, (output_line, "\n".join(translated_lines))

    def translate_content(
        self, content, filename="", emit=None, progress=None,
        previous_content=None, resume_lines=None, checkpoint=None,
        continuation_hints=None,
    ):
        """翻译文件内容，逐个物理位置生成并保持 Markdown 结构。"""
        content = normalize_text_content(content)[0]
        lines = content.split("\n")
        out = list(lines)
        units = self._document_units(content, filename)
        for line_index, line in enumerate(lines):
            unit = units[line_index]
            if not unit["translate"] and line.strip() and emit:
                emit(line_index, line)

        translatable = [
            (line_index, unit["text"])
            for line_index, unit in enumerate(units)
            if unit["translate"]
        ]
        configured_source = self.cfg.get("src_lang", "自动判断")
        document_source = (
            detect_lang("\n".join(text for _, text in translatable))
            if configured_source == "自动判断" and translatable
            else configured_source
        )
        reference_plan = self._active_reference_plan(content, translatable, filename)
        resumed = {}
        for raw_line, translated_line in (resume_lines or {}).items():
            try:
                line_index = int(raw_line)
            except (TypeError, ValueError):
                continue
            if isinstance(translated_line, str) and translated_line.strip():
                resumed[line_index] = translated_line

        total_units = len(translatable)
        completed_units = 0
        history = []
        if progress:
            progress(0, total_units)

        for position, (line_index, source) in enumerate(translatable):
            unit = units[line_index]
            resumed_line = resumed.get(line_index)
            if resumed_line is not None:
                translated_body = self._translated_unit_text(unit, resumed_line)
                if translated_body:
                    out[line_index] = resumed_line
                    history.append((source, translated_body))
                    if emit:
                        emit(line_index, resumed_line)
                    if checkpoint:
                        checkpoint(line_index, resumed_line)
                    completed_units += 1
                    if progress:
                        progress(completed_units, total_units)
                    continue

            reference = reference_plan[position]
            remaining_sources = [
                text for _, text in translatable[position + 1:]
            ]

            def preview_current(_position, text, current_line=line_index, current_unit=unit):
                if emit:
                    emit(
                        current_line,
                        current_unit["prefix"] + text + current_unit["suffix"]
                        if text else "",
                    )

            # 整个文件重译不把旧版译文喂回模型；previous_content 只用于
            # 选择重译提示。单行重译仍由 retranslate_line() 提供旧译文。
            previous_batch = [""] if previous_content is not None else None
            ok, translated = self.translate_batch(
                [source],
                list(history),
                remaining_sources,
                previous_batch,
                preview_current,
                document_source,
                reference["text"],
                reference["kind"],
                self._continuation_hint_for_line(
                    continuation_hints, line_index
                ),
            )
            if not ok:
                return False, translated

            translated_body = translated[0]
            output_line = unit["prefix"] + translated_body + unit["suffix"]
            out[line_index] = output_line
            history.append((source, translated_body))
            if emit:
                emit(line_index, output_line)
            if checkpoint:
                checkpoint(line_index, output_line)
            completed_units += 1
            if progress:
                progress(completed_units, total_units)

        return True, "\n".join(out)

def run_job(job_id):
    """后台翻译任务，支持立即中断与优先级插队。"""
    job = JOBS[job_id]
    cfg = job["config"]
    translator = Translator(cfg)
    with JOBS_LOCK:
        job["translator"] = translator
    files = job["files"]
    job["total"] = len(files)
    job["done_names"] = []

    def emit(kind, **kw):
        enqueue_job_event(job, {"kind": kind, **kw})

    pending = list(range(len(files)))   # 待处理顺序(原始索引)
    done = 0
    try:
        while pending:
            if job.get("interrupt"):
                break
            prio = job.get("priority") or []
            chosen = None
            for i in pending:               # 优先插队: 选优先级里的第一个
                if files[i]["name"] in prio:
                    chosen = i
                    break
            if chosen is None:
                chosen = pending[0]
            pending.remove(chosen)
            idx = chosen
            name, content = files[idx]["name"], files[idx].get("content", "")
            job["current"] = name
            job["current_file_done"] = 0
            job["current_file_total"] = 0
            emit("progress", done=done, total=len(files), current=name)

            def emit_line(line_no, text):
                with JOBS_LOCK:
                    job["partials"].setdefault(name, {})[str(line_no)] = text
                emit("chunk", file=name, idx=idx, line=line_no, text=text)

            def emit_file_progress(file_done, file_total):
                with JOBS_LOCK:
                    job["current_file_done"] = file_done
                    job["current_file_total"] = file_total
                emit("file_progress", file=name, idx=idx, done=file_done, total=file_total)

            def checkpoint_line(line_no, text):
                with JOBS_LOCK:
                    job["partials"].setdefault(name, {})[str(line_no)] = text
                    job["completed_partials"].setdefault(name, {})[str(line_no)] = text
                emit("line_done", file=name, idx=idx, line=line_no, text=text)

            try:
                previous_content = (
                    files[idx].get("translated_content")
                    if files[idx].get("retranslate") else None
                )
                ok, out = translator.translate_content(
                    content, name, emit=emit_line, progress=emit_file_progress,
                    previous_content=previous_content,
                    resume_lines=files[idx].get("resume_lines"),
                    checkpoint=checkpoint_line,
                    continuation_hints=files[idx].get("continuation_hints"),
                )
                if job.get("interrupt") or translator.interrupted:
                    # 中断只停止当前模型请求；已流式显示的内容继续保留，
                    # 便于用户检查中断前的结果，不再重置整个文件预览。
                    break
                if not ok:
                    job["results"].append({"name": name, "status": "error", "error": out})
                    emit("fail", file=name, msg=out)
                else:
                    target = cfg.get("tgt_lang", "中文")
                    out_name = save_translation_output(
                        files[idx],
                        out,
                        target,
                        pdf_strict_layout=cfg.get("pdf_strict_layout", True),
                    )
                    job["results"].append({
                        "name": name, "output_name": out_name,
                        "target_language": target, "status": "ok", "content": out,
                        "source_format": files[idx].get("source_format", "text"),
                    })
                    job["done_names"].append(name)
                    emit(
                        "file_done", file=name, idx=idx, output_name=out_name,
                        done=job.get("current_file_total", 0),
                        total=job.get("current_file_total", 0),
                    )
                    with JOBS_LOCK:
                        # 成功文件已经有完整 result，无需再在内存中保存逐行副本。
                        job["completed_partials"].pop(name, None)
            except Exception as e:
                if job.get("interrupt") or translator.interrupted:
                    # socket 被 interrupt() 主动关闭时也保留现有预览。
                    break
                job["results"].append({"name": name, "status": "error", "error": str(e)})
                emit("fail", file=name, msg=str(e))
            with JOBS_LOCK:
                job["partials"].pop(name, None)
            done += 1
            job["done"] = done
            emit("overall_progress", done=done, total=len(files))
    finally:
        translator.close()
        with JOBS_LOCK:
            job.pop("translator", None)
            for file_info in job.get("files", []):
                file_info.pop("translated_content", None)
                file_info.pop("resume_lines", None)

    job["current"] = ""
    job["finished_at"] = time.time()
    if job.get("interrupt"):
        job["status"] = "interrupted"
        emit("interrupted", total=len(files))
    else:
        job["status"] = "done"
        emit("done", total=len(files))


def prune_jobs():
    """保留正在运行的任务，清理超时或过多的已结束任务。"""
    now = time.time()
    with JOBS_LOCK:
        finished = [
            (jid, job) for jid, job in JOBS.items()
            if job.get("status") != "running"
        ]
        remove = {
            jid for jid, job in finished
            if now - job.get("finished_at", job.get("created_at", now)) > JOB_TTL_SECONDS
        }
        survivors = sorted(
            ((jid, job) for jid, job in finished if jid not in remove),
            key=lambda pair: pair[1].get("finished_at", pair[1].get("created_at", 0)),
            reverse=True,
        )
        retained_chars = 0
        retained_count = 0
        for jid, job in survivors:
            text_chars = _job_text_chars(job)
            exceeds_count = retained_count >= MAX_FINISHED_JOBS
            exceeds_size = (
                retained_count > 0
                and retained_chars + text_chars > MAX_FINISHED_JOB_TEXT_CHARS
            )
            if exceeds_count or exceeds_size:
                remove.add(jid)
                continue
            retained_count += 1
            retained_chars += text_chars
        for jid in remove:
            JOBS.pop(jid, None)


def _job_text_chars(value):
    """估算任务中保留的字符量，避免已完成大任务无界占内存。"""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(_job_text_chars(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_job_text_chars(item) for item in value)
    return 0


def job_status_snapshot(job_id, requested_full=False):
    """在锁内只复制内存状态；JSON 序列化和网络发送留到锁外。"""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        snapshot = {
            key: job.get(key)
            for key in (
                "status",
                "total",
                "done",
                "current",
                "error",
                "current_file_done",
                "current_file_total",
            )
        }
        full = requested_full or job.get("status") != "running"
        if full:
            snapshot["results"] = [dict(result) for result in job.get("results", [])]
        else:
            snapshot["results"] = [
                {
                    key: result.get(key)
                    for key in (
                        "name",
                        "status",
                        "error",
                        "output_name",
                        "target_language",
                    )
                }
                for result in job.get("results", [])
            ]
        snapshot["names"] = [item["name"] for item in job.get("files", [])]
        if requested_full:
            snapshot["sources"] = [
                {
                    "name": item["name"],
                    "content": item.get("content", ""),
                    "source_id": item.get("source_id"),
                    "source_format": item.get("source_format", "text"),
                    "source_sha256": item.get("source_sha256"),
                    "pdf_page_count": item.get("pdf_page_count"),
                    "pdf_page_start": item.get("pdf_page_start"),
                    "pdf_page_end": item.get("pdf_page_end"),
                    "pdf_page_selection": item.get("pdf_page_selection"),
                    "text_eol": item.get("text_eol", "lf"),
                    "text_bom": bool(item.get("text_bom", False)),
                }
                for item in job.get("files", [])
            ]
        snapshot["done_names"] = list(job.get("done_names", []))
        snapshot["partials"] = {
            name: dict(lines) for name, lines in job.get("partials", {}).items()
        }
        snapshot["completed_partials"] = {
            name: dict(lines)
            for name, lines in job.get("completed_partials", {}).items()
        }
    return snapshot


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _request_allowed(self, mutating=False):
        """只接受本机同源浏览器请求，阻断 DNS rebinding 与简单 CSRF。"""
        def reject(message, code=403):
            self.close_connection = True
            self._send_json({"error": message}, code)
            return False

        host_values = self.headers.get_all("Host", [])
        if len(host_values) != 1:
            return reject("invalid host")
        try:
            host_parts = urlsplit("//" + host_values[0])
            host_name = (host_parts.hostname or "").lower()
            host_port = host_parts.port
        except ValueError:
            return reject("invalid host")
        server_port = int(self.server.server_address[1])
        if (
            host_name not in {"localhost", "127.0.0.1", "::1"}
            or host_port not in {None, server_port}
        ):
            return reject("local access only")

        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            return reject("cross-site request rejected")

        origin_values = self.headers.get_all("Origin", [])
        if len(origin_values) > 1:
            return reject("invalid origin")
        if origin_values:
            try:
                origin = urlsplit(origin_values[0])
                origin_name = (origin.hostname or "").lower()
                origin_port = origin.port or (
                    443 if origin.scheme.lower() == "https" else 80
                )
            except ValueError:
                return reject("invalid origin")
            if (
                origin.scheme.lower() != "http"
                or origin_name != host_name
                or origin_port != server_port
            ):
                return reject("cross-origin request rejected")

        if mutating and self.command == "POST":
            content_type = (
                self.headers.get("Content-Type", "")
                .split(";", 1)[0].strip().lower()
            )
            request_path = unquote(urlsplit(self.path).path)
            allowed_types = {"application/json"}
            if request_path == "/api/import":
                allowed_types.add("application/octet-stream")
            if content_type not in allowed_types:
                return reject("application/json required", 415)
        return True

    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        ln = int(self.headers.get("Content-Length", 0))
        if ln <= 0:
            return {}
        return json.loads(self.rfile.read(ln).decode("utf-8"))

    def _request_parts(self):
        parsed = urlsplit(self.path)
        return unquote(parsed.path), parse_qs(parsed.query)

    def do_GET(self):
        if not self._request_allowed():
            return
        path, query = self._request_parts()
        if path == "/" or path == "/index.html":
            self._serve_file(os.path.join(STATIC_DIR, "index.html"))
        elif path == "/api/status":
            job = query.get("job", [""])[0]
            base = job_status_snapshot(
                job, requested_full=query.get("full", [""])[0] == "1"
            )
            if base is None:
                self._send_json({"status": "notfound"})
                return
            self._send_json(base)
        elif path == "/api/running":
            with JOBS_LOCK:
                running = [jid for jid, j in JOBS.items() if j.get("status") == "running"]
            self._send_json({"job": running[0] if running else None})
        elif path == "/api/source":
            source_id = query.get("id", [""])[0]
            source_format = query.get("format", [""])[0]
            source_sha256 = query.get("sha256", [""])[0]
            requested_page_start = query.get("page_start", [None])[0]
            requested_page_end = query.get("page_end", [None])[0]
            requested_page_selection = query.get("pages", [None])[0]
            try:
                data = load_cached_source(
                    SOURCE_CACHE_DIR, source_id, source_format, source_sha256
                )
            except (DocumentFormatError, OSError) as exc:
                self._send_json({"error": str(exc)}, 404)
                return
            try:
                if str(source_format).lower().lstrip(".") == "pdf":
                    page_count = pdf_page_count(data)
                    selected_pages = normalize_pdf_page_selection(
                        page_count,
                        requested_page_selection,
                        requested_page_start,
                        requested_page_end,
                    )
                    page_selection = format_pdf_page_selection(
                        selected_pages, page_count
                    )
                    page_start = selected_pages[0]
                    page_end = selected_pages[-1]
                    content = extract_binary_text(
                        source_format,
                        data,
                        pdf_page_selection=page_selection,
                    )
                else:
                    page_count = page_start = page_end = page_selection = None
                    content = extract_binary_text(source_format, data)
            except (DocumentFormatError, OSError, ValueError) as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            result = {
                "ok": True,
                "content": content,
                "source_format": source_format,
                "source_sha256": binary_digest(data),
            }
            if page_count is not None:
                result.update({
                    "pdf_page_count": page_count,
                    "pdf_page_start": page_start,
                    "pdf_page_end": page_end,
                    "pdf_page_selection": page_selection,
                    "pdf_extraction_version": PDF_EXTRACTION_VERSION,
                })
            self._send_json(result)
        elif path == "/api/history":
            files_list = []
            with OUTPUT_INDEX_LOCK:
                output_index = _read_output_index()
            if os.path.isdir(OUTPUTS_DIR):
                for fn in sorted(os.listdir(OUTPUTS_DIR)):
                    if fn.lower().endswith((".md", ".markdown", ".txt", ".text", ".srt", ".vtt", ".docx", ".epub", ".pdf")):
                        p = os.path.join(OUTPUTS_DIR, fn)
                        entry = {"name": fn, "size": os.path.getsize(p)}
                        meta = output_index.get(fn, {})
                        entry.update({
                            "source_name": meta.get("source_name"),
                            "source_sha256": meta.get("source_sha256"),
                            "source_format": meta.get("source_format", "text"),
                            "target_language": meta.get("target_language"),
                            "pdf_page_count": meta.get("pdf_page_count"),
                            "pdf_page_start": meta.get("pdf_page_start"),
                            "pdf_page_end": meta.get("pdf_page_end"),
                            "pdf_page_selection": meta.get("pdf_page_selection"),
                            "pdf_extraction_version": meta.get("pdf_extraction_version"),
                            "pdf_strict_layout": meta.get("pdf_strict_layout"),
                        })
                        files_list.append(entry)
            self._send_json({"files": files_list})
        elif path == "/api/output":
            name = query.get("name", [""])[0]
            if not name or name != os.path.basename(name) or name.endswith(".meta.json"):
                self._send_json({"error": "invalid output name"}, 400)
                return
            output_path = os.path.join(OUTPUTS_DIR, name)
            if not os.path.isfile(output_path):
                self._send_json({"error": "not found"}, 404)
                return
            try:
                with OUTPUT_INDEX_LOCK:
                    metadata = _read_output_index().get(name, {})
                content = read_output_preview(
                    output_path, name, metadata.get("preview_content")
                )
            except (OSError, DocumentFormatError) as exc:
                self._send_json({"error": f"读取译文失败: {exc}"}, 500)
                return
            self._send_json({
                "name": name,
                "content": content,
                "source_format": document_extension(name).lstrip(".") or "text",
            })
        elif path == "/api/output-file":
            name = query.get("name", [""])[0]
            if not name or name != os.path.basename(name) or name.endswith(".meta.json"):
                self._send_json({"error": "invalid output name"}, 400)
                return
            output_path = os.path.join(OUTPUTS_DIR, name)
            if not os.path.isfile(output_path):
                self._send_json({"error": "not found"}, 404)
                return
            try:
                with open(output_path, "rb") as handle:
                    data = handle.read()
            except OSError as exc:
                self._send_json({"error": f"读取译文失败: {exc}"}, 500)
                return
            content_types = {
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".epub": "application/epub+zip",
                ".pdf": "application/pdf",
                ".srt": "application/x-subrip; charset=utf-8",
                ".vtt": "text/vtt; charset=utf-8",
            }
            self.send_response(200)
            self.send_header(
                "Content-Type",
                content_types.get(document_extension(name), "application/octet-stream"),
            )
            self.send_header(
                "Content-Disposition",
                "attachment; filename*=UTF-8''" + quote(name, safe=""),
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        elif path == "/api/stream":
            job = query.get("job", [""])[0]
            with JOBS_LOCK:
                j = JOBS.get(job)
            if not j:
                self._send_json({"status": "notfound"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last_ping = time.monotonic()
            try:
                while True:
                    try:
                        ev = j["q"].get(timeout=1.0)
                    except queue.Empty:
                        with JOBS_LOCK:
                            st = j.get("status")
                        if st in ("done", "error", "interrupted"):
                            terminal = b"interrupted" if st == "interrupted" else b"done"
                            self.wfile.write(b"event: " + terminal + b"\ndata: {}\n\n")
                            self.wfile.flush()
                            break
                        if time.monotonic() - last_ping >= 15:
                            self.wfile.write(b": keep-alive\n\n")
                            self.wfile.flush()
                            last_ping = time.monotonic()
                        continue
                    kind = ev.get("kind", "event")
                    data = json.dumps(ev, ensure_ascii=False).encode("utf-8")
                    self.wfile.write(b"event: " + kind.encode("utf-8") + b"\ndata: " + data + b"\n\n")
                    self.wfile.flush()
                    if kind in ("done", "interrupted"):
                        break
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
            return
        elif path == "/api/download":
            job = query.get("job", [""])[0]
            with JOBS_LOCK:
                j = JOBS.get(job)
            if not j:
                self._send_json({"status": "notfound"})
                return
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for r in j.get("results", []):
                    if r.get("status") == "ok":
                        output_name = r.get("output_name") or output_filename(
                            r["name"], j.get("config", {}).get("tgt_lang", "中文")
                        )
                        output_path = os.path.join(OUTPUTS_DIR, output_name)
                        if os.path.isfile(output_path):
                            z.write(output_path, output_name)
                        else:
                            z.writestr(output_name, r["content"])
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="translated.zip"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        else:
            self._serve_file(os.path.join(STATIC_DIR, path.lstrip("/")))

    def do_DELETE(self):
        if not self._request_allowed(mutating=True):
            return
        path, query = self._request_parts()
        if path == "/api/source":
            source_id = query.get("id", [""])[0]
            try:
                deleted = delete_binary_source(SOURCE_CACHE_DIR, source_id)
            except DocumentFormatError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            self._send_json({"ok": True, "deleted": deleted})
            return
        if path == "/api/cache":
            try:
                cached, legacy = clear_output_cache()
            except OSError as exc:
                self._send_json({"error": f"清除缓存失败: {exc}"}, 500)
                return
            with JOBS_LOCK:
                finished = [
                    job_id for job_id, job in JOBS.items()
                    if job.get("status") != "running"
                ]
                for job_id in finished:
                    JOBS.pop(job_id, None)
            self._send_json({
                "ok": True,
                "records": cached,
                "legacy_files": legacy,
                "finished_jobs": len(finished),
            })
            return
        if path != "/api/output":
            self._send_json({"error": "not found"}, 404)
            return

        name = query.get("name", [""])[0]
        if not name or name != os.path.basename(name) or name.endswith(".meta.json"):
            self._send_json({"error": "invalid output name"}, 400)
            return

        output_path = os.path.join(OUTPUTS_DIR, name)
        deleted = []
        try:
            for candidate in (output_path, output_path + ".meta.json"):
                if os.path.isfile(candidate):
                    os.remove(candidate)
                    deleted.append(os.path.basename(candidate))
            remove_output_metadata(name)
        except OSError as exc:
            self._send_json({"error": f"删除输出失败: {exc}"}, 500)
            return

        # 同步移除内存中的同名结果，避免已删除的译文仍能从旧任务 ZIP 中导出。
        with JOBS_LOCK:
            for job in JOBS.values():
                job["results"] = [
                    result for result in job.get("results", [])
                    if result.get("output_name") != name
                ]
        self._send_json({"ok": True, "name": name, "deleted": deleted})

    def _serve_file(self, path):
        static_root = os.path.realpath(STATIC_DIR)
        resolved = os.path.realpath(path)
        try:
            inside_static = os.path.commonpath((static_root, resolved)) == static_root
        except ValueError:
            inside_static = False
        if not inside_static or not os.path.isfile(resolved):
            self.send_response(404)
            self.end_headers()
            return
        ext = os.path.splitext(resolved)[1]
        with open(resolved, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        if ext in (".html", ".css", ".js"):
            self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if not self._request_allowed(mutating=True):
            return
        path = self.path.split("?")[0]
        if path == "/api/test":
            try:
                body = self._read_body()
            except Exception:
                self._send_json({"error": "bad json"}, 400)
                return
            server = body.get("server", "") if isinstance(body, dict) else ""
            ok, msg = test_server(server)
            self._send_json({"ok": ok, "msg": msg})
            return
        if path == "/api/import":
            try:
                content_type = (
                    self.headers.get("Content-Type", "")
                    .split(";", 1)[0].strip().lower()
                )
                if content_type == "application/octet-stream":
                    _, query = self._request_parts()
                    name = query.get("name", [""])[0]
                    if not name or name != os.path.basename(name):
                        raise DocumentFormatError("导入文件名无效")
                    imported = cache_binary_source_stream(
                        SOURCE_CACHE_DIR,
                        name,
                        self.rfile,
                        self.headers.get("Content-Length", ""),
                    )
                else:
                    # 保留旧 JSON 接口，避免已打开的旧页面在刷新前失效。
                    body = self._read_body()
                    if not isinstance(body, dict):
                        raise DocumentFormatError("导入请求无效")
                    name = body.get("name", "")
                    encoded = body.get("data_base64", "")
                    if (
                        not isinstance(name, str)
                        or not name
                        or not isinstance(encoded, str)
                    ):
                        raise DocumentFormatError("导入请求无效")
                    try:
                        data = base64.b64decode(encoded, validate=True)
                    except (ValueError, TypeError) as exc:
                        raise DocumentFormatError("文档数据不是有效的 Base64") from exc
                    imported = cache_binary_source(SOURCE_CACHE_DIR, name, data)
            except (DocumentFormatError, OSError, ValueError, UnicodeError) as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            self._send_json({"ok": True, "name": name, **imported})
            return
        if path == "/api/update-line":
            try:
                body = self._read_body()
            except Exception:
                self._send_json({"error": "bad json"}, 400)
                return
            if not isinstance(body, dict):
                self._send_json({"error": "bad json"}, 400)
                return
            cfg = body.get("config", {})
            name = body.get("name", "")
            content = body.get("content")
            translated_content = body.get("translated_content")
            edited_text = body.get("text")
            source_id = body.get("source_id")
            source_format = body.get("source_format", "text")
            source_sha256 = body.get("source_sha256", "")
            try:
                line_index = int(body.get("line"))
            except (TypeError, ValueError):
                self._send_json({"error": "行号无效"}, 400)
                return
            if (not isinstance(cfg, dict) or not isinstance(name, str) or not name
                    or not isinstance(content, str) or not isinstance(translated_content, str)
                    or not isinstance(edited_text, str)):
                self._send_json({"error": "invalid line update request"}, 400)
                return

            content, detected_eol, detected_bom = normalize_text_content(content)
            translated_content = normalize_text_content(translated_content)[0]
            text_eol = _text_eol(body.get("text_eol"), detected_eol)
            text_bom = bool(body.get("text_bom", detected_bom))

            source_lines = content.split("\n")
            if line_index < 0 or line_index >= len(source_lines):
                self._send_json({"error": "行号超出文件范围"}, 400)
                return
            units = Translator._document_units(content, name)
            unit = units[line_index]
            if not unit.get("translate"):
                self._send_json({"error": "该行不能编辑译文"}, 400)
                return

            translated_lines = translated_content.split("\n")
            if len(translated_lines) < len(source_lines):
                translated_lines.extend(source_lines[len(translated_lines):])
            edited_body = Translator._one_line(edited_text)
            output_line = unit["prefix"] + edited_body + unit["suffix"]
            translated_lines[line_index] = output_line
            updated_content = "\n".join(translated_lines)
            target = cfg.get("tgt_lang", "中文")
            try:
                out_name = save_translation_output(
                    {
                        "name": name,
                        "content": content,
                        "source_id": source_id,
                        "source_format": source_format,
                        "source_sha256": source_sha256,
                        "pdf_page_count": body.get("pdf_page_count"),
                        "pdf_page_start": body.get("pdf_page_start"),
                        "pdf_page_end": body.get("pdf_page_end"),
                        "pdf_page_selection": body.get("pdf_page_selection"),
                        "text_eol": text_eol,
                        "text_bom": text_bom,
                    },
                    updated_content,
                    target,
                    pdf_strict_layout=cfg.get("pdf_strict_layout", True),
                )
            except (OSError, DocumentFormatError) as exc:
                self._send_json({"error": f"保存编辑失败: {exc}"}, 500)
                return

            with JOBS_LOCK:
                for job in JOBS.values():
                    for old_result in job.get("results", []):
                        if old_result.get("output_name") == out_name:
                            old_result["content"] = updated_content
                            old_result["target_language"] = target
            self._send_json({
                "ok": True, "line": line_index, "text": output_line,
                "content": updated_content, "output_name": out_name,
                "target_language": target,
            })
            return
        if path == "/api/retranslate-line":
            try:
                body = self._read_body()
            except Exception:
                self._send_json({"error": "bad json"}, 400)
                return
            if not isinstance(body, dict):
                self._send_json({"error": "bad json"}, 400)
                return
            cfg = body.get("config", {})
            name = body.get("name", "")
            content = body.get("content")
            translated_content = body.get("translated_content")
            line_index = body.get("line")
            source_id = body.get("source_id")
            source_format = body.get("source_format", "text")
            source_sha256 = body.get("source_sha256", "")
            if (not isinstance(cfg, dict) or not isinstance(name, str) or not name
                    or not isinstance(content, str) or not isinstance(translated_content, str)):
                self._send_json({"error": "invalid retranslation request"}, 400)
                return

            content, detected_eol, detected_bom = normalize_text_content(content)
            translated_content = normalize_text_content(translated_content)[0]
            text_eol = _text_eol(body.get("text_eol"), detected_eol)
            text_bom = bool(body.get("text_bom", detected_bom))

            continuation_hints = None
            if str(source_format).lower().lstrip(".") == "pdf":
                try:
                    source_data = load_cached_source(
                        SOURCE_CACHE_DIR, source_id, "pdf", source_sha256
                    )
                    count = pdf_page_count(source_data)
                    selected_pages = normalize_pdf_page_selection(
                        count,
                        body.get("pdf_page_selection"),
                        body.get("pdf_page_start"),
                        body.get("pdf_page_end"),
                    )
                    extracted_pdf = extract_pdf_translation_data(
                        source_data,
                        page_selection=format_pdf_page_selection(
                            selected_pages, count
                        ),
                    )
                    if extracted_pdf["content"] != content:
                        raise DocumentFormatError(
                            "PDF 页码选择与原文预览不一致，请重新选择页面"
                        )
                    continuation_hints = extracted_pdf["continuation_hints"]
                except (DocumentFormatError, OSError) as exc:
                    self._send_json({"error": str(exc)}, 400)
                    return

            translator = Translator(cfg)
            try:
                ok, result = translator.retranslate_line(
                    content, translated_content, line_index, name,
                    continuation_hints=continuation_hints,
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            finally:
                translator.close()
            if not ok:
                self._send_json({"error": result}, 502)
                return

            output_line, updated_content = result
            target = cfg.get("tgt_lang", "中文")
            try:
                out_name = save_translation_output(
                    {
                        "name": name,
                        "content": content,
                        "source_id": source_id,
                        "source_format": source_format,
                        "source_sha256": source_sha256,
                        "pdf_page_count": body.get("pdf_page_count"),
                        "pdf_page_start": body.get("pdf_page_start"),
                        "pdf_page_end": body.get("pdf_page_end"),
                        "pdf_page_selection": body.get("pdf_page_selection"),
                        "text_eol": text_eol,
                        "text_bom": text_bom,
                    },
                    updated_content,
                    target,
                    pdf_strict_layout=cfg.get("pdf_strict_layout", True),
                )
            except (OSError, DocumentFormatError) as exc:
                self._send_json({"error": f"保存重译结果失败: {exc}"}, 500)
                return

            with JOBS_LOCK:
                for job in JOBS.values():
                    for old_result in job.get("results", []):
                        if old_result.get("output_name") == out_name:
                            old_result["content"] = updated_content
                            old_result["target_language"] = target
            self._send_json({
                "ok": True, "line": int(line_index), "text": output_line,
                "content": updated_content, "output_name": out_name,
                "target_language": target,
            })
            return
        if path == "/api/interrupt":
            try:
                body = self._read_body()
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}
            jid = body.get("job") or self.path.split("job=")[-1].split("&")[0]
            with JOBS_LOCK:
                j = JOBS.get(jid)
                if j:
                    j["interrupt"] = True
                    translator = j.get("translator")
                else:
                    translator = None
            if translator:
                translator.interrupt()
            self._send_json({"ok": bool(j)})
            return
        if path == "/api/prioritize":
            try:
                body = self._read_body()
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}
            jid = body.get("job")
            with JOBS_LOCK:
                j = JOBS.get(jid)
                if j:
                    j["priority"] = body.get("names", [])
            self._send_json({"ok": bool(j)})
            return
        if path != "/api/translate":
            self._send_json({"error": "unknown path"}, 404)
            return
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "bad json"}, 400)
            return
        if not isinstance(body, dict):
            self._send_json({"error": "bad json"}, 400)
            return
        cfg = body.get("config", {})
        files = body.get("files", [])
        if not isinstance(cfg, dict):
            self._send_json({"error": "invalid config"}, 400)
            return
        if not files:
            self._send_json({"error": "no files"}, 400)
            return
        if not all(isinstance(f, dict) and isinstance(f.get("name"), str)
                   and isinstance(f.get("content", ""), str) for f in files):
            self._send_json({"error": "invalid files"}, 400)
            return
        names = [file_info["name"] for file_info in files]
        if any(
            not name
            or name != os.path.basename(name.replace("\\", "/"))
            for name in names
        ):
            self._send_json({"error": "invalid file name"}, 400)
            return
        if len({name.casefold() for name in names}) != len(names):
            self._send_json({"error": "同一任务中不能包含同名文件"}, 400)
            return
        if not all(
            not f.get("retranslate")
            or isinstance(f.get("translated_content"), str)
            for f in files
        ):
            self._send_json({"error": "invalid retranslation files"}, 400)
            return
        if not all(
            f.get("resume_lines") is None
            or (
                isinstance(f.get("resume_lines"), dict)
                and all(isinstance(value, str) for value in f["resume_lines"].values())
            )
            for f in files
        ):
            self._send_json({"error": "invalid resume lines"}, 400)
            return
        try:
            for file_info in files:
                extension = document_extension(file_info["name"])
                source_format = str(file_info.get("source_format") or "text").lower()
                if extension in BINARY_EXTENSIONS:
                    if source_format != extension.lstrip("."):
                        raise DocumentFormatError(
                            f"{file_info['name']} 缺少二进制原文，请重新添加文件"
                        )
                    source_data = load_cached_source(
                        SOURCE_CACHE_DIR,
                        file_info.get("source_id"),
                        source_format,
                        file_info.get("source_sha256", ""),
                    )
                    if source_format == "pdf":
                        count = pdf_page_count(source_data)
                        selected_pages = normalize_pdf_page_selection(
                            count,
                            file_info.get("pdf_page_selection"),
                            file_info.get("pdf_page_start"),
                            file_info.get("pdf_page_end"),
                        )
                        page_selection = format_pdf_page_selection(
                            selected_pages, count
                        )
                        extracted_pdf = extract_pdf_translation_data(
                            source_data,
                            page_selection=page_selection,
                        )
                        expected_content = extracted_pdf["content"]
                        if expected_content != file_info.get("content", ""):
                            raise DocumentFormatError(
                                f"{file_info['name']} 的 PDF 页码选择与原文预览不一致，请重新选择页面"
                            )
                        file_info["pdf_page_count"] = count
                        file_info["pdf_page_start"] = selected_pages[0]
                        file_info["pdf_page_end"] = selected_pages[-1]
                        file_info["pdf_page_selection"] = page_selection
                        file_info["continuation_hints"] = extracted_pdf[
                            "continuation_hints"
                        ]
                elif is_binary_document(source_format):
                    raise DocumentFormatError("文件扩展名与二进制原文格式不一致")
                else:
                    content, detected_eol, detected_bom = normalize_text_content(
                        file_info.get("content", "")
                    )
                    file_info["content"] = content
                    file_info["text_eol"] = _text_eol(
                        file_info.get("text_eol"), detected_eol
                    )
                    file_info["text_bom"] = bool(
                        file_info.get("text_bom", detected_bom)
                    )
                    if isinstance(file_info.get("translated_content"), str):
                        file_info["translated_content"] = normalize_text_content(
                            file_info["translated_content"]
                        )[0]
        except DocumentFormatError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        prune_jobs()
        job_id = uuid.uuid4().hex[:12]
        conflict = False
        with JOBS_LOCK:
            conflict = any(
                existing.get("status") == "running" for existing in JOBS.values()
            )
            if not conflict:
                JOBS[job_id] = {
                    "status": "running", "total": len(files), "done": 0,
                    "current": "", "results": [], "config": cfg, "files": files,
                    "current_file_done": 0, "current_file_total": 0,
                    "q": queue.Queue(maxsize=SSE_QUEUE_MAX_EVENTS), "interrupt": False,
                    "priority": list(body.get("priority", [])), "done_names": [],
                    "partials": {}, "completed_partials": {}, "created_at": time.time(),
                }
        if conflict:
            self._send_json({"error": "已有翻译任务正在运行，请先中断或等待完成"}, 409)
            return
        threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
        self._send_json({"job": job_id})


def test_server(server):
    parsed = urlsplit(str(server).rstrip("/"))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False, "服务地址必须是有效的 http:// 或 https:// 地址"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = cls(parsed.hostname, port, timeout=5)
    try:
        conn.request("GET", parsed.path.rstrip("/") + "/health")
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", "replace")
        if not 200 <= resp.status < 300:
            return False, f"HTTP {resp.status}: {data[:300]}"
        return True, data
    except Exception as e:
        return False, str(e)
    finally:
        conn.close()


def main():
    port = 9000
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    migrate_legacy_metadata()
    prune_output_metadata()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"翻译工作台: http://localhost:{port}")
    print(f"输出目录: {OUTPUTS_DIR}")
    print("按 Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

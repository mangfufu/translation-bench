#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地翻译工作台 · 后端

调用本地 OpenAI 兼容翻译模型(如 llama-server / HY-MT1.5), 提供网页拖拽翻译。

用法:
  python app.py [port]        # 默认 9000
  浏览器打开 http://localhost:9000
"""
import io
import hashlib
import http.client
import json
import os
import queue
import re
import sys
import time
import uuid
import threading
import zipfile
from urllib.parse import parse_qs, unquote, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
OUTPUTS_DIR = os.path.join(BASE_DIR, "outputs")

JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_TTL_SECONDS = 24 * 60 * 60
MAX_FINISHED_JOBS = 50

DEFAULT_CONFIG = {
    "server": "http://127.0.0.1:8081",
    "api_key": "",
    "model": "hy-mt1.5-1.8b",
    "context_size": 65536,        # 服务端槽位上下文(用于预算 max_tokens)
    "temperature": 0.7,
    "top_k": 20,
    "top_p": 0.6,
    "repeat_penalty": 1.05,
    "batch_paras": 15,            # 每批最多段落数
    "chunk_chars": 12000,         # 每批最多字符数
    "context_chars": 4000,        # 携带此前原文+译文作为术语/语气参考，0=关闭
    "future_context_chars": 2000, # 携带后续原文作为消歧参考，0=关闭
    "max_retries": 5,
    "connection_max_requests": 32,  # 定期轮换 keep-alive，避开服务端连接寿命上限
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
    "2. 只输出译文，不输出说明、标签或原文。\n"
    "3. 输入有多行时逐行对应输出，行数和顺序必须一致；单行译文内部不要换行。\n"
    "4. 结合参考上下文统一术语、专名、称谓和语气，但不得重复输出参考内容。\n"
    "5. 原样保留 URL、行内代码、变量、占位符、数字及 Markdown 行内标记。"
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
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


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

class Translator:
    def __init__(self, config):
        self.cfg = {**DEFAULT_CONFIG, **(config or {})}
        self._conn = None
        self._server = None
        self._requests_on_connection = 0

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
                self._requests_on_connection = 0

    def _post_json(self, payload):
        _, _, _, prefix = self._server_parts()
        path = prefix + "/v1/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Connection": "keep-alive",
        }
        api_key = str(self.cfg.get("api_key", "") or "").strip()
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        try:
            conn = self._connection()
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            if not 200 <= resp.status < 300:
                detail = data.decode("utf-8", "replace")[:500]
                raw_retry_after = resp.getheader("Retry-After")
                try:
                    retry_after = max(0.0, min(60.0, float(raw_retry_after)))
                except (TypeError, ValueError):
                    retry_after = None
                raise ModelServiceError(resp.status, detail, retry_after)
            result = json.loads(data.decode("utf-8"))
            self._requests_on_connection += 1
            max_requests = max(1, int(self.cfg.get("connection_max_requests", 32)))
            if resp.will_close or self._requests_on_connection >= max_requests:
                self.close()
            return result
        except Exception:
            self.close()
            raise

    def translate_text(self, text, context_before="", context_after=""):
        """翻译一段文本, 返回 (成功, 结果/错误)。"""
        cfg = self.cfg
        extra_prompt = str(cfg.get("system_prompt", "") or "").strip()
        style = cfg.get("translation_style", "自动判断")
        style_prompt = STYLE_PROMPTS.get(style, STYLE_PROMPTS["自动判断"])
        prompt_parts = [BASE_SYSTEM_PROMPT]
        if extra_prompt:
            prompt_parts.append(f"【用户补充要求】\n{extra_prompt}")
        if style_prompt:
            prompt_parts.append(f"【风格偏好（{style}）】\n{style_prompt}")
        sys_prompt = "\n\n".join(prompt_parts)
        est_out = len(text) // 2 + 40
        est_in = (
            len(text) + len(context_before) + len(context_after) + len(sys_prompt)
        ) // 2 + 160
        ctx = max(2048, int(cfg.get("context_size", 65536)))
        # 输出约为输入量级; 限制 max_tokens 防止模型跑偏时无限生成导致卡死
        available = ctx - est_in - 96
        if available < 256:
            return False, f"输入超出上下文预算（估算输入 {est_in} tokens，上下文 {ctx}）"
        max_tokens = min(int(est_out * 1.6) + 200, available, 80000)
        src = cfg.get("src_lang", "自动判断")
        tgt = cfg.get("tgt_lang", "中文")
        if src == "自动判断":
            src = detect_lang(text)
        if context_before or context_after:
            references = []
            if context_before:
                references.append(
                    "【前文参考（原文及既有译文）】\n" + context_before
                )
            if context_after:
                references.append(
                    "【下文参考（尚未翻译的原文）】\n" + context_after
                )
            prompt = (
                "以下参考内容只用于理解语境、消除歧义并统一术语、称谓和语气。\n"
                "只翻译【当前待译文本】，不得翻译、续写或输出任何前文和下文参考。\n\n"
                + "\n\n".join(references)
                + f"\n\n【当前待译文本（{src} → {tgt}）】\n{text}"
            )
        else:
            prompt = f"将以下{src}文本翻译为{tgt}，注意只需要输出翻译后的结果，不要额外解释：\n\n{text}"
        payload = {
            "model": cfg["model"],
            "messages": ([{"role": "system", "content": sys_prompt}] if sys_prompt else [])
                       + [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": cfg.get("temperature", 0.7),
            "top_k": cfg.get("top_k", 20),
            "top_p": cfg.get("top_p", 0.6),
            "repeat_penalty": cfg.get("repeat_penalty", 1.05),
        }
        last = None
        retryable = True
        attempts = max(1, int(cfg.get("max_retries", 5)))
        for attempt in range(attempts):
            try:
                r = self._post_json(payload)
                out = r["choices"][0]["message"]["content"]
                if r["choices"][0].get("finish_reason") == "length":
                    raise RuntimeError("译文达到 max_tokens，输出被截断")
                return True, out
            except Exception as e:
                last = e
                retryable = not isinstance(e, ModelServiceError) or e.transient
                if not retryable or attempt + 1 >= attempts:
                    break
                retry_after = getattr(e, "retry_after", None)
                delay = retry_after if retry_after is not None else min(20.0, 1.5 * (2 ** attempt))
                self.close()
                time.sleep(delay)
        suffix = f"（已尝试 {attempts} 次）" if attempts > 1 and retryable else ""
        return False, str(last) + suffix

    @staticmethod
    def _one_line(text):
        """模型偶尔自行断行；压回一行以保持源文的行结构。"""
        return re.sub(r"\s*\r?\n+\s*", " ", str(text).strip())

    def _context_text(self, history):
        """从已完成的 (原文, 译文) 中截取最近的滑动参考窗口。"""
        limit = max(0, int(self.cfg.get("context_chars", 4000)))
        if not history or limit == 0:
            return ""
        blocks = []
        used = 0
        for source, translated in reversed(history):
            block = f"[原文]\n{source}\n[译文]\n{translated}"
            extra = len(block) + (2 if blocks else 0)
            if blocks and used + extra > limit:
                break
            if not blocks and len(block) > limit:
                block = block[-limit:]
                extra = len(block)
            blocks.append(block)
            used += extra
        return "\n\n".join(reversed(blocks))

    def _future_context_text(self, sources):
        """从当前批次之后的原文开头截取下文参考窗口。"""
        limit = max(0, int(self.cfg.get("future_context_chars", 2000)))
        if not sources or limit == 0:
            return ""
        blocks = []
        used = 0
        for source in sources:
            source = str(source).strip()
            if not source:
                continue
            separator = 2 if blocks else 0
            remaining = limit - used - separator
            if remaining <= 0:
                break
            if len(source) > remaining:
                source = source[:remaining]
            blocks.append(source)
            used += separator + len(source)
            if used >= limit:
                break
        return "\n\n".join(blocks)

    def translate_batch(self, paras, history=None, future=None):
        """批翻译行；数量不匹配时逐行回退，保证一对一。"""
        history = list(history or [])
        future = list(future or [])
        text = "\n\n".join(paras)
        ok, out = self.translate_text(
            text,
            self._context_text(history),
            self._future_context_text(future),
        )
        if ok:
            lines = [self._one_line(line) for line in out.splitlines() if line.strip()]
            if len(lines) == len(paras):
                return True, lines
        res = []
        for pos, p in enumerate(paras):
            line_future = paras[pos + 1:] + future
            ok2, o2 = self.translate_text(
                p,
                self._context_text(history),
                self._future_context_text(line_future),
            )
            if not ok2:
                return False, o2
            translated = self._one_line(o2)
            res.append(translated)
            history.append((p, translated))
        return True, res

    def _batches(self, items):
        bp = max(1, int(self.cfg.get("batch_paras", 15)))
        cc = max(256, int(self.cfg.get("chunk_chars", 12000)))
        batches, cur, chars = [], [], 0
        for item in items:
            text = item[1]
            if cur and (len(cur) >= bp or chars + len(text) + 2 > cc):
                batches.append(cur)
                cur, chars = [], 0
            cur.append(item)
            chars += len(text) + 2
        if cur:
            batches.append(cur)
        return batches

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

    def translate_content(self, content, filename="", emit=None, progress=None):
        """翻译一段文件内容, 返回 (成功, 翻译后内容/错误)。
        保留空行、标题层级、列表/引用前缀与代码块。emit(行号, 译文) 推送定位结果。"""
        lines = content.split("\n")
        out = list(lines)
        units = []
        in_fence = False
        for idx, line in enumerate(lines):
            unit, in_fence = self._markdown_unit(line, in_fence)
            units.append(unit)
            if not unit["translate"] and line.strip() and emit:
                emit(idx, line)

        translatable = [(idx, unit["text"]) for idx, unit in enumerate(units) if unit["translate"]]
        completed_units = 0
        if progress:
            progress(0, len(translatable))
        history = []
        batches = self._batches(translatable)
        for batch_pos, batch in enumerate(batches):
            sources = [text for _, text in batch]
            future_sources = [
                text
                for future_batch in batches[batch_pos + 1:]
                for _, text in future_batch
            ]
            ok, trans = self.translate_batch(sources, history, future_sources)
            if not ok:
                return False, trans
            for (idx, source), translated in zip(batch, trans):
                unit = units[idx]
                out[idx] = unit["prefix"] + translated + unit["suffix"]
                history.append((source, translated))
                if emit:
                    emit(idx, out[idx])
                completed_units += 1
                if progress:
                    progress(completed_units, len(translatable))
        return True, "\n".join(out)


def run_job(job_id):
    """后台翻译任务, 支持中断(interrupt)与优先级插队(priority 文件名列表)。
    当前正在翻译的文件不被打断; 下个文件优先从 priority 里选。"""
    job = JOBS[job_id]
    cfg = job["config"]
    translator = Translator(cfg)
    files = job["files"]
    job["total"] = len(files)
    job["done_names"] = []

    def emit(kind, **kw):
        job["q"].put({"kind": kind, **kw})

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

            try:
                ok, out = translator.translate_content(
                    content, name, emit=emit_line, progress=emit_file_progress
                )
                if not ok:
                    job["results"].append({"name": name, "status": "error", "error": out})
                    emit("fail", file=name, msg=out)
                else:
                    target = cfg.get("tgt_lang", "中文")
                    out_name = output_filename(name, target)
                    os.makedirs(OUTPUTS_DIR, exist_ok=True)
                    with open(os.path.join(OUTPUTS_DIR, out_name), "w", encoding="utf-8") as fh:
                        fh.write(out)
                    metadata = {
                        "source_name": name,
                        "source_sha256": source_digest(content),
                        "target_language": target,
                        "output_name": out_name,
                    }
                    with open(os.path.join(OUTPUTS_DIR, out_name + ".meta.json"), "w", encoding="utf-8") as fh:
                        json.dump(metadata, fh, ensure_ascii=False, indent=2)
                    job["results"].append({
                        "name": name, "output_name": out_name,
                        "target_language": target, "status": "ok", "content": out,
                    })
                    job["done_names"].append(name)
                    emit(
                        "file_done", file=name, idx=idx, output_name=out_name,
                        done=job.get("current_file_total", 0),
                        total=job.get("current_file_total", 0),
                    )
            except Exception as e:
                job["results"].append({"name": name, "status": "error", "error": str(e)})
                emit("fail", file=name, msg=str(e))
            with JOBS_LOCK:
                job["partials"].pop(name, None)
            done += 1
            job["done"] = done
            emit("overall_progress", done=done, total=len(files))
    finally:
        translator.close()

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
        remove.update(jid for jid, _ in survivors[MAX_FINISHED_JOBS:])
        for jid in remove:
            JOBS.pop(jid, None)


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

    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        ln = int(self.headers.get("Content-Length", 0))
        if ln <= 0:
            return {}
        if ln > 100 * 1024 * 1024:
            raise ValueError("请求体超过 100MB 限制")
        return json.loads(self.rfile.read(ln).decode("utf-8"))

    def _request_parts(self):
        parsed = urlsplit(self.path)
        return unquote(parsed.path), parse_qs(parsed.query)

    def do_GET(self):
        path, query = self._request_parts()
        if path == "/" or path == "/index.html":
            self._serve_file(os.path.join(STATIC_DIR, "index.html"))
        elif path == "/api/status":
            job = query.get("job", [""])[0]
            with JOBS_LOCK:
                j = JOBS.get(job)
                if not j:
                    self._send_json({"status": "notfound"})
                    return
                base = {k: j.get(k) for k in
                        ("status", "total", "done", "current", "error",
                         "current_file_done", "current_file_total")}
                full = query.get("full", [""])[0] == "1" or j.get("status") != "running"
                if full:
                    base["results"] = list(j.get("results", []))
                else:
                    base["results"] = [
                        {k: result.get(k) for k in
                         ("name", "status", "error", "output_name", "target_language")}
                        for result in j.get("results", [])
                    ]
                base["names"] = [f["name"] for f in j.get("files", [])]
                base["done_names"] = j.get("done_names", [])
                base["partials"] = {
                    name: dict(lines) for name, lines in j.get("partials", {}).items()
                }
                self._send_json(base)
        elif path == "/api/running":
            with JOBS_LOCK:
                running = [jid for jid, j in JOBS.items() if j.get("status") == "running"]
            self._send_json({"job": running[0] if running else None})
        elif path == "/api/history":
            files_list = []
            if os.path.isdir(OUTPUTS_DIR):
                for fn in sorted(os.listdir(OUTPUTS_DIR)):
                    if fn.lower().endswith((".md", ".markdown", ".txt", ".text")):
                        p = os.path.join(OUTPUTS_DIR, fn)
                        entry = {"name": fn, "size": os.path.getsize(p)}
                        meta_path = p + ".meta.json"
                        if os.path.isfile(meta_path):
                            try:
                                with open(meta_path, "r", encoding="utf-8") as fh:
                                    meta = json.load(fh)
                                entry.update({
                                    "source_name": meta.get("source_name"),
                                    "source_sha256": meta.get("source_sha256"),
                                    "target_language": meta.get("target_language"),
                                })
                            except (OSError, ValueError):
                                pass
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
            with open(output_path, "r", encoding="utf-8") as fh:
                self._send_json({"name": name, "content": fh.read()})
        elif path == "/api/test":
            server = query.get("server", [""])[0]
            ok, msg = test_server(server)
            self._send_json({"ok": ok, "msg": msg})
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
                        z.writestr(
                            r.get("output_name") or output_filename(
                                r["name"], j.get("config", {}).get("tgt_lang", "中文")
                            ),
                            r["content"],
                        )
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
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/interrupt":
            try:
                body = self._read_body()
            except Exception:
                body = {}
            jid = body.get("job") or self.path.split("job=")[-1].split("&")[0]
            with JOBS_LOCK:
                j = JOBS.get(jid)
                if j:
                    j["interrupt"] = True
            self._send_json({"ok": bool(j)})
            return
        if path == "/api/prioritize":
            try:
                body = self._read_body()
            except Exception:
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
        prune_jobs()
        job_id = uuid.uuid4().hex[:12]
        with JOBS_LOCK:
            JOBS[job_id] = {
                "status": "running", "total": len(files), "done": 0,
                "current": "", "results": [], "config": cfg, "files": files,
                "current_file_done": 0, "current_file_total": 0,
                "q": queue.Queue(), "interrupt": False,
                "priority": list(body.get("priority", [])), "done_names": [],
                "partials": {}, "created_at": time.time(),
            }
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

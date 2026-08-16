#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parse.py — 聊天记录解析 → 标准消息流 JSON（§5.1）

用法：
  python3 parse.py <聊天记录文件|照片目录> [更多文件...] [--out messages.json] [--platform X] [--map '{"名字":"A"}']

支持的格式（尽力兼容，能解析多少算多少，解析不了的保留原文不丢）：
  - 微信导出 txt/html（行格式 `[YYYY-MM-DD HH:MM] 发送者: 内容`）
  - Telegram 导出 json/html、QQ/短信导出、iMessage 导出、抖音私信导出
  - Google Takeout / Twitter 归档（tweet.js / 通用时间戳文本）
  - 纯文本/无时间戳格式（尽力推测，推测不出标记"时间未知"）
  - 照片文件夹（EXIF 时间线——"这张照片=那个夏天"）
  - CSV 短信导出

依赖策略：标准库为主（html.parser / urllib / struct）；若环境允许：
  - beautifulsoup4 → HTML 解析更稳（不可用回退 html.parser+正则）
  - Pillow → EXIF 增强（不可用回退 struct 手写 EXIF/TIFF/PNG 解析）
解析失败时明确提示（如"该文件格式异常，请提供 txt 导出"），不静默出错。

输出：
  messages.json   — 标准消息流（§5.1）
  parse_meta.json — 解析元信息（格式/平台/发送者映射/警告）
"""
import argparse
import base64
import csv
import io
import json
import os
import re
import struct
import sys

# ---------- 可选依赖探测（§0.5 依赖策略） ----------
try:
    from PIL import Image  # type: ignore
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

try:
    from bs4 import BeautifulSoup  # type: ignore
    HAVE_BS4 = True
except Exception:
    HAVE_BS4 = False

# ---------- 常量 ----------
PLATFORM_UNKNOWN = "unknown"
MSG_TYPES = ("text", "emoji", "image", "system", "voice", "video", "file")

TS_LINE_RE = re.compile(
    r"^\s*[\[(]?"
    r"(\d{4})[年/\-.](\d{1,2})[月/\-.](\d{1,2})[日]?\s+"
    r"(\d{1,2}):(\d{2})(?::(\d{2}))?"
    r"[)\]]?\s*"
    r"([^:：]{1,24})[:：]\s?(.*)$"
)
TS_LINE_NO_SENDER_RE = re.compile(
    r"^\s*[\[(]?(\d{4})[年/\-.](\d{1,2})[月/\-.](\d{1,2})[日]?\s+"
    r"(\d{1,2}):(\d{2})(?::(\d{2}))?[)\]]?\s*(.*)$"
)
NAME_LINE_RE = re.compile(r"^([^:：\s]{1,24})[:：]\s?(.*)$")

# 系统消息关键词（撤回/群系统等）
SYSTEM_KEYWORDS = ("撤回", "加入了群聊", "邀请", "拍了拍", "移出", "退出了群聊",
                   "设置群聊名称", "开启了朋友验证", "你已添加", "开始对话吧",
                   "消息已发出", "被对方拒收")
# 多媒体占位
MEDIA_PATTERNS = [
    (r"\[(图片|照片|表情|语音|视频|文件|动画表情|红包|转账|位置|名片|链接|音乐)\]", None),
    (r"&lt;?\[?(图片|表情)\]?&gt;?", None),
]
MEDIA_TYPE_MAP = {
    "图片": "image", "照片": "image", "表情": "emoji", "动画表情": "emoji",
    "语音": "voice", "视频": "video", "文件": "file", "红包": "system",
    "转账": "system", "位置": "file", "名片": "file", "链接": "file", "音乐": "file",
}


# ---------- 消息模型 ----------
def make_msg(ts, sender, text, mtype="text", platform=PLATFORM_UNKNOWN):
    """ts: ISO 字符串或 None（时间未知）。"""
    return {"ts": ts, "sender": sender, "text": text, "type": mtype,
            "platform": platform}


def detect_type(text):
    t = (text or "").strip()
    low = t.lower()
    if t.startswith("["):
        for key, _ in MEDIA_PATTERNS:
            m = re.match(r"\[([^\]]+)\]", t)
            if m:
                return MEDIA_TYPE_MAP.get(m.group(1), "text")
    for kw in SYSTEM_KEYWORDS:
        if kw in low or kw in t:
            return "system"
    return "text"


# ---------- 时间解析 ----------
def fmt_ts(y, mo, d, h, mi, s=0):
    try:
        return "%04d-%02d-%02dT%02d:%02d:%02d" % (int(y), int(mo), int(d),
                                                   int(h), int(mi), int(s or 0))
    except (ValueError, TypeError):
        return None


def parse_iso(ts_str):
    """宽松 ISO 时间解析，失败返回 None。"""
    s = (ts_str or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})[T\s](\d{1,2}):(\d{2})(?::(\d{2}))?",
                 s)
    if m:
        return fmt_ts(*m.groups())
    return None


# ---------- 编码探测 ----------
def read_text(path):
    """UTF-8 优先，兼容 GBK（Windows 导出常见）；降级 latin-1 不丢字节。"""
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
        return raw.decode("utf-8", errors="replace")
    for enc in ("utf-8", "gbk", "gb18030", "utf-16"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", errors="replace")


# ---------- TXT 行格式解析（微信/QQ/短信/iMessage/抖音共用） ----------
def parse_txt_lines(text, platform=PLATFORM_UNKNOWN):
    """
    核心规则（§8 工程坑 3）：一条消息可能跨多行；以"时间戳开头"为新消息的判定标准。
    行格式：[YYYY-MM-DD HH:MM] 发送者: 内容（括号可选，/年 分隔符兼容）。
    """
    msgs = []
    cur = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            continue
        m = TS_LINE_RE.match(line)
        if m:
            y, mo, d, h, mi, s, sender, content = m.groups()
            ts = fmt_ts(y, mo, d, h, mi, s)
            if cur is not None:
                msgs.append(cur)
            cur = make_msg(ts, sender.strip(), content, detect_type(content),
                           platform)
            continue
        m2 = TS_LINE_NO_SENDER_RE.match(line)
        if m2:
            y, mo, d, h, mi, s, content = m2.groups()
            ts = fmt_ts(y, mo, d, h, mi, s)
            if cur is not None:
                msgs.append(cur)
            cur = make_msg(ts, None, content, detect_type(content), platform)
            continue
        # 无时间戳：若是"名字: 内容"且当前消息为空，则作为新消息（尽力推测）
        m3 = NAME_LINE_RE.match(line)
        if cur is not None and cur["ts"] is None and m3:
            msgs.append(cur)
            cur = make_msg(None, m3.group(1).strip(), m3.group(2),
                           detect_type(m3.group(2)), platform)
            continue
        # 续行：接到当前消息尾部（保留原文不丢）
        if cur is not None:
            cur["text"] = cur["text"] + "\n" + line
        else:
            # 文件开头无时间戳：作为"时间未知"消息（尽力推测发送者）
            m4 = NAME_LINE_RE.match(line)
            if m4:
                cur = make_msg(None, m4.group(1).strip(), m4.group(2),
                               detect_type(m4.group(2)), platform)
            else:
                cur = make_msg(None, None, line, detect_type(line), platform)
    if cur is not None:
        msgs.append(cur)
    return msgs


# ---------- HTML 解析（微信导出 html 等） ----------
try:
    from html.parser import HTMLParser

    class _VisibleText(HTMLParser):
        def __init__(self):
            HTMLParser.__init__(self)
            self.parts = []
            self.skip = 0

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.skip += 1
            if tag in ("p", "div", "br", "li"):
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag in ("script", "style") and self.skip:
                self.skip -= 1
            if tag in ("p", "div", "li"):
                self.parts.append("\n")

        def handle_data(self, data):
            if not self.skip:
                self.parts.append(data)

    def html_to_text(html_text):
        p = _VisibleText()
        try:
            p.feed(html_text)
        except Exception:
            pass
        return "".join(p.parts)
except Exception:  # pragma: no cover
    def html_to_text(html_text):  # 最坏回退：去标签
        return re.sub(r"<[^>]+>", " ", html_text)


def parse_html(html_text, platform):
    """HTML 导出 → 消息流。优先 BeautifulSoup（若可用），回退 html.parser。"""
    if HAVE_BS4:
        soup = BeautifulSoup(html_text, "html.parser")
        found = []
        for node in soup.find_all(["div", "p", "li"], class_=re.compile(
                r"(?i)msg|message|chat|item|row|content|bubble")):
            txt = node.get_text(" ", strip=True)
            if not txt:
                continue
            # 分别提取 name / time / content 子元素（微信导出常见结构）
            name_el = node.find(class_=re.compile(r"(?i)name|nick|user"))
            time_el = node.find(class_=re.compile(r"(?i)time|date|timestamp"))
            content_el = node.find(class_=re.compile(
                r"(?i)content|text|msg|bubble|body")) or node
            name = name_el.get_text(" ", strip=True) if name_el else None
            time_txt = time_el.get_text(" ", strip=True) if time_el else None
            content = content_el.get_text(" ", strip=True)
            if time_el and content == (time_txt or "") and name_el:
                # content 元素没找对：退化为整个节点文本
                content = txt
            if content == (time_txt or "") and name_el:
                content = txt
            dt = node.get("datetime") or node.get("data-time") or time_txt
            found.append((dt, name, content))
        if len(found) >= 2:
            return _from_bs4_nodes(found, platform)
    # 回退：转可见文本后按 TXT 行格式解析（大多数 html 导出转文本后是时间行）
    text = html_to_text(html_text)
    msgs = parse_txt_lines(text, platform)
    if len(msgs) >= 2:
        return msgs
    return []


def _from_bs4_nodes(nodes, platform):
    msgs = []
    for dt, name, content in nodes:
        if not content:
            continue
        ts = None
        if dt:
            ts = parse_iso(dt)
        if not ts and dt and re.match(r"^\d{1,2}:\d{2}", str(dt)):
            ts = None  # 只有时分，无日期 → 时间未知（不硬猜）
        # 内容若还带着 name/time 前缀则剥离（未找到独立内容元素时）
        for prefix in (name,):
            if prefix and content.startswith(prefix):
                content = content[len(prefix):].strip()
        content = re.sub(r"^\d{1,2}:\d{2}\s*", "", content).strip()
        if not content:
            continue
        msgs.append(make_msg(ts, name, content, detect_type(content), platform))
    return msgs


# ---------- JSON 导出解析（Telegram / Twitter / Takeout / 通用） ----------
def parse_telegram_json(obj, platform="telegram"):
    msgs = []
    for m in obj.get("messages", []):
        ts = parse_iso(m.get("date") or m.get("date_unixtime") or "")
        if not ts and m.get("date_unixtime"):
            try:
                import datetime
                ts = datetime.datetime.utcfromtimestamp(
                    int(m["date_unixtime"])).strftime("%Y-%m-%dT%H:%M:%S")
            except Exception:
                ts = None
        sender = m.get("from") or m.get("from_id") or m.get("actor")
        if sender and str(sender).startswith("user"):
            sender = m.get("from") or sender
        text = m.get("text")
        if isinstance(text, list):
            buf = []
            for part in text:
                if isinstance(part, str):
                    buf.append(part)
                elif isinstance(part, dict):
                    buf.append(part.get("text", ""))
            text = "".join(buf)
        elif text is None:
            text = ""
        text = str(text)
        mtype = "text"
        media = m.get("media_type") or m.get("photo")
        if media:
            mtype = "image" if str(media) in ("photo", "sticker") else "file"
            if not text:
                text = "[%s]" % str(media)
        msgs.append(make_msg(ts, sender, text, mtype, platform))
    return msgs


def parse_twitter_js(text):
    """tweet.js / account.js 归档：`window.YTD.tweet.part0 = [ {...} ]`"""
    msgs = []
    m = re.search(r"=\s*(\[.*\])\s*;?\s*$", text, re.S)
    if not m:
        return []
    try:
        tweets = json.loads(m.group(1))
    except ValueError:
        # 容错：去掉尾逗号后重试
        cleaned = re.sub(r",\s*([}\]])", r"\1", m.group(1))
        try:
            tweets = json.loads(cleaned)
        except ValueError:
            return []
    for t in tweets:
        created = t.get("tweet", t).get("created_at", "")
        ts = None
        try:
            import datetime
            dt = datetime.datetime.strptime(created,
                                            "%a %b %d %H:%M:%S %z %Y")
            ts = dt.astimezone(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S")
        except Exception:
            ts = parse_iso(created)
        text = t.get("tweet", t).get("full_text") or t.get("tweet", t).get(
            "text") or ""
        sender = t.get("tweet", t).get("user", {}).get("screen_name")
        msgs.append(make_msg(ts, sender, text, detect_type(text),
                             "twitter"))
    return msgs


def parse_generic_json(text, platform=PLATFORM_UNKNOWN):
    """通用 JSON 归档：找 messages/items 列表，取 date/time/created_at + from/sender/author + text/body。"""
    try:
        obj = json.loads(text)
    except ValueError:
        return []
    msgs = []
    candidates = None
    for key in ("messages", "items", "chats", "data"):
        if isinstance(obj, dict) and isinstance(obj.get(key), list):
            candidates = obj[key]
            break
    if candidates is None:
        candidates = obj if isinstance(obj, list) else None
    if not candidates:
        return []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        ts = None
        for k in ("ts", "timestamp", "created_at", "date", "time", "sent_at"):
            if item.get(k):
                ts = parse_iso(str(item[k]))
                if ts:
                    break
        sender = None
        for k in ("from", "sender", "author", "user", "nickname", "name"):
            v = item.get(k)
            if isinstance(v, dict):
                v = v.get("name") or v.get("username") or v.get("id")
            if v:
                sender = str(v)
                break
        text = None
        for k in ("text", "body", "content", "message", "text_content"):
            v = item.get(k)
            if isinstance(v, dict):
                v = v.get("text") or ""
            if v:
                text = str(v)
                break
        if text is None and sender is None and ts is None:
            continue
        msgs.append(make_msg(ts, sender, text or "", detect_type(text or ""),
                             platform))
    return msgs


# ---------- CSV 短信解析 ----------
def parse_sms_csv(text, platform="sms"):
    msgs = []
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except Exception:
        return []
    if not rows:
        return []
    header = [h.lower().strip() for h in rows[0]]
    idx_date = next((i for i, h in enumerate(header)
                     if "date" in h or "time" in h), None)
    idx_from = next((i for i, h in enumerate(header)
                     if h in ("from", "sender", "address", "phone", "name")), None)
    idx_body = next((i for i, h in enumerate(header)
                     if h in ("body", "text", "content", "message")), None)
    if idx_date is None and idx_body is None:
        return []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        date = row[idx_date] if idx_date is not None and idx_date < len(row) else None
        body = row[idx_body] if idx_body is not None and idx_body < len(row) else ""
        sender = row[idx_from] if idx_from is not None and idx_from < len(row) else None
        ts = parse_iso(date) if date else None
        msgs.append(make_msg(ts, sender, body, detect_type(body), platform))
    return msgs


# ---------- 照片/EXIF（§4.49 多模态记忆节点） ----------
def exif_datetime_jpeg(data):
    """手写 JPEG APP1 EXIF 解析（struct，零依赖）；失败返回 None。"""
    try:
        i = 2
        while i < len(data) - 4:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01, 0xD9):
                i += 2
                continue
            seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker == 0xE1 and data[i + 4:i + 8] == b"Exif\x00\x00":
                return exif_datetime_tiff(data[i + 8:i + 4 + seg_len])
            i += 2 + seg_len
    except Exception:
        return None
    return None


def exif_datetime_tiff(data):
    """TIFF/EXIF 数据块解析：读 IFD0 的 DateTime(0x0132)/DateTimeOriginal(0x9003)。"""
    try:
        if data[:2] == b"II":
            endian = "<"
        elif data[:2] == b"MM":
            endian = ">"
        else:
            return None
        ifd_offset = struct.unpack(endian + "I", data[4:8])[0]
        n = struct.unpack(endian + "H", data[ifd_offset:ifd_offset + 2])[0]
        for k in range(n):
            off = ifd_offset + 2 + k * 12
            tag = struct.unpack(endian + "H", data[off:off + 2])[0]
            typ = struct.unpack(endian + "H", data[off + 2:off + 4])[0]
            count = struct.unpack(endian + "I", data[off + 4:off + 8])[0]
            if tag in (0x0132, 0x9003, 0x9004) and typ == 2:
                if count <= 4:
                    raw = data[off + 8:off + 8 + count]
                else:
                    poff = struct.unpack(endian + "I", data[off + 8:off + 12])[0]
                    raw = data[poff:poff + count]
                s = raw.rstrip(b"\x00").decode("ascii", errors="replace").strip()
                m = re.match(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", s)
                if m:
                    return fmt_ts(*m.groups())
                return parse_iso(s)
    except Exception:
        return None
    return None


def png_date(data):
    """PNG tEXt/iTXt 块中的日期（Creation Time 等），尽力解析。"""
    try:
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        i = 8
        while i < len(data) - 8:
            length = struct.unpack(">I", data[i:i + 4])[0]
            kind = data[i + 4:i + 8]
            chunk = data[i + 8:i + 8 + length]
            if kind == b"IEND":
                break
            if kind in (b"tEXt", b"iTXt"):
                txt = chunk.decode("latin-1", errors="replace")
                for key in ("Creation Time", "date", "Date"):
                    if txt.lower().startswith(key.lower()):
                        val = txt[len(key):].lstrip("\x00 ").strip()
                        ts = parse_iso(val)
                        if ts:
                            return ts
            i += 12 + length
    except Exception:
        return None
    return None


def photo_timestamp(path):
    """照片时间：Pillow（若可用）→ 手写 JPEG/TIFF/PNG 解析（标准库回退）。"""
    ts = None
    if HAVE_PIL:
        try:
            img = Image.open(path)
            exif = img.getexif()
            for tag in (36867, 36868, 306):  # DateTimeOriginal/Digitized/DateTime
                v = exif.get(tag)
                if v:
                    m = re.match(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", str(v))
                    if m:
                        ts = fmt_ts(*m.groups())
                        break
        except Exception:
            ts = None
    if ts is None:
        try:
            with open(path, "rb") as f:
                head = f.read(64 * 1024)
            if head[:2] == b"\xff\xd8":
                ts = exif_datetime_jpeg(head)
            elif head[:4] in (b"II*\x00", b"MM\x00*"):
                ts = exif_datetime_tiff(head)
            elif head[:8] == b"\x89PNG\r\n\x1a\n":
                with open(path, "rb") as f:
                    ts = png_date(f.read(1024 * 1024))
        except OSError:
            pass
    return ts


def parse_photo_folder(path, platform="photos"):
    msgs = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if not os.path.isfile(full):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in (".jpg", ".jpeg", ".tif", ".tiff", ".png", ".heic", ".webp"):
            continue
        ts = photo_timestamp(full)
        text = "照片: %s" % name
        msgs.append(make_msg(ts, None, text, "image", platform))
    return msgs


# ---------- 格式探测 ----------
def sniff_format(path, text, is_dir):
    if is_dir:
        return "photos"
    ext = os.path.splitext(path)[1].lower()
    head = (text or "")[:2000].lstrip()
    if ext == ".json" or text.lstrip().startswith("{"):
        low = head[:500].lower()
        if '"messages"' in low and ('"date"' in low or '"from"' in low):
            return "telegram_json"
        if "created_at" in low and ("full_text" in low or '"text"' in low):
            return "twitter_js"
        return "generic_json"
    if ext == ".csv":
        return "sms_csv"
    if ext in (".html", ".htm"):
        return "html"
    if ext == ".js":
        return "twitter_js"
    # 文本：先数"时间戳行"占比
    lines = [l for l in text.splitlines() if l.strip()]
    ts_lines = sum(1 for l in lines if TS_LINE_RE.match(l))
    if ts_lines >= max(2, len(lines) // 10):
        return "txt_timeline"
    return "plain_txt"


# ---------- 发送者归一化（§8 工程坑 1：锚点校准） ----------
def normalize_senders(msgs, forced_map=None):
    """
    归一化发送者为 A/B/C...（按消息数从多到少）。
    - forced_map: 用户提供锚点（{"名字": "A"}），优先应用（多段记录校准用）。
    - 未命中名字按频率分配；≤1 个真实发送者时保持原名（不硬造）。
    返回 (新消息列表, 映射表)。
    """
    from collections import Counter
    counts = Counter(m["sender"] for m in msgs if m["sender"])
    mapping = {}
    used = set()
    if forced_map:
        for name, letter in forced_map.items():
            mapping[name] = letter
            used.add(letter)
    next_letter = "A"
    while next_letter in used:
        next_letter = chr(ord(next_letter) + 1)
    for name, _ in counts.most_common():
        if name in mapping:
            continue
        if len(used) == 0:
            mapping[name] = "A"
            used.add("A")
        elif len(used) == 1:
            mapping[name] = "B"
            used.add("B")
        else:
            mapping[name] = next_letter
            next_letter = chr(ord(next_letter) + 1)
            used.add(mapping[name])
    out = []
    for m in msgs:
        nm = dict(m)
        if nm["sender"]:
            nm["sender"] = mapping.get(nm["sender"], nm["sender"])
        out.append(nm)
    return out, mapping


# ---------- 主流程 ----------
def _effective_platform(fmt, hint):
    """格式自带平台优先；--platform 提示只作用于无法自判的格式。"""
    if fmt == "telegram_json":
        return "telegram"
    if fmt == "twitter_js":
        return "twitter"
    if fmt == "sms_csv":
        return "sms"
    if fmt == "photos":
        return "photos"
    if fmt in ("txt_timeline", "html"):
        return hint or "wechat"
    return hint or PLATFORM_UNKNOWN


def parse_files(inputs, platform=None, forced_map=None, out_dir="."):
    all_msgs = []
    warnings = []
    formats = []
    for inp in inputs:
        if os.path.isdir(inp):
            msgs = parse_photo_folder(inp)
            if not msgs:
                warnings.append("%s: 未找到可解析的照片（支持 jpg/tif/png/heic/webp）" % inp)
            else:
                n_ts = sum(1 for m in msgs if m["ts"])
                warnings.append("%s: 照片 %d 张，其中 %d 张含时间戳（EXIF）"
                                % (inp, len(msgs), n_ts))
            formats.append("photos")
            all_msgs.extend(msgs)
        else:
            try:
                text = read_text(inp)
            except OSError as e:
                sys.stderr.write("无法读取文件 %s: %s\n" % (inp, e))
                return 1
            fmt = sniff_format(inp, text, False)
            formats.append(fmt)
            pfx = _effective_platform(fmt, platform)
            if fmt == "txt_timeline":
                msgs = parse_txt_lines(text, pfx)
            elif fmt == "html":
                msgs = parse_html(text, pfx)
            elif fmt == "telegram_json":
                import json as _json
                try:
                    msgs = parse_telegram_json(_json.loads(text), pfx)
                except ValueError:
                    msgs = []
            elif fmt == "twitter_js":
                msgs = parse_twitter_js(text)
            elif fmt == "sms_csv":
                msgs = parse_sms_csv(text, pfx)
            elif fmt == "generic_json":
                msgs = parse_generic_json(text, pfx)
            else:  # plain_txt
                msgs = parse_txt_lines(text, pfx)
                if len([m for m in msgs if m["ts"]]) < 2:
                    msgs = parse_plain_fallback(text, pfx)
            if not msgs:
                warnings.append("%s: 格式(%s)未解析出消息——该文件格式异常，请提供 txt 导出"
                                % (inp, fmt))
            else:
                warnings.append("%s: 格式(%s) 解析出 %d 条消息"
                                % (inp, fmt, len(msgs)))
            all_msgs.extend(msgs)

    if not all_msgs:
        sys.stderr.write("错误：未能从输入解析出任何消息。\n"
                         "  该文件格式异常，请提供 txt 导出（微信/QQ/Telegram 均可），"
                         "或纯文本聊天记录。\n")
        return 2

    all_msgs, mapping = normalize_senders(all_msgs, forced_map)

    # 多文件合并：按时间排序（时间未知的保持文件内相对顺序）
    keyed = []
    for i, m in enumerate(all_msgs):
        keyed.append((m["ts"] or "", i, m))
    keyed.sort(key=lambda x: (x[0] == "", x[0], x[1]))
    all_msgs = [k[2] for k in keyed]

    messages_path = os.path.join(out_dir, "messages.json")
    with open(messages_path, "w", encoding="utf-8") as f:
        json.dump(all_msgs, f, ensure_ascii=False, indent=2)

    meta = {
        "format": formats,
        "platform": platform or PLATFORM_UNKNOWN,
        "sender_map": mapping,
        "count": len(all_msgs),
        "warnings": warnings,
        "pipeline": "parse.py",
    }
    meta_path = os.path.join(out_dir, "parse_meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    n_ts = sum(1 for m in all_msgs if m["ts"])
    print("解析完成：共 %d 条消息（%d 条含时间戳），发送者 %d 人"
          % (len(all_msgs), n_ts, len(mapping)))
    print("发送者映射: %s" % json.dumps(mapping, ensure_ascii=False))
    print("输出: %s" % messages_path)
    for w in warnings:
        print("  - %s" % w)
    return 0


def parse_plain_fallback(text, platform):
    """纯文本（无时间戳）：按「名字: 内容」行切分；无法切分的整段保留为一条。"""
    msgs = []
    cur = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            continue
        m = NAME_LINE_RE.match(line)
        if m:
            if cur is not None:
                msgs.append(cur)
            cur = make_msg(None, m.group(1).strip(), m.group(2),
                           detect_type(m.group(2)), platform)
        else:
            if cur is not None:
                cur["text"] = cur["text"] + "\n" + line
            else:
                cur = make_msg(None, None, line, detect_type(line), platform)
    if cur is not None:
        msgs.append(cur)
    if len(msgs) == 1 and msgs[0]["sender"] is None:
        # 完全无法切分：保留原文不丢，标记时间未知
        msgs[0]["text"] = text.strip()
    return msgs


def main(argv=None):
    ap = argparse.ArgumentParser(description="溯洄 · 聊天记录解析 → 标准消息流（§5.1）")
    ap.add_argument("inputs", nargs="+", help="聊天记录文件或照片目录（可多个，时间线合并）")
    ap.add_argument("--out", default=".", help="输出目录（默认当前目录）")
    ap.add_argument("--platform", default=None,
                    help="强制平台标签（wechat/douyin/qq/telegram/sms/...；默认自动）")
    ap.add_argument("--map", default=None,
                    help='锚点发送者映射，如 \'{"小美":"B","我":"A"}\'（§8 多段校准）')
    args = ap.parse_args(argv)
    forced_map = None
    if args.map:
        try:
            forced_map = json.loads(args.map)
        except ValueError:
            sys.stderr.write("--map 不是合法 JSON\n")
            return 1
    os.makedirs(args.out, exist_ok=True)
    return parse_files(args.inputs, args.platform, forced_map, args.out)


if __name__ == "__main__":
    sys.exit(main())

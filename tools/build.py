#!/usr/bin/env python3
"""
build.py — single entry point that turns Discord exports + manual messages
into the static data the page consumes.

    python tools/build.py              # normal build
    python tools/build.py --dry-run    # show what would change, write nothing
    python tools/build.py --clean      # also remove generated media no longer referenced
    python tools/build.py --tiles      # also generate Deep Zoom tiles for the Magma board
    python tools/build.py --force      # reprocess media even if the cache says it is current

Inputs
    inbox/discord/*.json   DiscordChatExporter exports (with their *_Files/ media folders)
    data/manual.json       hand-written messages (output format, ids like "manual-001")
    data/overrides.json    per-id tweaks: lang / content / author / hidden / pinned
    img/magma/source.png   the retouched Magma board

Outputs (generated — do not edit by hand)
    data/messages.json     the message wall
    data/version.json      content hashes used for cache-busting
    img/avatars/ img/attachments/ img/emoji/   copied + optimised media
    img/magma/magma-web.webp img/magma/magma-full.webp   (+ tiles/ with --tiles)

Only the standard library is required. Pillow is used when available to
resize/convert images and to build Deep Zoom tiles; without it the script
still works (copies originals untouched) and prints a warning.
"""

import argparse
import hashlib
import html as htmllib
import json
import os
import re
import shutil
import sys
import urllib.request
from html.parser import HTMLParser

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INBOX_DISCORD = os.path.join(ROOT, "inbox", "discord")
INBOX_DISCORD_HTML = os.path.join(ROOT, "inbox", "discord-html")
INBOX_MANUAL = os.path.join(ROOT, "inbox", "manual")
INBOX_DECOR = os.path.join(ROOT, "inbox", "decor")
DATA_DIR = os.path.join(ROOT, "data")
IMG_DIR = os.path.join(ROOT, "img")
MAGMA_DIR = os.path.join(IMG_DIR, "magma")

MESSAGES_JSON = os.path.join(DATA_DIR, "messages.json")
VERSION_JSON = os.path.join(DATA_DIR, "version.json")
MANUAL_JSON = os.path.join(DATA_DIR, "manual.json")
OVERRIDES_JSON = os.path.join(DATA_DIR, "overrides.json")
CACHE_JSON = os.path.join(DATA_DIR, ".build-cache.json")

def find_magma_source():
    """The Magma board source: img/magma/source.{png,jpg,jpeg,webp} (first found)."""
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        p = os.path.join(MAGMA_DIR, "source" + ext)
        if os.path.exists(p):
            return p
    return None

# Sentinel tokens that survive HTML escaping in app.js (interlinear controls,
# never present in real text). Keep in sync with assets/app.js.
TOK_A = "￹"
TOK_B = "￺"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v"}
AVATAR_MAX = 128
ATTACH_MAX = 2560  # big enough to zoom into inside the viewer
EMOJI_MAX = 64
STICKER_MAX = 320
DECOR_MAX = 640

try:
    from PIL import Image

    HAVE_PIL = True
except Exception:
    HAVE_PIL = False


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
def sha1_bytes(data):
    return hashlib.sha1(data).hexdigest()


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  ! could not read {os.path.relpath(path, ROOT)}: {e}")
        return default


def web_path(abs_path):
    """Repo-relative path with forward slashes, for use in JSON/HTML."""
    return os.path.relpath(abs_path, ROOT).replace(os.sep, "/")


def parse_ts(s):
    """Tolerant ISO8601 parser returning a sortable string; falls back to raw."""
    return s or ""


# --------------------------------------------------------------------------- #
# Media cache
# --------------------------------------------------------------------------- #
class MediaCache:
    """Maps a generated web path -> sha1 of its source, to skip re-processing."""

    def __init__(self):
        self.data = read_json(CACHE_JSON, default={}) or {}
        self.referenced = set()

    def current(self, web, src_hash):
        return self.data.get(web) == src_hash

    def mark(self, web, src_hash):
        self.data[web] = src_hash
        self.referenced.add(web)

    def touch(self, web):
        self.referenced.add(web)

    def save(self):
        with open(CACHE_JSON, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)


# --------------------------------------------------------------------------- #
# Image emission (copy / optimise with cache)
# --------------------------------------------------------------------------- #
def _load_source_bytes(src):
    """src is ('path', abspath) or ('url', url) or ('bytes', data)."""
    kind, val = src
    if kind == "bytes":
        return val
    if kind == "path":
        with open(val, "rb") as f:
            return f.read()
    if kind == "url":
        req = urllib.request.Request(val, headers={"User-Agent": "mother-build/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    raise ValueError(src)


def emit_image(src, src_ext, out_dir_abs, stem, max_dim, cache, ctx):
    """
    Copy/optimise one image into out_dir_abs/stem.* and return (web_path, is_new).
    Uses the cache to avoid reprocessing unchanged sources. Honours dry-run.
    """
    ext = (src_ext or "").lower()
    # Preserve animation for gif; otherwise prefer webp when Pillow is available.
    if ext == ".gif":
        out_name = stem + ".gif"
    elif HAVE_PIL:
        out_name = stem + ".webp"
    else:
        out_name = stem + (ext if ext in IMAGE_EXTS else ".png")

    dest_abs = os.path.join(out_dir_abs, out_name)
    web = web_path(dest_abs)
    cache.touch(web)

    # Remote media is immutable: once produced, never re-download on later builds
    # (Discord signed URLs expire). Skip without fetching.
    if (not ctx["force"] and src[0] == "url"
            and os.path.exists(dest_abs) and web in cache.data):
        return web, False

    try:
        data = _load_source_bytes(src)
    except Exception as e:
        print(f"  ! skipped image ({e}): {src[1] if len(src) > 1 else src}")
        return None, False

    src_hash = sha1_bytes(data)
    if not ctx["force"] and os.path.exists(dest_abs) and cache.current(web, src_hash):
        return web, False  # up to date

    if ctx["dry_run"]:
        return web, True  # would be (re)written

    os.makedirs(out_dir_abs, exist_ok=True)
    if ext == ".gif" or not HAVE_PIL:
        with open(dest_abs, "wb") as f:
            f.write(data)
    else:
        try:
            import io

            im = Image.open(io.BytesIO(data))
            im = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")
            w, h = im.size
            if max(w, h) > max_dim:
                scale = max_dim / float(max(w, h))
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            im.save(dest_abs, "WEBP", quality=82, method=6)
        except Exception as e:
            print(f"  ! Pillow failed on an image, copying as-is ({e})")
            fallback = os.path.join(out_dir_abs, stem + (ext if ext in IMAGE_EXTS else ".png"))
            with open(fallback, "wb") as f:
                f.write(data)
            dest_abs = fallback
            web = web_path(dest_abs)
            cache.touch(web)

    cache.mark(web, src_hash)
    return web, True


def emit_raw(src, out_dir_abs, out_name, cache, ctx):
    """Copy/download a file verbatim (no image processing). For videos."""
    dest_abs = os.path.join(out_dir_abs, out_name)
    web = web_path(dest_abs)
    cache.touch(web)
    # Remote media is immutable: skip re-download once produced (URLs expire).
    if (not ctx["force"] and src[0] == "url"
            and os.path.exists(dest_abs) and web in cache.data):
        return web, False
    try:
        data = _load_source_bytes(src)
    except Exception as e:
        print(f"  ! skipped file ({e})")
        return None, False
    h = sha1_bytes(data)
    if not ctx["force"] and os.path.exists(dest_abs) and cache.current(web, h):
        return web, False
    if ctx["dry_run"]:
        return web, True
    os.makedirs(out_dir_abs, exist_ok=True)
    with open(dest_abs, "wb") as f:
        f.write(data)
    cache.mark(web, h)
    return web, True


YOUTUBE_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?(?:[^\"'&]*&)*v=|embed/|shorts/|live/)"
    r"|youtu\.be/)([A-Za-z0-9_-]{11})",
    re.I,
)


def youtube_id(url):
    m = YOUTUBE_RE.search(url or "")
    return m.group(1) if m else None


def resolve_media_source(url, base_dir):
    """Return ('path', abs) for a local file or ('url', url) for a remote one."""
    if not url:
        return None, None
    if url.startswith("http://") or url.startswith("https://"):
        return ("url", url), os.path.splitext(url.split("?")[0])[1]
    local = os.path.normpath(os.path.join(base_dir, url))
    if os.path.exists(local):
        return ("path", local), os.path.splitext(local)[1]
    # Maybe it is already a repo-relative path.
    local2 = os.path.normpath(os.path.join(ROOT, url))
    if os.path.exists(local2):
        return ("path", local2), os.path.splitext(local2)[1]
    print(f"  ! media not found: {url}")
    return None, None


# --------------------------------------------------------------------------- #
# Content rendering (mentions, custom emoji, cleanup)
# --------------------------------------------------------------------------- #
CUSTOM_EMOJI_RE = re.compile(r"<(a)?:([A-Za-z0-9_]+):(\d+)>")
USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")


def clean_content(text, mention_names, emoji_map, cache, ctx):
    """
    Resolve mentions and custom emoji into safe sentinel tokens that app.js
    turns into the only HTML we allow. Returns processed content string.
    """
    if not text:
        return ""
    text = text.replace(TOK_A, "").replace(TOK_B, "")  # strip any injected sentinels

    def mention_sub(m):
        name = mention_names.get(m.group(1), "unknown")
        return f"{TOK_A}m|{name}{TOK_B}"

    text = USER_MENTION_RE.sub(mention_sub, text)
    text = ROLE_MENTION_RE.sub(lambda m: f"{TOK_A}m|role{TOK_B}", text)
    text = CHANNEL_MENTION_RE.sub(lambda m: "#channel", text)

    def emoji_sub(m):
        animated, name, eid = m.group(1), m.group(2), m.group(3)
        info = emoji_map.get(eid)
        web = None
        if info:
            src, ext = info
            if src:
                web, _ = emit_image(
                    src, ext, os.path.join(IMG_DIR, "emoji"), eid, EMOJI_MAX, cache, ctx
                )
        if web:
            return f"{TOK_A}e|{web}|{name}{TOK_B}"
        return f":{name}:"  # image unavailable -> plain text fallback

    text = CUSTOM_EMOJI_RE.sub(emoji_sub, text)
    return text.replace("\r\n", "\n").strip("\n")


# --------------------------------------------------------------------------- #
# Discord web-client HTML parsing
# --------------------------------------------------------------------------- #
class _ContentRenderer(HTMLParser):
    """
    Turn the inner HTML of a Discord `message-content-*` node into our content
    string: plain text + light markdown + safe sentinel tokens for custom emoji
    and mentions. Emoji images are downloaded into img/emoji/.
    """

    def __init__(self, cache, ctx):
        super().__init__(convert_charrefs=True)
        self.cache = cache
        self.ctx = ctx
        self.parts = []
        self.in_mention = 0
        self.mbuf = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "") or ""
        if tag == "img" and "emoji" in cls:
            raw_alt = a.get("alt") or ""
            src = a.get("src") or ""
            # Custom Discord emoji are served from /emojis/ (alt may or may not
            # carry surrounding colons); standard emoji are Twemoji svgs whose
            # alt IS the unicode character.
            if src.startswith("http") and "/emojis/" in src:
                name = raw_alt.strip(":") or "emoji"
                web, _ = emit_image(
                    ("url", htmllib.unescape(src)), ".webp",
                    os.path.join(IMG_DIR, "emoji"),
                    a.get("data-id") or name, EMOJI_MAX, self.cache, self.ctx,
                )
                self.parts.append(
                    f"{TOK_A}e|{web}|{name}{TOK_B}" if web else f":{name}:"
                )
            else:
                self.parts.append(raw_alt)
        elif tag == "span" and "mention" in cls:
            self.in_mention += 1
            if self.in_mention == 1:
                self.mbuf = []
        elif self.in_mention and tag == "span":
            self.in_mention += 1
        elif tag in ("strong", "b"):
            self.parts.append("**")
        elif tag in ("em", "i"):
            self.parts.append("*")
        elif tag == "code":
            self.parts.append("`")
        elif tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.in_mention and tag == "span":
            self.in_mention -= 1
            if self.in_mention == 0:
                name = "".join(self.mbuf).strip().lstrip("@")
                self.parts.append(f"{TOK_A}m|{name}{TOK_B}")
            return
        if tag in ("strong", "b"):
            self.parts.append("**")
        elif tag in ("em", "i"):
            self.parts.append("*")
        elif tag == "code":
            self.parts.append("`")
        elif tag == "blockquote":
            self.parts.append("\n")

    def handle_data(self, data):
        data = data.replace(TOK_A, "").replace(TOK_B, "")
        if self.in_mention:
            self.mbuf.append(data)
        else:
            self.parts.append(data)

    def text(self):
        return "".join(self.parts)


def _search(block, pattern, flags=0, group=1):
    m = re.search(pattern, block, flags)
    return m.group(group) if m else None


def parse_discord_html(cache, ctx, counters):
    """Parse saved Discord web-client pages from inbox/discord-html/*.html."""
    by_id = {}
    if not os.path.isdir(INBOX_DISCORD_HTML):
        return by_id

    files = sorted(
        f for f in os.listdir(INBOX_DISCORD_HTML)
        if f.lower().endswith((".html", ".htm"))
    )
    for fname in files:
        raw = open(os.path.join(INBOX_DISCORD_HTML, fname), "rb").read()
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            html = raw.decode("cp1252", errors="replace")

        starts = [
            (m.group(1), m.start())
            for m in re.finditer(r'<li id="chat-messages-\d+-(\d+)"', html)
        ]
        if not starts:
            print(f"  ! {fname}: no Discord messages found (wrong HTML?)")
            continue

        last_author = last_avatar = last_uid = None
        prev_entry = None  # last message added (for merging sticker-only msgs)
        for i, (mid, pos) in enumerate(starts):
            end = starts[i + 1][1] if i + 1 < len(starts) else len(html)
            block = html[pos:end]

            author = _search(
                block, r'id="message-username-%s".*?data-text="([^"]*)"' % mid,
                re.S,
            )
            author = htmllib.unescape(author) if author else None

            avatar_url = _search(
                block, r'<img[^>]*class="avatar_[^"]*"[^>]*src="([^"]+)"'
            ) or _search(
                block,
                r'<img[^>]*src="(https://cdn\.discordapp\.com/(?:guilds|avatars)[^"]+)"',
            )
            avatar_url = htmllib.unescape(avatar_url) if avatar_url else None
            uid = None
            if avatar_url:
                mm = re.search(r"/users/(\d+)/|/avatars/(\d+)/", avatar_url)
                if mm:
                    uid = mm.group(1) or mm.group(2)

            # Compact/grouped messages repeat neither author nor avatar.
            if not author:
                author = last_author
            else:
                last_author = author
            if not avatar_url:
                avatar_url, uid = last_avatar, uid or last_uid
            else:
                last_avatar, last_uid = avatar_url, uid or last_uid

            date = _search(
                block,
                r'id="message-timestamp-%s"[^>]*datetime="([^"]+)"' % mid,
            ) or ""

            # Content: slice between the content node and the accessories node.
            content = ""
            youtube = []
            cm = re.search(r'id="message-content-%s"[^>]*>' % mid, block)
            if cm:
                acc = re.search(r'id="message-accessories-%s"' % mid, block)
                content_html = block[cm.end():(acc.start() if acc else len(block))]
                # Drop the trailing inline "(edited)" marker + its hidden date.
                ts = content_html.find('<span class="timestamp_')
                if ts != -1:
                    content_html = content_html[:ts]
                r = _ContentRenderer(cache, ctx)
                r.feed(content_html)
                content = r.text().replace("\r\n", "\n").strip()
                # YouTube links in the message -> embeds (dedup, keep order).
                for href in re.findall(r'href="([^"]+)"', content_html):
                    yid = youtube_id(htmllib.unescape(href))
                    if yid and yid not in youtube:
                        youtube.append(yid)
                if youtube:
                    # drop the bare URL text so it isn't shown twice.
                    content = re.sub(
                        r"\s*https?://\S*(?:youtube\.com|youtu\.be)\S*", "", content
                    ).strip()

            # Attachments (images + videos) inside the accessories region.
            images = []
            videos = []
            acc = re.search(r'id="message-accessories-%s"' % mid, block)
            if acc:
                region = block[acc.start():]
                seen = set()

                # Images: prefer the full-res originalLink; else any attachment url.
                urls = re.findall(r'class="originalLink_[^"]*"[^>]*href="([^"]+)"', region)
                if not urls:
                    urls = re.findall(
                        r'href="(https://(?:cdn\.discordapp\.com|media\.discordapp\.net)'
                        r'/attachments/[^"]+)"',
                        region,
                    )
                for url in urls:
                    url = htmllib.unescape(url)
                    base = url.split("?")[0]
                    ext = os.path.splitext(base)[1].lower()
                    if ext not in IMAGE_EXTS or base in seen:
                        continue
                    seen.add(base)
                    web, is_new = emit_image(
                        ("url", url), ext, os.path.join(IMG_DIR, "attachments"),
                        f"{mid}-{len(images)}", ATTACH_MAX, cache, ctx,
                    )
                    if web:
                        images.append(web)
                        if is_new:
                            counters["new_images"] += 1

                # Videos: <video src="...mp4" poster="...webp">
                for vm in re.finditer(r"<video\b[^>]*>", region):
                    tag = vm.group(0)
                    vsrc = _search(tag, r'\bsrc="([^"]+)"')
                    if not vsrc:
                        continue
                    vsrc = htmllib.unescape(vsrc)
                    vbase = vsrc.split("?")[0]
                    vext = os.path.splitext(vbase)[1].lower()
                    if vext not in VIDEO_EXTS or vbase in seen:
                        continue
                    seen.add(vbase)
                    vi = len(videos)
                    vweb, is_new = emit_raw(
                        ("url", vsrc), os.path.join(IMG_DIR, "videos"),
                        f"{mid}-{vi}{vext}", cache, ctx,
                    )
                    if not vweb:
                        continue
                    poster = None
                    psrc = _search(tag, r'\bposter="([^"]+)"')
                    if psrc:
                        poster, _ = emit_image(
                            ("url", htmllib.unescape(psrc)), ".webp",
                            os.path.join(IMG_DIR, "videos"),
                            f"{mid}-{vi}-poster", ATTACH_MAX, cache, ctx,
                        )
                    videos.append({"src": vweb, "poster": poster})
                    if is_new:
                        counters["new_images"] += 1

            # Stickers (each usually sent as its own text-less message).
            stickers = []
            if acc:
                for sm in re.finditer(
                    r'<img\b[^>]*data-type="sticker"[^>]*>', block[acc.start():]
                ):
                    stag = sm.group(0)
                    ssrc = _search(stag, r'\bsrc="([^"]+)"')
                    sid = _search(stag, r'\bdata-id="(\d+)"')
                    if not ssrc:
                        continue
                    sweb, _ = emit_image(
                        ("url", htmllib.unescape(ssrc)), ".webp",
                        os.path.join(IMG_DIR, "stickers"),
                        sid or f"{mid}-{len(stickers)}", STICKER_MAX, cache, ctx,
                    )
                    if sweb and sweb not in stickers:
                        stickers.append(sweb)

            has_text = bool(content)
            has_media = bool(images or videos or youtube or stickers)
            if not has_text and not has_media:
                continue  # truly empty

            # Fold any follow-up from the same author as the previous message
            # into it, so one person's consecutive messages form a single card
            # (text concatenated, media combined).
            if prev_entry is not None and prev_entry.get("author") == (author or "unknown"):
                pe = prev_entry
                if content:
                    pe["content"] = (pe["content"] + "\n\n" + content) if pe["content"] else content
                    pe["lang"] = detect_lang(
                        re.sub(r"%s[^%s]*%s" % (TOK_A, TOK_B, TOK_B), " ", pe["content"])
                    )
                for im in images:
                    if im not in pe["images"]:
                        pe["images"].append(im)
                seen_v = {v["src"] for v in pe["videos"]}
                for v in videos:
                    if v["src"] not in seen_v:
                        pe["videos"].append(v)
                        seen_v.add(v["src"])
                for y in youtube:
                    if y not in pe["youtube"]:
                        pe["youtube"].append(y)
                sl = pe.setdefault("stickers", [])
                sl.extend(s for s in stickers if s not in sl)
                continue

            avatar = None
            if avatar_url and avatar_url.startswith("http"):
                avatar, _ = emit_image(
                    ("url", avatar_url), ".webp", os.path.join(IMG_DIR, "avatars"),
                    uid or mid, AVATAR_MAX, cache, ctx,
                )

            entry = {
                "id": mid,
                "author": author or "unknown",
                "avatar": avatar,
                "date": date,
                "lang": detect_lang(re.sub(r"%s[^%s]*%s" % (TOK_A, TOK_B, TOK_B), " ", content)),
                "content": content,
                "images": images,
                "videos": videos,
                "youtube": youtube,
                "stickers": stickers,
                "pinned": False,
                "_edited": date,
            }
            by_id[mid] = entry
            prev_entry = entry

    return by_id


# --------------------------------------------------------------------------- #
# Language heuristic
# --------------------------------------------------------------------------- #
FR_WORDS = {
    "je", "tu", "le", "la", "les", "un", "une", "des", "et", "est", "pour", "merci",
    "joyeux", "joyeuse", "anniversaire", "bon", "bonne", "très", "avec", "ton", "tes",
    "toi", "moi", "ai", "cest", "qui", "que", "pas", "plus", "tout", "toute", "continue",
    "bisous", "sur", "ça", "mon", "ma", "mes", "nous", "vous", "dans", "jai", "fait",
    "année", "années", "meilleur", "meilleure", "coeur", "cœur", "aussi", "depuis",
    "encore", "vraiment", "beaucoup", "espère", "petit", "petite", "au", "aux", "du",
}
EN_WORDS = {
    "the", "you", "your", "and", "happy", "birthday", "for", "thanks", "thank", "this",
    "that", "with", "have", "been", "here", "love", "so", "much", "to", "of", "is",
    "are", "my", "me", "we", "a", "an", "but", "its", "all", "it", "im", "was", "were",
    "best", "year", "years", "since", "still", "really", "hope", "made", "day", "days",
    "from", "wish", "wishing", "amazing", "community", "everything", "keep",
}
WORD_RE = re.compile(r"[a-zàâäéèêëîïôöùûüçœæ]+", re.IGNORECASE)


def detect_lang(text):
    if not text:
        return None
    words = WORD_RE.findall(text.lower().replace("'", ""))
    if not words:
        return None
    fr = sum(1 for w in words if w in FR_WORDS)
    en = sum(1 for w in words if w in EN_WORDS)
    if fr == en:
        return None
    return "FR" if fr > en else "EN"


# --------------------------------------------------------------------------- #
# Discord export parsing
# --------------------------------------------------------------------------- #
SYSTEM_SKIP_TYPES = None  # we keep only Default/Reply instead
KEEP_TYPES = {"Default", "Reply", "0", "19"}


def parse_discord_exports(cache, ctx, counters):
    """Return a dict id -> message (output format) from all inbox exports."""
    by_id = {}
    if not os.path.isdir(INBOX_DISCORD):
        return by_id

    files = sorted(
        f for f in os.listdir(INBOX_DISCORD) if f.lower().endswith(".json")
    )
    for fname in files:
        path = os.path.join(INBOX_DISCORD, fname)
        base_dir = os.path.dirname(path)
        export = read_json(path)
        if not export or "messages" not in export:
            print(f"  ! {fname}: not a DiscordChatExporter JSON, skipping")
            continue

        # Build a mention-name table from every author + mention in this file.
        mention_names = {}
        for msg in export["messages"]:
            a = msg.get("author") or {}
            if a.get("id"):
                mention_names[a["id"]] = a.get("nickname") or a.get("name") or "unknown"
            for men in msg.get("mentions", []) or []:
                if men.get("id"):
                    mention_names[men["id"]] = (
                        men.get("nickname") or men.get("name") or "unknown"
                    )

        for msg in export["messages"]:
            mid = str(msg.get("id", ""))
            if not mid:
                continue
            mtype = str(msg.get("type", "Default"))
            if mtype not in KEEP_TYPES:
                continue
            author = msg.get("author") or {}
            if author.get("isBot"):
                continue

            # Custom-emoji image map: prefer DCE's inlineEmojis (local when --media).
            emoji_map = {}
            for e in msg.get("inlineEmojis", []) or []:
                eid = str(e.get("id") or "")
                if not eid:
                    continue
                src, ext = resolve_media_source(e.get("imageUrl"), base_dir)
                emoji_map[eid] = (src, ext)

            content = clean_content(
                msg.get("content", ""), mention_names, emoji_map, cache, ctx
            )

            # YouTube links -> embeds (and drop the bare URL from the text).
            yt = []
            for yid in (youtube_id(w) for w in re.findall(r"https?://\S+", msg.get("content", ""))):
                if yid and yid not in yt:
                    yt.append(yid)
            if yt:
                content = re.sub(
                    r"\s*https?://\S*(?:youtube\.com|youtu\.be)\S*", "", content
                ).strip()

            images = []
            videos = []
            for att in msg.get("attachments", []) or []:
                fn = att.get("fileName") or att.get("url", "")
                ext = os.path.splitext(fn.split("?")[0])[1].lower()
                src, real_ext = resolve_media_source(att.get("url"), base_dir)
                if not src:
                    continue
                if ext in IMAGE_EXTS:
                    web, is_new = emit_image(
                        src, real_ext or ext, os.path.join(IMG_DIR, "attachments"),
                        f"{mid}-{len(images)}", ATTACH_MAX, cache, ctx,
                    )
                    if web:
                        images.append(web)
                        if is_new:
                            counters["new_images"] += 1
                elif ext in VIDEO_EXTS:
                    web, is_new = emit_raw(
                        src, os.path.join(IMG_DIR, "videos"),
                        f"{mid}-{len(videos)}{ext}", cache, ctx,
                    )
                    if web:
                        videos.append({"src": web, "poster": None})
                        if is_new:
                            counters["new_images"] += 1

            if not content and not images and not videos and not yt:
                continue  # empty + no media -> ignore

            # Avatar (optional).
            avatar = None
            asrc, aext = resolve_media_source(author.get("avatarUrl"), base_dir)
            if asrc:
                aid = str(author.get("id") or mid)
                web, _ = emit_image(
                    asrc, aext, os.path.join(IMG_DIR, "avatars"),
                    aid, AVATAR_MAX, cache, ctx,
                )
                avatar = web

            out = {
                "id": mid,
                "author": author.get("nickname") or author.get("name") or "unknown",
                "avatar": avatar,
                "date": msg.get("timestamp", ""),
                "lang": detect_lang(msg.get("content", "")),
                "content": content,
                "images": images,
                "videos": videos,
                "youtube": yt,
                "stickers": [],
                "pinned": False,
                # internal: effective edit time for dedup, stripped before output
                "_edited": msg.get("timestampEdited") or msg.get("timestamp") or "",
            }

            prev = by_id.get(mid)
            if prev is None or parse_ts(out["_edited"]) >= parse_ts(prev["_edited"]):
                by_id[mid] = out

    return by_id


# --------------------------------------------------------------------------- #
# Manual messages
# --------------------------------------------------------------------------- #
def parse_manual(cache, ctx, counters):
    by_id = {}
    manual = read_json(MANUAL_JSON, default=[]) or []
    if isinstance(manual, dict):
        manual = manual.get("messages", [])
    for m in manual:
        mid = str(m.get("id") or "")
        if not mid:
            print("  ! a manual message has no id, skipping")
            continue
        images = []
        for i, img in enumerate(m.get("images", []) or []):
            src, ext = resolve_media_source(img, ROOT)
            if not src:
                continue
            web, is_new = emit_image(
                src, ext, os.path.join(IMG_DIR, "attachments"),
                f"{mid}-{i}", ATTACH_MAX, cache, ctx,
            )
            if web:
                images.append(web)
                if is_new:
                    counters["new_images"] += 1

        avatar = m.get("avatar")
        if avatar:
            asrc, aext = resolve_media_source(avatar, ROOT)
            if asrc:
                web, _ = emit_image(
                    asrc, aext, os.path.join(IMG_DIR, "avatars"),
                    mid, AVATAR_MAX, cache, ctx,
                )
                avatar = web

        content = clean_content(m.get("content", ""), {}, {}, cache, ctx)
        by_id[mid] = {
            "id": mid,
            "author": m.get("author") or "unknown",
            "avatar": avatar,
            "date": m.get("date", ""),
            "lang": m.get("lang") if "lang" in m else detect_lang(m.get("content", "")),
            "content": content,
            "images": images,
            "videos": m.get("videos", []),
            "youtube": m.get("youtube", []),
            "stickers": m.get("stickers", []),
            "pinned": bool(m.get("pinned", False)),
            "_edited": m.get("date", ""),
        }
    return by_id


# --------------------------------------------------------------------------- #
# Overrides
# --------------------------------------------------------------------------- #
def apply_overrides(by_id, counters):
    overrides = read_json(OVERRIDES_JSON, default={}) or {}
    # Authors to hide entirely (e.g. a noisy bot), by display name (case-insensitive).
    hidden_authors = {
        a.lower() for a in overrides.get("__hiddenAuthors__", []) if isinstance(a, str)
    }
    result = {}
    for mid, msg in by_id.items():
        ov = overrides.get(mid, {})
        if ov.get("hidden") or (msg.get("author", "").lower() in hidden_authors):
            counters["hidden"] += 1
            continue
        if "lang" in ov:
            msg["lang"] = ov["lang"]
        if "content" in ov:
            # corrected content is plain text; app.js escapes + markdowns it
            msg["content"] = ov["content"]
        if "author" in ov:
            msg["author"] = ov["author"]
        if ov.get("pinned"):
            msg["pinned"] = True
        result[mid] = msg
    # overrides may reference ids that only matter as hidden; already handled.
    for mid, msg in result.items():
        if msg.get("pinned"):
            counters["pinned"] += 1
    return result


# --------------------------------------------------------------------------- #
# Magma board
# --------------------------------------------------------------------------- #
DECOR_JSON = os.path.join(DATA_DIR, "decor.json")
DECOR_PLACE_KEYS = ("side", "top", "width", "rotate", "opacity", "speed", "inset")


def _decor_side_from_name(name, alt):
    low = name.lower()
    if low.startswith(("left", "l-", "l_")):
        return "left", alt
    if low.startswith(("right", "r-", "r_")):
        return "right", alt
    return ("left" if alt % 2 == 0 else "right"), alt + 1


def _emit_decor_file(fname, cache, ctx, hashes):
    """Optimise one decoration; return its web path (or None)."""
    abspath = os.path.join(INBOX_DECOR, fname)
    if not os.path.exists(abspath):
        print(f"  ! decor file not found: {fname}")
        return None
    ext = os.path.splitext(fname)[1].lower()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.splitext(fname)[0]).strip("-") or "decor"
    web, _ = emit_image(
        ("path", abspath), ext, os.path.join(IMG_DIR, "decor"),
        stem, DECOR_MAX, cache, ctx,
    )
    if web:
        try:
            hashes.append(sha1_file(abspath)[:8])
        except Exception:
            pass
    return web


def build_decor(cache, ctx):
    """
    Optimise side decorations from inbox/decor/ into img/decor/ and return the
    placement list for the page.

    data/decor.json (optional, hand-authored, never overwritten) gives a curated
    layout: a list of items with at least "file" and any of
    side / top(%) / width(px) / rotate(deg) / opacity / speed / inset(px).
    Files present in inbox/decor/ but not listed are appended automatically
    (side from the filename, otherwise alternating).

    Returns (list of placement dicts, short content hash for cache-busting).
    """
    items, hashes = [], []
    if not os.path.isdir(INBOX_DECOR):
        return items, ""

    present = {
        f for f in os.listdir(INBOX_DECOR)
        if os.path.splitext(f)[1].lower() in IMAGE_EXTS
    }
    used = set()

    # 1) Curated items from decor.json, in order.
    layout = read_json(DECOR_JSON, default={}) or {}
    for it in layout.get("items", []) if isinstance(layout, dict) else []:
        fname = it.get("file")
        if not fname:
            continue
        web = _emit_decor_file(fname, cache, ctx, hashes)
        if not web:
            continue
        used.add(fname)
        entry = {"src": web, "side": it.get("side", "left")}
        for k in DECOR_PLACE_KEYS:
            if k in it and k != "side":
                entry[k] = it[k]
        items.append(entry)

    # 2) Any remaining files -> auto placement.
    alt = 0
    for fname in sorted(present - used):
        side, alt = _decor_side_from_name(fname, alt)
        web = _emit_decor_file(fname, cache, ctx, hashes)
        if web:
            items.append({"src": web, "side": side})

    decor_hash = sha1_bytes("".join(hashes).encode())[:12] if hashes else ""
    return items, decor_hash


def build_magma(ctx, version):
    """Generate magma-web / magma-full (+ optional tiles). Returns 'changed'/'unchanged'/'missing'."""
    magma_src = find_magma_source()
    if not magma_src:
        print("  ! img/magma/source.{png,jpg,jpeg,webp} not found — Magma board skipped")
        version["magma"] = None
        version["magmaWeb"] = None
        version["magmaFull"] = None
        version["magmaTiles"] = False
        return "missing"

    src_hash = sha1_file(magma_src)[:16]
    prev = read_json(VERSION_JSON, default={}) or {}
    web_abs = os.path.join(MAGMA_DIR, "magma-web.webp")
    full_abs = os.path.join(MAGMA_DIR, "magma-full.webp")

    tiles_dir = os.path.join(MAGMA_DIR, "tiles")
    dzi_abs = os.path.join(tiles_dir, "magma.dzi")

    need = (
        prev.get("magma") != src_hash
        or not os.path.exists(web_abs)
        or not os.path.exists(full_abs)
        or ctx["force"]
    )
    tiles_need = ctx["tiles"] and (need or not os.path.exists(dzi_abs))

    version["magma"] = src_hash

    if not need and not tiles_need:
        version["magmaWeb"] = web_path(web_abs) if os.path.exists(web_abs) else web_path(magma_src)
        version["magmaFull"] = web_path(full_abs) if os.path.exists(full_abs) else web_path(magma_src)
        version["magmaTiles"] = os.path.exists(dzi_abs)
        if version["magmaTiles"]:
            version["magmaTilesDir"] = web_path(dzi_abs)
        return "unchanged"

    if ctx["dry_run"]:
        version["magmaWeb"] = web_path(web_abs)
        version["magmaFull"] = web_path(full_abs)
        version["magmaTiles"] = bool(ctx["tiles"])
        if ctx["tiles"]:
            version["magmaTilesDir"] = web_path(dzi_abs)
        return "changed"

    if not HAVE_PIL:
        print("  ! Pillow not installed — Magma served as-is (no resize/webp)")
        version["magmaWeb"] = web_path(magma_src)
        version["magmaFull"] = web_path(magma_src)
        version["magmaTiles"] = False
        return "changed"

    im = Image.open(magma_src)
    im = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")

    # Full resolution, optimised.
    im.save(full_abs, "WEBP", quality=88, method=6)
    version["magmaFull"] = web_path(full_abs)

    # Lightweight ~2000px-wide version for the card.
    w, h = im.size
    if w > 2000:
        wim = im.resize((2000, max(1, int(h * 2000 / w))), Image.LANCZOS)
    else:
        wim = im
    wim.save(web_abs, "WEBP", quality=82, method=6)
    version["magmaWeb"] = web_path(web_abs)

    version["magmaTiles"] = False
    if ctx["tiles"]:
        make_dzi(im, tiles_dir, "magma")
        version["magmaTiles"] = True
        version["magmaTilesDir"] = web_path(dzi_abs)

    return "changed"


def make_dzi(im, tiles_dir, name, tile_size=254, overlap=1):
    """Write a Deep Zoom (.dzi + _files pyramid) for OpenSeadragon."""
    if os.path.isdir(os.path.join(tiles_dir, name + "_files")):
        shutil.rmtree(os.path.join(tiles_dir, name + "_files"))
    os.makedirs(tiles_dir, exist_ok=True)
    w, h = im.size
    max_level = (max(w, h) - 1).bit_length()  # ceil(log2(max dim))
    files_dir = os.path.join(tiles_dir, name + "_files")

    for level in range(max_level + 1):
        scale = 2 ** (max_level - level)
        lw = max(1, (w + scale - 1) // scale)
        lh = max(1, (h + scale - 1) // scale)
        level_img = im.resize((lw, lh), Image.LANCZOS)
        level_dir = os.path.join(files_dir, str(level))
        os.makedirs(level_dir, exist_ok=True)
        cols = (lw + tile_size - 1) // tile_size
        rows = (lh + tile_size - 1) // tile_size
        for col in range(cols):
            for row in range(rows):
                x = col * tile_size - (overlap if col > 0 else 0)
                y = row * tile_size - (overlap if row > 0 else 0)
                x2 = min(col * tile_size + tile_size + overlap, lw)
                y2 = min(row * tile_size + tile_size + overlap, lh)
                tile = level_img.crop((x, y, x2, y2))
                tile.save(os.path.join(level_dir, f"{col}_{row}.webp"), "WEBP", quality=80)

    dzi = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Image xmlns="http://schemas.microsoft.com/deepzoom/2008"\n'
        f'  Format="webp" Overlap="{overlap}" TileSize="{tile_size}">\n'
        f'  <Size Width="{w}" Height="{h}"/>\n'
        "</Image>\n"
    )
    with open(os.path.join(tiles_dir, name + ".dzi"), "w", encoding="utf-8") as f:
        f.write(dzi)


# --------------------------------------------------------------------------- #
# Clean unreferenced media
# --------------------------------------------------------------------------- #
def clean_unreferenced(referenced, ctx):
    removed = 0
    for sub in ("avatars", "attachments", "emoji", "videos", "decor", "stickers"):
        d = os.path.join(IMG_DIR, sub)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            web = f"img/{sub}/{fn}"
            if web not in referenced:
                removed += 1
                if not ctx["dry_run"]:
                    os.remove(os.path.join(d, fn))
    return removed


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Build the MOTHER birthday page data.")
    ap.add_argument("--dry-run", action="store_true", help="show changes, write nothing")
    ap.add_argument("--clean", action="store_true", help="remove generated media no longer referenced")
    ap.add_argument("--tiles", action="store_true", help="generate Deep Zoom tiles for the Magma board")
    ap.add_argument("--force", action="store_true", help="reprocess media even if cached")
    args = ap.parse_args()

    try:  # keep accents/em-dashes readable on Windows consoles
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ctx = {"dry_run": args.dry_run, "tiles": args.tiles, "force": args.force}
    counters = {"new_images": 0, "hidden": 0, "pinned": 0}

    if not HAVE_PIL:
        print("  (Pillow not installed: images are copied without resize/webp)")

    os.makedirs(DATA_DIR, exist_ok=True)
    cache = MediaCache()

    # Previous state (to report how many messages are new).
    prev_msgs = read_json(MESSAGES_JSON, default={}) or {}
    prev_ids = {m["id"] for m in prev_msgs.get("messages", [])} if isinstance(prev_msgs, dict) else {
        m["id"] for m in prev_msgs
    }

    # Gather messages: Discord JSON, then saved HTML (merged by id, newest edit
    # wins), then manual (manual always wins on an id clash).
    by_id = parse_discord_exports(cache, ctx, counters)
    for mid, msg in parse_discord_html(cache, ctx, counters).items():
        prev = by_id.get(mid)
        if prev is None or parse_ts(msg["_edited"]) >= parse_ts(prev["_edited"]):
            by_id[mid] = msg
    by_id.update(parse_manual(cache, ctx, counters))
    by_id = apply_overrides(by_id, counters)

    messages = list(by_id.values())
    messages.sort(key=lambda m: parse_ts(m.get("date", "")))
    for m in messages:
        m.pop("_edited", None)

    out = {
        "_generated": "Do not edit by hand — produced by tools/build.py.",
        "count": len(messages),
        "messages": messages,
    }
    messages_blob = json.dumps(out, ensure_ascii=False, indent=2, sort_keys=False)
    messages_hash = sha1_bytes(messages_blob.encode("utf-8"))[:16]

    # Version / cache-busting file.
    version = read_json(VERSION_JSON, default={}) or {}
    version["_generated"] = "Do not edit by hand — produced by tools/build.py."
    version["messages"] = messages_hash
    magma_state = build_magma(ctx, version)
    version["decor"], version["decorHash"] = build_decor(cache, ctx)

    new_count = len(set(by_id.keys()) - prev_ids)

    # Write outputs.
    if not args.dry_run:
        with open(MESSAGES_JSON, "w", encoding="utf-8") as f:
            f.write(messages_blob)
        with open(VERSION_JSON, "w", encoding="utf-8") as f:
            json.dump(version, f, ensure_ascii=False, indent=2)
        cache.save()

    removed = 0
    if args.clean:
        # Referenced set from the FINAL (post-override) messages, so media that
        # belonged only to hidden/removed messages is purged too.
        referenced = set()
        for m in messages:
            if m.get("avatar"):
                referenced.add(m["avatar"])
            for img in m.get("images", []):
                referenced.add(img)
            for vid in m.get("videos", []):
                if vid.get("src"):
                    referenced.add(vid["src"])
                if vid.get("poster"):
                    referenced.add(vid["poster"])
            for st in m.get("stickers", []):
                referenced.add(st)
            for em in re.findall(r"%s e\|([^|]+)\|" % TOK_A, m.get("content", ""), re.X):
                referenced.add(em)
        for d in version.get("decor", []):
            referenced.add(d["src"])
        removed = clean_unreferenced(referenced, ctx)

    # Summary.
    parts = [
        f"+{new_count} new message{'s' if new_count != 1 else ''}",
        f"{counters['new_images']} new image{'s' if counters['new_images'] != 1 else ''}",
        f"{counters['hidden']} hidden",
        f"{counters['pinned']} pinned",
        f"Magma {magma_state}",
        f"total {len(messages)}",
    ]
    if args.clean:
        parts.append(f"{removed} media removed")
    prefix = "[dry-run] would be: " if args.dry_run else ""
    print(prefix + ", ".join(parts))


if __name__ == "__main__":
    main()

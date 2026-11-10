"""
Channel Saver + Thumbnail Bot (GitHub Actions वर्ज़न: सारी वैल्यू env से)

काम:
  1. बॉट को एक फ़ोटो भेजें            -> वही थंबनेल (कवर) बन जाती है
  2. पब्लिक या प्राइवेट चैनल का लिंक भेजें (सिंगल या रेंज)
  3. बॉट हर पोस्ट TARGET_CHANNEL में भेजता है:
       - वीडियो                       -> आपके थंबनेल के साथ
       - फ़ोटो / फ़ाइल / ऑडियो / टेक्स्ट -> जैसा का तैसा
     कैप्शन से @username, #hashtag और लिंक हटा दिए जाते हैं।

प्राइवेट चैनल (नीचे SESSION_STRING वाले अकाउंट से):
  - फॉरवर्डिंग ON: पोस्ट बिना डाउनलोड/अपलोड के रिले होती है।
  - सेव बंद (Restrict saving content): पोस्ट डाउनलोड होकर नए थंबनेल/कैप्शन से अपलोड होती है।

Install:  pip install -r requirements.txt
Run:      python saver_bot.py   (पहले env वैल्यू सेट करें)

ज़रूरी:
  - बॉट को TARGET_CHANNEL में ADMIN बनाएं (पोस्ट करने की अनुमति के साथ)
  - SESSION_STRING वाला अकाउंट बॉट को एक बार /start कर चुका हो
  - वही अकाउंट उस प्राइवेट चैनल का मेंबर हो
"""
import os
import re
import json
import html as _html
from urllib.parse import urlparse
import time
import shutil
import asyncio

# Python 3.12+ पर pyrogram import से पहले इवेंट लूप चाहिए
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import aiohttp
from pyrogram import Client, filters, enums, idle, StopPropagation
from pyrogram.errors import FloodWait
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

# ================= सारी वैल्यू Environment / GitHub Secrets से =================
def _need(name):
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"❌ Environment variable '{name}' सेट नहीं है। GitHub Secrets में जोड़ें।")
    return v


API_ID = int(_need("API_ID"))
API_HASH = _need("API_HASH")
BOT_TOKEN = _need("BOT_TOKEN")
SESSION_STRING = _need("SESSION_STRING")   # API_ID/API_HASH वही जिनसे यह बनी थी

TARGET_CHANNEL = int(_need("TARGET_CHANNEL"))
# कई ओनर हों तो कॉमा से: 111,222
OWNER_IDS = [int(x) for x in _need("OWNER_IDS").replace(" ", "").split(",") if x]

# डाउनलोड फोल्डर (वैकल्पिक)
DL_DIR = os.environ.get("DL_DIR") or os.path.join(os.getcwd(), "downloads")
# ===========================================================================

MAX_RANGE = 10000      # एक रेंज में अधिकतम पोस्ट
DELAY = 1.5          # हर पोस्ट के बीच इंतज़ार (सेकंड)
RELAY_TIMEOUT = 40   # प्राइवेट पोस्ट के बॉट तक पहुंचने का अधिकतम इंतज़ार (सेकंड)
THUMB_FILE = "thumb.json"   # थंबनेल का file_id यहां सेव होता है

app = Client("saver_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
user = Client("saver_user", api_id=API_ID, api_hash=API_HASH,
              session_string=SESSION_STRING, in_memory=True, no_updates=True)

LINK_RE = re.compile(
    r"(?:https?://)?t\.me/(?P<user>[A-Za-z][A-Za-z0-9_]{3,})/(?P<a>\d+)(?:-(?P<b>\d+))?"
)
PRIV_RE = re.compile(
    r"(?:https?://)?t\.me/c/(?P<chat>\d+)(?:/\d+)?/(?P<a>\d+)(?:-(?P<b>\d+))?"
)

RUNNING = set()       # जिन यूज़र्स का टास्क चल रहा है
CANCEL = set()        # जिन्होंने /cancel किया
RELAY = {}            # telegram_id -> Future (प्राइवेट पोस्ट बॉट तक आने का इंतज़ार)
BOT_USERNAME = ""
USER_TG_ID = None     # SESSION_STRING वाले अकाउंट की ID
THUMB_ID = None
THUMB_CACHE = {}
owner_filter = filters.user(OWNER_IDS)


# ---------------- थंबनेल सेव / लोड (thumb.json में) ----------------
def load_thumb():
    try:
        with open(THUMB_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("file_id")
    except Exception:
        return None


def save_thumb(file_id):
    with open(THUMB_FILE, "w", encoding="utf-8") as f:
        json.dump({"file_id": file_id}, f)


def clear_thumb():
    if os.path.exists(THUMB_FILE):
        os.remove(THUMB_FILE)


# ---------------- कैप्शन / टेक्स्ट की सफ़ाई ----------------
CLEAN_CAPTIONS = True   # False करने पर कैप्शन जैसा का तैसा जाएगा

_A_TAG = re.compile(r"<a\b[^>]*>.*?</a>", re.I | re.S)
_URL = re.compile(r"(?:https?://|www\.|(?:t|telegram)\.me/)[^\s<]+", re.I)
_MENTION = re.compile(r"(?<![\w&/.])@[A-Za-z0-9_]{3,}")
_HASHTAG = re.compile(r"(?<![\w&])#[^\s#<>]+")
_EMPTY_TAG = re.compile(
    r"<(b|i|u|s|em|strong|code|pre|blockquote|tg-spoiler)\b[^>]*>\s*</\1>", re.I
)


# इन साइटों के लिंक कभी नहीं हटते (बाकी सब लिंक हटते हैं)। ज़रूरत हो तो यहां और जोड़ें।
KEEP_DOMAINS = (
    "drive.google.com", "docs.google.com", "drive.usercontent.google.com",
    "mega.nz", "mega.io", "mega.co.nz",
)
_A_FULL = re.compile(r"<a\b[^>]*?\bhref=([\"'])(.*?)\1[^>]*>.*?</a>", re.I | re.S)
_KEEP_URL = re.compile(
    r"(?<![\w@./-])(?:https?://)?(?:[\w-]+\.)*(?:"
    + "|".join(re.escape(d) for d in KEEP_DOMAINS)
    + r")(?:/[^\s<]*)?", re.I)


def _keep_host(url: str) -> bool:
    try:
        u = _html.unescape(url).strip()
        host = (urlparse(u if "//" in u else "//" + u).hostname or "").lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in KEEP_DOMAINS)


def clean_html(html: str) -> str:
    """@username, #hashtag, लिंक और टेक्स्ट-लिंक हटा देता है।
    पर Google Drive और Mega के लिंक (सीधे लिखे हों या टेक्स्ट-लिंक) जैसे हैं वैसे रहते हैं।"""
    if not html or not CLEAN_CAPTIONS:
        return html or ""

    # रखने वाले लिंक अलग रख दो, सफ़ाई के बाद वापस लगा देंगे
    keep = []

    def stash(piece):
        keep.append(piece)
        return f"\x00K{len(keep) - 1}\x00"

    text = _A_FULL.sub(lambda m: stash(m.group(0)) if _keep_host(m.group(2)) else m.group(0), html)
    text = _KEEP_URL.sub(lambda m: stash(m.group(0)), text)

    text = _A_TAG.sub("", text)
    text = _URL.sub("", text)
    text = _MENTION.sub("", text)
    text = _HASHTAG.sub("", text)
    prev = None
    while prev != text:                       # खाली बचे टैग हटाओ
        prev = text
        text = _EMPTY_TAG.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    for i, piece in enumerate(keep):
        text = text.replace(f"\x00K{i}\x00", piece)
    return text.strip()


# ---------------- बटन वाले लिंक: पोस्ट के अंदर जोड़ना ----------------
KEEP_BUTTON_LINKS = True   # False करने पर बटन के लिंक नहीं जुड़ेंगे


def button_rows(msg):
    """पोस्ट के इनलाइन बटन से सिर्फ़ URL वाले बटन [(टेक्स्ट, लिंक)] की पंक्तियां।"""
    rm = getattr(msg, "reply_markup", None)
    rows = []
    for row in (getattr(rm, "inline_keyboard", None) or []):
        r = [(b.text, b.url) for b in row if getattr(b, "url", None)]
        if r:
            rows.append(r)
    return rows


def kb(rows):
    if not rows:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, url=u) for t, u in r] for r in rows])


def kb_json(rows):
    return {"inline_keyboard": [[{"text": t, "url": u} for t, u in r] for r in rows]}


def _visible_len(h):
    return len(_html.unescape(re.sub(r"<[^>]+>", "", h)))


def compose(msg, base_html, limit):
    """सफ़ाई के बाद के टेक्स्ट में बटन के लिंक जोड़ता है।
    लिंक टेक्स्ट में समा जाएं तो (टेक्स्ट, None) देता है।
    लिमिट (कैप्शन 1024 / टेक्स्ट 4096) पार हो तो टेक्स्ट वैसा ही रखकर (टेक्स्ट, बटन-पंक्तियां) देता है,
    ताकि लिंक बटन के रूप में लगे।"""
    rows = button_rows(msg) if KEEP_BUTTON_LINKS else []
    if not rows:
        return base_html, None
    block = "\n".join(
        f'🔗 <a href="{_html.escape(u, quote=True)}">{_html.escape(t)}</a>'
        for r in rows for t, u in r)
    combined = f"{base_html}\n\n{block}" if base_html else block
    if _visible_len(combined) <= limit:
        return combined, None
    return base_html, rows


# ---------------- Bot API से कवर के साथ वीडियो भेजना ----------------
async def send_video_with_cover(msg: Message, cover_id: str, final=None) -> bool:
    """file_id से वीडियो नए कवर के साथ भेजता है। सफल होने पर True।
    final = (कैप्शन_html, बटन_पंक्तियां) दें तो वही इस्तेमाल होगा, वरना मैसेज से बनेगा।"""
    v = msg.video
    if final is None:
        final = compose(msg, clean_html(msg.caption.html) if msg.caption else "", 1024)
    caption, rows = final
    payload = {
        "chat_id": TARGET_CHANNEL,
        "video": v.file_id,
        "cover": cover_id,
        "supports_streaming": True,
    }
    if v.duration:
        payload["duration"] = v.duration
    if v.width:
        payload["width"] = v.width
    if v.height:
        payload["height"] = v.height
    if caption:
        payload["caption"] = caption
        payload["parse_mode"] = "HTML"
    if rows:
        payload["reply_markup"] = kb_json(rows)

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            data = await resp.json()
            if data.get("ok"):
                return True
            if data.get("error_code") == 429:
                wait = data.get("parameters", {}).get("retry_after", 5)
                await asyncio.sleep(wait + 1)
                async with session.post(url, json=payload) as resp2:
                    return (await resp2.json()).get("ok", False)
            print("sendVideo error:", data)
            return False


async def _retry(func):
    try:
        return await func()
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        return await func()


async def plain_copy(client: Client, msg: Message, username: str) -> bool:
    """पब्लिक पोस्ट चैनल में भेजता है (सफ़ाई के बाद, बटन के लिंक जोड़कर)।
    भेजने लायक कुछ न बचे तो False।"""
    if msg.text:
        text, rows = compose(msg, clean_html(msg.text.html), 4096)
        if not text:
            return False
        await _retry(lambda: client.send_message(
            TARGET_CHANNEL, text,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=kb(rows),
        ))
        return True

    cap, rows = compose(msg, clean_html(msg.caption.html) if msg.caption else "", 1024)
    kwargs = {}
    if msg.caption or cap:
        kwargs["caption"] = cap
        kwargs["parse_mode"] = enums.ParseMode.HTML
    await _retry(lambda: client.copy_message(
        TARGET_CHANNEL, username, msg.id, reply_markup=kb(rows), **kwargs
    ))
    return True


# ---------------- प्राइवेट चैनल ----------------
def is_protected(msg: Message) -> bool:
    """चैनल में 'Restrict saving content' ON हो तो True।"""
    chat = getattr(msg, "chat", None)
    return bool(getattr(msg, "has_protected_content", False)
                or getattr(chat, "has_protected_content", False))


def is_restricted_error(e: Exception) -> bool:
    s = str(e).upper()
    return "FORWARDS_RESTRICTED" in s or "NOFORWARDS" in s or "PROTECTED" in s


async def get_thumb_path(client: Client):
    """सेव थंबनेल (file_id) को फाइल के रूप में डाउनलोड करके पाथ देता है।"""
    if not THUMB_ID:
        return None
    p = THUMB_CACHE.get(THUMB_ID)
    if p and os.path.exists(p):
        return p
    os.makedirs(DL_DIR, exist_ok=True)
    name = f"thumb_{abs(hash(THUMB_ID))}.jpg"
    p = await client.download_media(THUMB_ID, file_name=os.path.join(DL_DIR, name))
    THUMB_CACHE.clear()
    THUMB_CACHE[THUMB_ID] = p
    return p


def hbytes(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024


def htime(sec):
    sec = int(max(sec, 0))
    m, s_ = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else (f"{m}m {s_}s" if m else f"{s_}s")


class Bar:
    """डाउनलोड/अपलोड का प्रोग्रेस बार: एक मैसेज जो हर ~4 सेकंड में अपडेट होता है।"""

    def __init__(self, chat_id, title):
        self.chat_id = chat_id
        self.title = title
        self.msg = None
        self.label = ""
        self.start = time.time()
        self.last = 0

    def phase(self, label):
        self.label = label
        self.start = time.time()
        self.last = 0

    async def update(self, current, total):
        now = time.time()
        if current < total and now - self.last < 4:
            return
        self.last = now
        total = total or current or 1
        pct = current * 100 / total
        filled = int(pct / 5)
        bar = "█" * filled + "░" * (20 - filled)
        el = max(now - self.start, 0.001)
        speed = current / el
        eta = (total - current) / speed if speed > 0 else 0
        text = (
            f"{self.label}\n{self.title}\n\n"
            f"[{bar}] {pct:.1f}%\n"
            f"💾 {hbytes(current)} / {hbytes(total)}\n"
            f"⚡ {hbytes(speed)}/s   ⏳ {htime(eta)}"
        )
        try:
            if self.msg is None:
                self.msg = await app.send_message(self.chat_id, text)
            else:
                await self.msg.edit_text(text)
        except Exception:
            pass

    async def close(self):
        if self.msg:
            try:
                await self.msg.delete()
            except Exception:
                pass


async def process_protected(client: Client, acc, msg: Message, chat_id=None) -> str:
    """सेव बंद वाली पोस्ट: यूज़र अकाउंट से डाउनलोड, बॉट से नए थंबनेल/कैप्शन के साथ अपलोड।"""
    tmp = os.path.join(DL_DIR, f"{abs(msg.chat.id)}_{msg.id}")
    os.makedirs(tmp, exist_ok=True)
    bar = Bar(chat_id, f"📌 पोस्ट ID {msg.id}") if chat_id else None
    cb = bar.update if bar else None
    try:
        kw = {}
        cap, rows = compose(msg, clean_html(msg.caption.html) if msg.caption else "", 1024)
        if cap:
            kw["caption"] = cap
            kw["parse_mode"] = enums.ParseMode.HTML
        if rows:
            kw["reply_markup"] = kb(rows)

        if msg.video:
            ext = ".mp4"
        elif msg.photo:
            ext = ".jpg"
        elif msg.audio:
            ext = ".mp3"
        elif msg.voice:
            ext = ".ogg"
        elif msg.animation:
            ext = ".mp4"
        elif msg.document:
            ext = re.sub(r"[^\w.]", "", os.path.splitext(msg.document.file_name or "")[1])[:8]
        else:
            return "skipped"

        # फाइल का नाम खुद तय करते हैं ताकि लंबे/खास अक्षर वाले नाम से एरर न आए
        if bar:
            bar.phase("⬇️ डाउनलोड हो रहा है")
        path = await acc.download_media(
            msg, file_name=os.path.join(tmp, f"{msg.id}{ext}"), progress=cb)
        if not path:
            raise Exception("डाउनलोड नहीं हुआ")

        if bar:
            bar.phase("⬆️ अपलोड हो रहा है")
        if msg.video:
            thumb = await get_thumb_path(client)
            await _retry(lambda: client.send_video(
                TARGET_CHANNEL, path, thumb=thumb,
                duration=msg.video.duration or 0,
                width=msg.video.width or 0,
                height=msg.video.height or 0,
                supports_streaming=True, progress=cb, **kw))
            return "video" if thumb else "other"
        if msg.photo:
            await _retry(lambda: client.send_photo(TARGET_CHANNEL, path, progress=cb, **kw))
        elif msg.audio:
            await _retry(lambda: client.send_audio(TARGET_CHANNEL, path, progress=cb, **kw))
        elif msg.voice:
            await _retry(lambda: client.send_voice(TARGET_CHANNEL, path, progress=cb, **kw))
        elif msg.animation:
            await _retry(lambda: client.send_animation(TARGET_CHANNEL, path, progress=cb, **kw))
        else:
            await _retry(lambda: client.send_document(
                TARGET_CHANNEL, path,
                file_name=msg.document.file_name, progress=cb, **kw))
        return "other"
    finally:
        if bar:
            await bar.close()
        shutil.rmtree(tmp, ignore_errors=True)


async def relay_via_bot(acc, tg_id, source, msg_id, kwargs):
    """यूज़र अकाउंट पोस्ट को बॉट की चैट में कॉपी करता है (डाउनलोड/अपलोड नहीं)।"""
    fut = asyncio.get_running_loop().create_future()
    RELAY[tg_id] = fut
    try:
        await _retry(lambda: acc.copy_message(
            BOT_USERNAME, source, msg_id, reply_markup=None, **kwargs
        ))
        return await asyncio.wait_for(fut, RELAY_TIMEOUT)
    finally:
        RELAY.pop(tg_id, None)


async def deliver_relayed(client: Client, rm: Message, rows=None) -> str:
    """बॉट तक पहुंची पोस्ट TARGET_CHANNEL में भेजता है।"""
    try:
        kind = "other"
        if rm.video and THUMB_ID:
            cap = rm.caption.html if rm.caption else ""
            if await send_video_with_cover(rm, THUMB_ID, final=(cap, rows)):
                return "video"
            kind = "fallback"   # कवर फेल हुआ तो पुराने थंबनेल के साथ
        await _retry(lambda: client.copy_message(
            TARGET_CHANNEL, rm.chat.id, rm.id, reply_markup=kb(rows)
        ))
        return kind
    finally:
        try:
            await rm.delete()
        except Exception:
            pass


async def process_private(client, acc, tg_id, msg: Message, source, chat_id=None) -> str:
    """Return: video / other / fallback / skipped"""
    # सिर्फ़ टेक्स्ट वाली पोस्ट: सेव बंद हो तब भी टेक्स्ट भेजा जा सकता है
    if msg.text:
        text, rows = compose(msg, clean_html(msg.text.html), 4096)
        if not text:
            return "skipped"
        await _retry(lambda: client.send_message(
            TARGET_CHANNEL, text,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=kb(rows),
        ))
        return "other"

    # सेव बंद (Restrict saving content) -> डाउनलोड + अपलोड
    if is_protected(msg):
        return await process_protected(client, acc, msg, chat_id)

    # फॉरवर्डिंग ON -> बिना डाउनलोड के रिले
    cap, rows = compose(msg, clean_html(msg.caption.html) if msg.caption else "", 1024)
    kwargs = {}
    if msg.caption or cap:
        kwargs["caption"] = cap
        kwargs["parse_mode"] = enums.ParseMode.HTML
    try:
        rm = await relay_via_bot(acc, tg_id, source, msg.id, kwargs)
    except Exception as e:
        # रिले फेल हुआ (चैनल ने रोका, या बॉट तक नहीं पहुंचा) -> डाउनलोड वाला तरीका
        print("relay fail, download वाला तरीका:", e)
        return await process_protected(client, acc, msg, chat_id)
    return await deliver_relayed(client, rm, rows)


# बॉट की चैट में यूज़र अकाउंट से आई पोस्ट पकड़ता है (बाकी हैंडलर से पहले)
@app.on_message(filters.private & filters.incoming & ~filters.text & ~filters.service, group=-1)
async def relay_handler(client: Client, message: Message):
    u = message.from_user
    if u and u.id in RELAY:
        fut = RELAY[u.id]
        if not fut.done():
            fut.set_result(message)
            raise StopPropagation


# ---------------- कमांड ----------------
@app.on_message(filters.command("start") & filters.private & owner_filter)
async def start_cmd(client: Client, message: Message):
    await message.reply_text(
        "<b>👋 नमस्ते!</b>\n\n"
        "1️⃣ पहले थंबनेल वाली <b>फ़ोटो</b> मुझे भेजिए (सेव हो जाएगी)\n"
        "2️⃣ फिर चैनल का लिंक भेजिए:\n"
        "<code>https://t.me/channelname/123</code>\n"
        "<code>https://t.me/channelname/123-150</code>\n"
        "<code>https://t.me/c/1234567890/45-60</code> (प्राइवेट)\n"
        "या दो लिंक (पहला = शुरू, दूसरा = अंत) अलग-अलग लाइन में\n\n"
        "🎞 वीडियो पर नया थंबनेल लगेगा\n"
        "🖼 फ़ोटो/फ़ाइल/ऑडियो/टेक्स्ट जैसे हैं वैसे ही चैनल में जाएँगे\n"
        "🔒 जिस प्राइवेट चैनल में सेव बंद है, वहाँ से डाउनलोड करके भेजा जाता है\n\n"
        "/bucket - कई लिंक/रेंज जमा करके बारी-बारी चलाएँ (आखिर में end लिखें)\n"
        "/resume - अधूरा बकेट जारी करें\n"
        "/thumb - सेव थंबनेल देखें\n"
        "/delthumb - थंबनेल हटाएँ\n"
        "/cancel - चल रहा टास्क रोकें",
        parse_mode=enums.ParseMode.HTML,
    )


@app.on_message(filters.command("thumb") & filters.private & owner_filter)
async def view_thumb(client: Client, message: Message):
    if not THUMB_ID:
        return await message.reply_text("❌ कोई थंबनेल सेव नहीं है। फ़ोटो भेजिए।")
    await message.reply_photo(THUMB_ID, caption="✅ यह आपका सेव थंबनेल है।")


@app.on_message(filters.command("delthumb") & filters.private & owner_filter)
async def del_thumb(client: Client, message: Message):
    global THUMB_ID
    THUMB_ID = None
    THUMB_CACHE.clear()
    clear_thumb()
    await message.reply_text("🗑 थंबनेल हटा दिया गया।")


@app.on_message(filters.command("cancel") & filters.private & owner_filter)
async def cancel_cmd(client: Client, message: Message):
    uid = message.from_user.id
    if uid in BUCKET_COLLECT:
        BUCKET_COLLECT.pop(uid, None)
        return await message.reply_text("❌ बकेट में जमा किए लिंक हटा दिए गए।")
    if uid in RUNNING:
        CANCEL.add(uid)
        await message.reply_text("❌ टास्क रोका जा रहा है... (चल रही पोस्ट पूरी होते ही रुकेगा)")
    else:
        await message.reply_text("कोई टास्क नहीं चल रहा।")


@app.on_message(filters.command("bucket") & filters.private & owner_filter)
async def bucket_cmd(client: Client, message: Message):
    uid = message.from_user.id
    if uid in RUNNING:
        return await message.reply_text("⚠️ पहले वाला टास्क चल रहा है। रोकने के लिए /cancel")
    BUCKET_COLLECT[uid] = []
    await message.reply_text(
        "🪣 <b>बकेट मोड चालू</b>\n\n"
        "अब सारे लिंक भेजिए (एक लाइन में एक लिंक, कई मैसेज में भी भेज सकते हैं):\n"
        "<code>https://t.me/c/1234567890/10402-10497</code>\n"
        "<code>https://t.me/c/1234567890/14237-14459</code>\n\n"
        "सारे लिंक भेजकर आखिर में <code>end</code> लिखिए। "
        "फिर बॉट एक-एक करके सब रेंज चलाएगा।\n"
        "रद्द करने के लिए /cancel",
        parse_mode=enums.ParseMode.HTML,
    )


@app.on_message(filters.command("resume") & filters.private & owner_filter)
async def resume_cmd(client: Client, message: Message):
    uid = message.from_user.id
    if uid in RUNNING:
        return await message.reply_text("⚠️ पहले वाला टास्क चल रहा है। रोकने के लिए /cancel")
    _, jobs = load_bucket()
    if not jobs:
        return await message.reply_text("कोई अधूरा बकेट नहीं है।")
    if any(j["private"] for j in jobs):
        err = private_error()
        if err:
            return await message.reply_text(err)
    await message.reply_text(f"🪣 अधूरा बकेट जारी: {len(jobs)} रेंज बाकी।")
    await run_bucket(client, message, uid, jobs)


# बॉट को सीधे फ़ोटो भेजेंगे तो वही नया थंबनेल बनेगा
@app.on_message(filters.photo & filters.private & owner_filter)
async def set_thumb(client: Client, message: Message):
    global THUMB_ID
    THUMB_ID = message.photo.file_id
    save_thumb(THUMB_ID)
    THUMB_CACHE.clear()
    await message.reply_text("✅ थंबनेल सेव हो गया। अब चैनल का लिंक भेजिए।")


# ---------------- लिंक पार्स / बकेट के औज़ार ----------------
BUCKET_FILE = "bucket.json"      # अधूरा बकेट यहां सेव रहता है (/resume के लिए)
BUCKET_MAX_RANGE = 5000          # बकेट में हर रेंज की अधिकतम पोस्ट
BUCKET_COLLECT = {}              # uid -> [jobs]  (लिंक जमा हो रहे हैं)
END_WORDS = {"end", "/end", "done", "खत्म", "ख़त्म"}
STAT_KEYS = ("found", "videos", "others", "fallback", "skipped", "deleted", "failed")
ABORT_AFTER = 5                  # लगातार इतने फेच-एरर पर रेंज छोड़ दी जाती है


def parse_jobs(text):
    """टेक्स्ट से सारे लिंक/रेंज निकालता है, उसी क्रम में जिसमें लिखे हैं।"""
    found = []
    for m in PRIV_RE.finditer(text):
        found.append((m.start(), True, m))
    for m in LINK_RE.finditer(text):
        found.append((m.start(), False, m))
    found.sort(key=lambda x: x[0])
    jobs = []
    for _, priv, m in found:
        a = int(m.group("a"))
        b = int(m.group("b") or a)
        if b < a:
            a, b = b, a
        if priv:
            source, key = int("-100" + m.group("chat")), m.group("chat")
        else:
            source = m.group("user")
            key = source.lower()
        jobs.append({"private": priv, "source": source, "key": key,
                     "start": a, "end": b, "next": a})
    return jobs


def _mk(j, a, b):
    d = dict(j)
    d["start"], d["end"], d["next"] = a, b, a
    return d


def dedupe_jobs(jobs):
    """एक ही चैनल की दोहराई गई पोस्ट ID हटाता है। return: (नई jobs, हटी गिनती)"""
    seen, out, removed = {}, [], 0
    for j in jobs:
        s = seen.setdefault((j["private"], j["key"]), set())
        run_start = prev = None
        for i in range(j["start"], j["end"] + 1):
            if i in s:
                removed += 1
                if run_start is not None:
                    out.append(_mk(j, run_start, prev))
                    run_start = None
                continue
            s.add(i)
            if run_start is None:
                run_start = i
            prev = i
        if run_start is not None:
            out.append(_mk(j, run_start, prev))
    return out, removed


def save_bucket(uid, jobs):
    try:
        with open(BUCKET_FILE, "w", encoding="utf-8") as f:
            json.dump({"uid": uid, "jobs": jobs}, f)
    except Exception as e:
        print("bucket save error:", e)


def load_bucket():
    """return: (uid, बाकी jobs)"""
    try:
        with open(BUCKET_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        jobs = [j for j in d.get("jobs", []) if j.get("next", j["start"]) <= j["end"]]
        return d.get("uid"), jobs
    except Exception:
        return None, []


def clear_bucket():
    save_bucket(None, [])


def new_stats():
    d = {k: 0 for k in STAT_KEYS}
    d["last_error"] = ""
    return d


def merge_stats(total, s):
    for k in STAT_KEYS:
        total[k] += s[k]
    if s["last_error"]:
        total["last_error"] = s["last_error"]


def stats_text(s):
    return (
        f"📦 मौजूद पोस्ट मिलीं: {s['found']}\n"
        f"🎞 नए थंबनेल के साथ वीडियो: {s['videos']}\n"
        f"🖼 बाकी: {s['others']}\n"
        f"↩️ पुराने थंबनेल के साथ गए: {s['fallback']}\n"
        f"⏭ सफ़ाई के बाद खाली (छोड़े): {s['skipped']}\n"
        f"🗑 डिलीट पोस्ट (छोड़ी): {s['deleted']}\n"
        f"❌ फेल: {s['failed']}"
    )


def private_error():
    if not user.is_connected or not USER_TG_ID:
        return "❌ यूज़र अकाउंट कनेक्ट नहीं है। SESSION_STRING जांचें।"
    if not BOT_USERNAME:
        return "❌ बॉट का यूज़रनेम नहीं मिला। बॉट रीस्टार्ट कीजिए।"
    return None


async def process_one(client, reader, acc, tg_id, job, msg_id, st, chat_id):
    """एक पोस्ट प्रोसेस करता है और st की गिनती बढ़ाता है।
    return: "found" (पोस्ट मिली) / "gone" (डिलीट/खाली) / "err" (फेच एरर)"""
    source = job["source"]
    try:
        msg = await _retry(lambda: reader.get_messages(source, msg_id))
    except Exception as e:
        print("fetch error:", msg_id, e)
        st["failed"] += 1
        st["last_error"] = f"{type(e).__name__}: {e}"
        return "err"

    # डिलीट हुई / खाली पोस्ट: छोड़ो, अगली पर जाओ
    if not msg or msg.empty or msg.service:
        st["deleted"] += 1
        return "gone"

    st["found"] += 1
    try:
        if job["private"]:
            res = await process_private(client, acc, tg_id, msg, source, chat_id)
        elif msg.video and THUMB_ID:
            if await send_video_with_cover(msg, THUMB_ID):
                res = "video"
            else:
                # कवर फेल हुआ तो वीडियो पुराने थंबनेल के साथ भेज दो
                await plain_copy(client, msg, source)
                res = "fallback"
        else:
            res = "other" if await plain_copy(client, msg, source) else "skipped"
        st[{"video": "videos", "fallback": "fallback", "skipped": "skipped"}.get(res, "others")] += 1
    except Exception as e:
        print("error:", msg_id, e)
        st["failed"] += 1
        st["last_error"] = f"{type(e).__name__}: {e}"
    return "found"


async def run_job(client, message, uid, job, header="", persist=None):
    """एक रेंज चलाता है। persist=(uid, jobs) दें तो हर पोस्ट के बाद प्रगति bucket.json में सेव होती है।
    return: (cancelled, stats)"""
    st = new_stats()
    st["aborted"] = False
    acc = tg_id = None
    if job["private"]:
        acc, tg_id = user, USER_TG_ID
    reader = acc if job["private"] else client
    start_id = job.get("next", job["start"])
    end_id = job["end"]
    prefix = f"{header} " if header else ""
    status = await message.reply_text(f"⏳ {prefix}शुरू कर रहा हूँ...")
    cancelled = False
    errs = 0

    try:
        for msg_id in range(start_id, end_id + 1):
            if uid in CANCEL:
                cancelled = True
                break

            r = await process_one(client, reader, acc, tg_id, job, msg_id, st, message.chat.id)
            job["next"] = msg_id + 1
            if persist:
                save_bucket(*persist)

            if r == "err":
                errs += 1
                if errs >= ABORT_AFTER:
                    st["aborted"] = True
                    job["next"] = end_id + 1
                    if persist:
                        save_bucket(*persist)
                    await message.reply_text(
                        f"⚠️ {prefix}लगातार {ABORT_AFTER} एरर, यह रेंज छोड़ दी।\n"
                        f"आखिरी एरर: {st['last_error'][:300]}\n"
                        "(प्राइवेट चैनल हो तो पक्का करें कि अकाउंट उसका मेंबर है)")
                    break
            else:
                errs = 0

            if r == "found" and st["found"] % 5 == 0:
                try:
                    await status.edit_text(f"⏳ {prefix}ID {msg_id} / {end_id}\n{stats_text(st)}")
                except Exception:
                    pass
            await asyncio.sleep(0.3 if r == "gone" else DELAY)
    finally:
        if tg_id:
            RELAY.pop(tg_id, None)
    return cancelled, st


async def run_bucket(client, message, uid, jobs):
    """बकेट की सारी रेंज बारी-बारी चलाता है।"""
    RUNNING.add(uid)
    CANCEL.discard(uid)
    total = new_stats()
    cancelled = False
    try:
        save_bucket(uid, jobs)
        n = len(jobs)
        for i, job in enumerate(jobs, 1):
            if job.get("next", job["start"]) > job["end"]:
                continue
            header = f"[{i}/{n}] {job['start']}-{job['end']}"
            cancelled, st = await run_job(client, message, uid, job, header, persist=(uid, jobs))
            merge_stats(total, st)
            if cancelled:
                break
            if not st["aborted"]:
                await message.reply_text(
                    f"✅ {header} पूरी\nपोस्ट मिलीं: {st['found']} | फेल: {st['failed']}")
    finally:
        RUNNING.discard(uid)
        CANCEL.discard(uid)

    if cancelled:
        await message.reply_text(
            "❌ बकेट रोका गया। बाकी रेंज सेव हैं, /resume से जारी करें।\n\n" + stats_text(total))
    else:
        clear_bucket()
        text = "🏁 <b>बकेट पूरा हुआ</b>\n\n" + stats_text(total)
        if total["failed"] and total["last_error"]:
            text += f"\n\nआखिरी एरर:\n{total['last_error'][:300]}"
        await message.reply_text(text, parse_mode=enums.ParseMode.HTML)


async def bucket_collect(client, message):
    """/bucket के बाद आए मैसेज: लिंक जमा करता है, 'end' पर बकेट शुरू करता है।"""
    uid = message.from_user.id
    text = (message.text or "").strip()
    jobs = BUCKET_COLLECT[uid]

    if text.lower() in END_WORDS:
        if not jobs:
            BUCKET_COLLECT.pop(uid, None)
            return await message.reply_text("⚠️ कोई लिंक जमा नहीं हुआ। /bucket से दोबारा शुरू कीजिए।")
        if any(j["private"] for j in jobs):
            err = private_error()
            if err:
                return await message.reply_text(err)
        BUCKET_COLLECT.pop(uid, None)
        jobs, removed = dedupe_jobs(jobs)
        if removed:
            await message.reply_text(f"ℹ️ {removed} दोहराई गई पोस्ट ID हटा दीं (एक ही पोस्ट दो बार नहीं जाएगी)।")
        if not THUMB_ID:
            await message.reply_text("ℹ️ थंबनेल सेव नहीं है, इसलिए वीडियो बिना नए थंबनेल के जाएँगे।")
        posts = sum(j["end"] - j["start"] + 1 for j in jobs)
        await message.reply_text(f"🪣 बकेट शुरू: {len(jobs)} रेंज, कुल करीब {posts} पोस्ट।\nरोकने के लिए /cancel")
        return await run_bucket(client, message, uid, jobs)

    new = parse_jobs(text)
    if not new:
        return await message.reply_text(
            "⚠️ इसमें कोई सही लिंक नहीं मिला। लिंक भेजिए, या शुरू करने के लिए end लिखिए।")
    for j in new:
        if j["end"] - j["start"] + 1 > BUCKET_MAX_RANGE:
            j["end"] = j["start"] + BUCKET_MAX_RANGE - 1
    base = len(jobs)
    jobs.extend(new)
    lines = "\n".join(
        f"{base + i}. {j['start']}-{j['end']}" for i, j in enumerate(new[:15], 1))
    more = f"\n… और {len(new) - 15}" if len(new) > 15 else ""
    posts = sum(j["end"] - j["start"] + 1 for j in jobs)
    await message.reply_text(
        f"✅ {len(new)} रेंज जुड़ीं (कुल {len(jobs)} रेंज, करीब {posts} पोस्ट)\n\n{lines}{more}\n\n"
        "और लिंक भेजिए, या शुरू करने के लिए <code>end</code> लिखिए।",
        parse_mode=enums.ParseMode.HTML,
    )


# ---------------- लिंक हैंडलर ----------------
@app.on_message(
    filters.text & filters.private & owner_filter
    & ~filters.command(["start", "thumb", "delthumb", "cancel", "bucket", "resume"])
)
async def link_handler(client: Client, message: Message):
    uid = message.from_user.id

    if uid in BUCKET_COLLECT:              # /bucket चल रहा है तो मैसेज उसी का हिस्सा है
        return await bucket_collect(client, message)

    jobs = parse_jobs(message.text)
    if not jobs:
        return await message.reply_text(
            "⚠️ सही लिंक भेजिए, जैसे:\n<code>https://t.me/channelname/123-150</code>\n\n"
            "कई लिंक एक साथ चलाने के लिए /bucket",
            parse_mode=enums.ParseMode.HTML,
        )

    if uid in RUNNING:
        return await message.reply_text("⚠️ पहले वाला टास्क चल रहा है। रोकने के लिए /cancel")

    job = jobs[0]
    # दो अलग लिंक भेजे हों (पहला = शुरू, दूसरा = अंत) तो भी रेंज मानो
    if len(jobs) >= 2 and jobs[1]["key"] == job["key"] and jobs[1]["private"] == job["private"]:
        job["end"] = jobs[1]["start"]
    if job["end"] < job["start"]:
        job["start"], job["end"] = job["end"], job["start"]
    job["next"] = job["start"]
    if job["end"] - job["start"] + 1 > MAX_RANGE:
        job["end"] = job["start"] + MAX_RANGE - 1
        await message.reply_text(
            f"ℹ️ एक बार में अधिकतम {MAX_RANGE} पोस्ट। ID {job['start']} से {job['end']} तक ले रहा हूँ।\n"
            "बड़ी रेंज के लिए /bucket इस्तेमाल करें।")

    if job["private"]:
        err = private_error()
        if err:
            return await message.reply_text(err)

    if not THUMB_ID:
        await message.reply_text("ℹ️ थंबनेल सेव नहीं है, इसलिए वीडियो बिना नए थंबनेल के जाएँगे।")

    RUNNING.add(uid)
    CANCEL.discard(uid)
    try:
        cancelled, st = await run_job(client, message, uid, job)
    finally:
        RUNNING.discard(uid)
        CANCEL.discard(uid)

    result = (
        f"{'❌ रोका गया' if cancelled else '✅ पूरा हुआ'} (ID {job['start']} से {job['end']} तक)\n"
        + stats_text(st)
    )
    if st["failed"] and st["last_error"]:
        result += f"\n\nआखिरी एरर:\n{st['last_error'][:300]}"
    await message.reply_text(result)


async def main():
    global THUMB_ID, BOT_USERNAME, USER_TG_ID
    os.makedirs(DL_DIR, exist_ok=True)

    await app.start()
    BOT_USERNAME = (await app.get_me()).username or ""
    THUMB_ID = load_thumb()
    print(f"✅ बॉट चालू: @{BOT_USERNAME}")

    try:
        await user.start()
        USER_TG_ID = (await user.get_me()).id
        print(f"✅ यूज़र अकाउंट कनेक्ट: {USER_TG_ID}")
        async for _ in user.get_dialogs():   # प्राइवेट चैनल की peer cache
            pass
        print("✅ डायलॉग लोड हो गए")
    except Exception as e:
        print(f"❌ SESSION_STRING में दिक्कत ({type(e).__name__}): {e}")
        print("   पब्लिक चैनल चलेंगे, प्राइवेट नहीं।")

    try:
        _, pend = load_bucket()
        extra = f"\n\n🪣 अधूरा बकेट मिला: {len(pend)} रेंज बाकी। /resume से जारी करें।" if pend else ""
        await app.send_message(OWNER_IDS[0], "✅ बॉट चालू हो गया। थंबनेल/लिंक भेजें।" + extra)
    except Exception as e:
        print("स्टार्टअप मैसेज नहीं गया (बॉट को /start करें):", e)

    await idle()

    if user.is_connected:
        await user.stop()
    await app.stop()


if __name__ == "__main__":
    app.run(main())

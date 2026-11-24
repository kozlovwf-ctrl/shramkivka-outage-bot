#!/usr/bin/env python3
"""Графік відключень Черкасиобленерго → Telegram.

Запускається GitHub Actions раз на ~10 хв: один прохід і вихід.
Між запусками все пам'ятається у state.json (workflow комітить його назад у репозиторій).

Що робить:
  * закріплене повідомлення «🗓 Графік відключень» (сьогодні й завтра), редагується щоразу;
  * графік змінився або оприлюднили новий → статус перепубліковується знизу + «🗓 Оновлення графіка»;
  * ⏰ нагадування за ~15–20 хв до відключення.

Секрети (змінні середовища, у GitHub — Settings → Secrets → Actions):
  TG_TOKEN — токен бота від @BotFather
  TG_CHAT  — id чату чи каналу (-100…)
  ABON_LS  — особовий рахунок адреси (за ним сайт обленерго віддає чергу й графік)

Локальна перевірка без Telegram:  python bot.py --dry
"""
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    KYIV = ZoneInfo("Europe/Kyiv")
except Exception:                       # Windows без пакета tzdata — лише для локальної перевірки
    KYIV = timezone(timedelta(hours=2))

API = "https://cabinet.cherkasyoblenergo.com/api_new/disconn.php"
ADDRESS = os.environ.get("ADDRESS") or "с. Шрамківка, вул. Поштова, 5"
STATE_FILE = os.environ.get("STATE_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
REMIND_MIN = 20        # cron раз на 10 хв, а GitHub ще й запізнюється, тож вікно з запасом
STALE_H = 3            # сайт мовчить довше — попередження в статусі
DRY = "--dry" in sys.argv

TOKEN = os.environ.get("TG_TOKEN", "")
CHAT = os.environ.get("TG_CHAT", "")
LS = os.environ.get("ABON_LS", "")

MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня",
          "серпня", "вересня", "жовтня", "листопада", "грудня"]
KIND_GPV, KIND_AVAR, KIND_WORKS = "gpv", "avar", "works"
KIND_TITLE = {KIND_AVAR: "⚠️ Аварійне відключення", KIND_WORKS: "🔧 Планові роботи"}


# ---------- Черкасиобленерго ----------

# Сайт обленерго не віддає проміжний сертифікат Sectigo (Windows і браузери докачують його самі,
# Linux на GitHub — ні: CERTIFICATE_VERIFY_FAILED). Тому він лежить поруч (дійсний до 2036).
SSL_CTX = ssl.create_default_context()
try:
    SSL_CTX.load_verify_locations(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sectigo_dv_r36.pem"))
    SSL_CTX.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
except Exception as e:
    print("Сертифікат Sectigo не завантажено:", e)


def api(params):
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40, context=SSL_CTX) as r:
                return json.loads(r.read().decode("utf-8-sig"))
        except Exception as e:          # URL не друкуємо: у ньому особовий рахунок
            err = f"{type(e).__name__}: {getattr(e, 'code', '') or getattr(e, 'reason', '')}"
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(err)


RX = re.compile(r"(\d{1,2}):(\d{2})\s+(\d{1,2})\.(\d{1,2})\.(\d{4})")


def parse_dt(s):
    m = RX.search(s or "")
    if not m:
        return None
    h, mi, d, mo, y = map(int, m.groups())
    return datetime(y, mo, d, h, mi, tzinfo=KYIV)


def merge(iv):
    """Записи обленерго перекриваються (14:00–17:10 і 16:00–20:10) — зливаємо."""
    out = []
    for s, e in sorted(iv):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [tuple(x) for x in out]


def fetch(now):
    d0 = (now - timedelta(days=1)).strftime("%d.%m.%Y")
    d1 = (now + timedelta(days=1)).strftime("%d.%m.%Y")
    base = {"op": "disconn_by_ls", "n_date": d0, "k_date": d1, "abon_ls": LS}
    data = {"queue": "", "avar_queue": "", "done": [], "plan": {KIND_GPV: [], KIND_AVAR: [], KIND_WORKS: []}}
    for sel, kind in (("2", KIND_GPV), ("1", KIND_AVAR), ("0", KIND_WORKS)):
        j = api(dict(base, disconn_selector=sel))
        for q in j.get("DISCONN_QUEUQ") or []:
            info = q.get("QUEUE_INFO", "")
            nums = re.findall(r"\d+", info)
            if "погодинн" in info and nums:
                data["queue"] = ".".join(nums[:2])
            elif "аварійн" in info and nums:
                data["avar_queue"] = nums[0]
        for r in j.get("DISCONNECTIONS") or []:
            s, e = parse_dt(r.get("DATE_START")), parse_dt(r.get("DATE_STOP"))
            if not s or not e or e <= s:
                continue
            st = (r.get("STATE_CHAR") or "").lower()
            if "відмін" in st or "скасов" in st:
                continue
            if "заверш" in st:
                data["done"].append((s, e))          # фактичний час
            else:
                data["plan"][kind].append((s, e))    # очікується / триває
    data["done"] = merge(data["done"])
    for k in data["plan"]:
        data["plan"][k] = merge(data["plan"][k])
    return data


# ---------- Тексти ----------

def hm(t):
    return t.strftime("%H:%M")


def dur(s, e):
    m = int((e - s).total_seconds() // 60)
    h, m = divmod(m, 60)
    return ((f"{h} год " if h else "") + (f"{m} хв" if m or not h else "")).strip()


def day_lines(day, data, now):
    """Відключення через північ — під днем початку. Але сьогоднішній список показує
    і хвіст учорашнього (обрізаний з 00:00), інакше ранкові години без світла зникли б."""
    start = datetime(day.year, day.month, day.day, tzinfo=KYIV)
    clip = day == now.date()
    items = []
    for lst, done in ((data["done"], True), (data["plan"][KIND_GPV], False)):
        for s, e in lst:
            if s.date() == day:
                items.append((s, e, done))
            elif clip and s < start < e:
                items.append((start, e, done))
    lines = []
    for s, e, done in sorted(items):
        span = f"{hm(s)}–{hm(e)}"
        if done or e <= now:
            lines.append(f"<s>{span}</s>")
        elif s <= now:
            lines.append(f"<b>{span}</b> ({dur(s, e)}) ← зараз")
        else:
            lines.append(f"{span} ({dur(s, e)})")
    return lines


def status_body(data, now):
    today, tomorrow = now.date(), (now + timedelta(days=1)).date()
    q = f" · черга {data['queue']}" if data["queue"] else ""
    off_now = any(s <= now < e for s, e in data["plan"][KIND_GPV])
    t = "🗓 <b>Графік відключень</b>\n"
    t += f"📍 {ADDRESS}{q}\n"
    t += ("❌ Зараз за графіком світла немає\n" if off_now else "")
    blk = []
    for label, day in (("Сьогодні", today), ("Завтра", tomorrow)):
        blk.append(f"{label}, {day.day} {MONTHS[day.month - 1]}:")
        ls = day_lines(day, data, now)
        blk += ls or ["відключень поки не заплановано"]
    t += "<blockquote>" + "\n".join(blk) + "</blockquote>"
    for kind in (KIND_AVAR, KIND_WORKS):
        for s, e in data["plan"][kind]:
            if e > now:
                t += f"\n{KIND_TITLE[kind]}: {s.strftime('%d.%m %H:%M')} – {e.strftime('%d.%m %H:%M')}"
    if data["avar_queue"]:
        t += f"\n<i>Аварійна черга {data['avar_queue']}</i>"
    return t


def footer(st, now):
    ok = st.get("last_ok")
    if ok and now - datetime.fromisoformat(ok) > timedelta(hours=STALE_H):
        return f"\n\n⚠️ <i>Сайт обленерго не відповідає з {datetime.fromisoformat(ok).strftime('%H:%M %d.%m')}, графік може бути неактуальним</i>"
    return f"\n\n<i>Оновлено {hm(now)} · дані Черкасиобленерго</i>"


# ---------- Telegram ----------

def tg(method, **p):
    if DRY:
        txt = p.get("text", "")
        print(f"\n[TG {method}] " + (("\n" + txt) if txt else json.dumps(p, ensure_ascii=False)))
        return {"ok": True, "result": {"message_id": 1000}}
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}",
                                 json.dumps(p).encode(), {"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:      # 400/403 — відповідь з описом, повтор не допоможе
            try:
                return json.load(e)
            except Exception:
                return {"ok": False, "description": f"HTTP {e.code}"}
        except Exception as e:
            err = type(e).__name__
            time.sleep(3 * (attempt + 1))
    return {"ok": False, "description": err}


def send(text, silent=False):
    r = tg("sendMessage", chat_id=CHAT, text=text, parse_mode="HTML",
           disable_notification=silent, link_preview_options={"is_disabled": True})
    if not r.get("ok"):
        print("sendMessage:", r.get("description"))
    return r


def post_status(st, text):
    """Новий статус знизу: надіслати → закріпити → прибрати «закріплено» → прибрати старий."""
    r = send(text, silent=True)
    if not r.get("ok"):
        return False
    mid = r["result"]["message_id"]
    tg("pinChatMessage", chat_id=CHAT, message_id=mid, disable_notification=True)
    tg("deleteMessage", chat_id=CHAT, message_id=mid + 1)       # службове «закріплено»
    old = st.get("pin_id")
    if old:
        if not tg("deleteMessage", chat_id=CHAT, message_id=old).get("ok"):   # старше 48 год
            tg("unpinChatMessage", chat_id=CHAT, message_id=old)
            tg("editMessageText", chat_id=CHAT, message_id=old, text="⬇️ Актуальний графік — нижче")
    st["pin_id"] = mid
    return True


def edit_status(st, text):
    if not st.get("pin_id"):
        return post_status(st, text)
    r = tg("editMessageText", chat_id=CHAT, message_id=st["pin_id"], text=text,
           parse_mode="HTML", link_preview_options={"is_disabled": True})
    desc = (r.get("description") or "").lower()
    if r.get("ok") or "not modified" in desc:
        return True
    print("editMessageText:", r.get("description"))
    if "not found" in desc or "can't be edited" in desc:
        return post_status(st, text)
    return False


# ---------- Основний прохід ----------

def key(kind, s, e):
    return f"{kind} {s.isoformat()} {e.isoformat()}"


def main():
    if not LS or (not DRY and not (TOKEN and CHAT)):
        print("Секрети TG_TOKEN / TG_CHAT / ABON_LS ще не задані — пропускаю")   # без помилки: інакше GitHub слав би листи щозапуску
        return
    now = datetime.now(KYIV).replace(second=0, microsecond=0)
    if DRY and os.environ.get("NOW"):     # перевірка: python bot.py --dry з NOW=2026-11-24T20:45
        now = datetime.fromisoformat(os.environ["NOW"]).replace(tzinfo=KYIV)
    try:
        st = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        st = {}
    st["alive"] = now.strftime("%Y-%m")   # раз на місяць коміт: GitHub вимикає cron через 60 днів тиші

    try:
        data = fetch(now)
    except Exception as e:
        print("Обленерго недоступне:", e)
        if st.get("body"):                # лише оновити підпис (попередження про застарілі дані)
            edit_status(st, st["body"] + footer(st, now))
        save(st)
        return
    st["last_ok"] = now.isoformat()

    body = status_body(data, now)
    text = body + footer(st, now)

    # Майбутні відключення (ще не почались) — для порівняння з попереднім запуском
    fut = sorted(key(k, s, e) for k, lst in data["plan"].items() for s, e in lst if s > now)
    prev = [x for x in st.get("future", []) if datetime.fromisoformat(x.split()[1]) > now]
    changed = "future" in st and fut != prev
    st["future"] = fut

    if changed or not st.get("pin_id"):
        ok = post_status(st, text)
        if changed and ok:
            send("🗓 <b>Оновлення графіка відключень</b>\nАктуальний графік — вище ⬆️")
    else:
        edit_status(st, text)
    st["body"] = body

    # ⏰ Нагадування
    night = now.hour >= 23 or now.hour < 7
    reminded = [x for x in st.get("reminded", []) if datetime.fromisoformat(x) > now - timedelta(days=2)]
    for kind, lst in data["plan"].items():
        for s, e in lst:
            left = (s - now).total_seconds() / 60
            if 0 < left <= REMIND_MIN and s.isoformat() not in reminded:
                title = "Відключення за графіком" if kind == KIND_GPV else KIND_TITLE[kind]
                send(f"⏰ <b>{title} о {hm(s)}</b> (за {int(left)} хв)\n"
                     f"Світла не буде до {hm(e)} ({dur(s, e)})", silent=night)
                reminded.append(s.isoformat())
    st["reminded"] = reminded
    save(st)


def save(st):
    if DRY:
        print("\n[state]", json.dumps(st, ensure_ascii=False)[:600])
        if not os.environ.get("STATE_FILE"):
            return
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()

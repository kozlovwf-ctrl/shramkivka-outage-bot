#!/usr/bin/env python3
"""Графік відключень Черкасиобленерго → Telegram.

Запускається GitHub Actions раз на ~10 хв: один прохід і вихід.
Між запусками все пам'ятається у state.json (workflow комітить його назад у репозиторій).

Що робить:
  * закріплене повідомлення «🗓 Графік відключень» (сьогодні й завтра), редагується щоразу;
  * графік змінився або оприлюднили новий → статус перепубліковується знизу + «🗓 Оновлення графіка»;
  * ⏰ нагадування за ~30 хв до відключення.

Секрети (змінні середовища, у GitHub — Settings → Secrets → Actions):
  TG_TOKEN — токен бота від @BotFather
  TG_CHAT  — id чату чи каналу (-100…)
  ABON_LS  — особовий рахунок адреси (за ним сайт обленерго віддає чергу й графік)

Локальна перевірка без Telegram:  python bot.py --dry
"""
import html
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

SITE = "https://cherkasyoblenergo.com"
API = "https://cabinet.cherkasyoblenergo.com/api_new/disconn.php"
ADDRESS = os.environ.get("ADDRESS") or "с. Шрамківка, вул. Поштова, 5"
STATE_FILE = os.environ.get("STATE_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
REMIND_MIN = 35        # ~30 хв: cron раз на 10 хв, тож перший запуск у вікні 25–35 хв до початку
STALE_H = 3            # сайт мовчить довше — попередження в статусі
DRY = "--dry" in sys.argv

TOKEN = os.environ.get("TG_TOKEN", "")
CHAT = os.environ.get("TG_CHAT", "")
LS = os.environ.get("ABON_LS", "")

MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня",
          "серпня", "вересня", "жовтня", "листопада", "грудня"]
WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]
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


def get_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40, context=SSL_CTX) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:
            err = f"{type(e).__name__}: {getattr(e, 'code', '') or getattr(e, 'reason', '')}"
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(err)


TIME_RX = re.compile(r"(\d{1,2})\s*:\s*(\d{2})\s*[-–—]\s*(\d{1,2})\s*:\s*(\d{2})")


def parse_article(page):
    """Новина «Графік погодинних відключень (ГПВ) на 25 листопада»:
    «1.1 08:00 - 10:00, 12:00 - 15:00, 18:00 - 20:00  1.2 …» → {"1.1": [(480, 600), …]} у хвилинах."""
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S | re.I)
    i = t.find("Години відсутності")
    if i < 0:
        return None
    t = t[i:]
    j = t.find("Повернутись до новин")
    t = t[:j] if j > 0 else t[:30000]
    # блочні теги → пробіл, решта → нічого (на сайті «08</strong>:00» розірване тегом)
    t = re.sub(r"<\s*/?\s*(p|br|div|li|tr|td|h\d)\b[^>]*>", " ", t, flags=re.I)
    x = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", t)))
    parts = re.split(r"(?<![\d:.])\b([1-6]\.[12])\b(?![.\d])", x)
    out = {}
    for q, seg in zip(parts[1::2], parts[2::2]):
        out.setdefault(q, [])
        for h1, m1, h2, m2 in TIME_RX.findall(seg):
            out[q].append((int(h1) * 60 + int(m1), int(h2) * 60 + int(m2)))
    return out if len(out) >= 6 else None      # менше черг — значить, формат змінився


def news_schedule(now, queue):
    """{дата: [(start, end)]} для черги з новин обленерго. Список новин — найновіші зверху,
    тож перша знайдена новина на дату (у т.ч. «Оновлено графік…») і є актуальною."""
    page = get_text(SITE + "/media?lang=uk")
    found = {}
    for m in re.finditer(r'href="(/media/[^"]*hpv[^"]*)"', page):
        title = html.unescape(re.sub(r"<[^>]+>", " ", page[m.end():m.end() + 500]))
        mt = re.search(r"\(ГПВ\)\s*на\s*(\d{1,2})\s+([а-яіїє]+)", title)
        if not mt or mt.group(2) not in MONTHS:
            continue
        mo, d = MONTHS.index(mt.group(2)) + 1, int(mt.group(1))
        y = now.year + (1 if mo < now.month - 6 else -1 if mo > now.month + 6 else 0)
        day = datetime(y, mo, d, tzinfo=KYIV)
        if day.date() in found or not (now.date() <= day.date() <= (now + timedelta(days=1)).date()):
            continue
        sched = parse_article(get_text(SITE + m.group(1)))
        if sched is None:
            print("Не розібрано новину", m.group(1))
            continue
        found[day.date()] = merge([(day + timedelta(minutes=a), day + timedelta(minutes=b))
                                   for a, b in sched.get(queue, []) if b > a])
    return found


def fetch(now):
    d0 = (now - timedelta(days=1)).strftime("%d.%m.%Y")
    d1 = (now + timedelta(days=1)).strftime("%d.%m.%Y")
    base = {"op": "disconn_by_ls", "n_date": d0, "k_date": d1, "abon_ls": LS}
    data = {"queue": "", "avar_queue": "", "done": [], "facts": [], "days": set(), "official": False,
            "plan": {KIND_GPV: [], KIND_AVAR: [], KIND_WORKS: []}}
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
                if kind == KIND_GPV:
                    data["facts"].append((s, e, "done"))
            elif "очіку" in st or "заплан" in st:
                data["plan"][kind].append((s, e))
            else:                                    # «Застосовано»: вимкнули, початок фактичний
                data["plan"][kind].append((s, e))
                if kind == KIND_GPV:
                    data["facts"].append((s, e, "active"))
    data["done"] = merge(data["done"])
    for k in data["plan"]:
        data["plan"][k] = merge(data["plan"][k])

    # Записи за особовим рахунком містять і чужу чергу (вулицю живлять дві лінії: 1.1 і 5.1),
    # тому години відключень беремо з офіційного графіка черг у новинах, а записи — лише як факт.
    if data["queue"]:
        try:
            news = news_schedule(now, data["queue"])
            data["plan"][KIND_GPV] = sorted(x for lst in news.values() for x in lst)
            data["days"] = set(news)
            data["official"] = True
        except Exception as e:
            print("Новини обленерго недоступні, графік за особовим рахунком:", e)
    return data


# ---------- Тексти ----------

def hm(t):
    return t.strftime("%H:%M")


def dur(s, e):
    m = int((e - s).total_seconds() // 60)
    h, m = divmod(m, 60)
    return ((f"{h} год " if h else "") + (f"{m} хв" if m or not h else "")).strip()


def fact_for(s, e, facts):
    """Фактичний запис, що відповідає проміжку графіка: перекриття ≥ половини коротшого,
    найближчий за початком. Так запис чужої черги (21:00–23:10 проти 23:00–24:00) не чіпляється."""
    best = None
    for fs, fe, kind in facts:
        ov = (min(e, fe) - max(s, fs)).total_seconds()
        if ov <= 0 or ov < 0.5 * min((e - s), (fe - fs)).total_seconds():
            continue
        if best is None or abs(fs - s) < abs(best[0] - s):
            best = (fs, fe, kind)
    return best


def day_lines(day, data, now):
    if data["official"]:
        if day not in data["days"]:
            return None
        lines = []
        for s, e in data["plan"][KIND_GPV]:
            if s.date() != day:
                continue
            f = fact_for(s, e, data["facts"])
            if f and f[2] == "active" and now < e + timedelta(minutes=30):
                lines.append(slot_line(s, e, now, now_off=True))
            else:
                lines.append(slot_line(s, e, now, done=bool(f and f[2] == "done")))
        return lines
    # Запасний шлях: лише записи за особовим рахунком (злиті, можуть містити чужу чергу).
    # Відключення через північ — під днем початку, а сьогоднішній список показує й хвіст учорашнього.
    start = datetime(day.year, day.month, day.day, tzinfo=KYIV)
    clip = day == now.date()
    items = []
    for lst, done in ((data["done"], True), (data["plan"][KIND_GPV], False)):
        for s, e in lst:
            if s.date() == day:
                items.append((s, e, done))
            elif clip and s < start < e:
                items.append((start, e, done))
    return [slot_line(s, e, now, done=done) for s, e, done in sorted(items)]


def slot_line(s, e, now, done=False, now_off=False):
    """Один проміжок без світла: значок + жирний час; минулий — закреслений."""
    span = f"{hm(s)} – {hm(e)}"
    if now_off or (not done and s <= now < e):
        return f"🔴 <b>{span}</b>  ← ЗАРАЗ"
    if done or e <= now:
        return f"✔️ <s>{span}</s>  (минуло)"
    return f"⚫️ <b>{span}</b>  ({dur(s, e)})"


def day_title(label, day):
    return f"<b>{label}, {WEEKDAYS[day.weekday()]}, {day.day} {MONTHS[day.month - 1]}</b>"


def when(t, now):
    d = t.date()
    if d == now.date():
        return f"сьогодні о {hm(t)}"
    if d == (now + timedelta(days=1)).date():
        return f"завтра о {hm(t)}"
    return f"{t.day} {MONTHS[t.month - 1]} о {hm(t)}"


def status_body(data, now):
    today, tomorrow = now.date(), (now + timedelta(days=1)).date()
    allplan = [x for lst in data["plan"].values() for x in lst]

    # 1. Головне — одним рядком: є світло чи ні і до коли
    active = [f for f in data["facts"] if f[2] == "active" and now < f[1] + timedelta(minutes=30)]
    cur = [(s, e) for s, e in allplan if s <= now < e]
    if active or cur:
        end = max([e for _, e in cur] or [active[0][1]])
        t = "❌ <b>ЗАРАЗ СВІТЛА НЕМАЄ</b>\n"
        if active:
            t += f"Вимкнули о {hm(active[0][0])}\n"
        t += f"За графіком увімкнуть <b>{when(end, now)}</b>\n"
    else:
        t = "💡 <b>Зараз світло є</b>  (за графіком)\n"
        nxt = min((s for s, _ in allplan if s > now), default=None)
        if nxt:
            t += f"Наступне вимкнення: <b>{when(nxt, now)}</b>\n"
        else:
            t += "Вимкнень поки не заплановано\n"

    # 2. Графік по днях
    for label, day in (("Сьогодні", today), ("Завтра", tomorrow)):
        t += f"\n{day_title(label, day)}\n"
        ls = day_lines(day, data, now)
        if ls is None:
            t += "графік ще не оприлюднено\n"
        else:
            t += "\n".join(ls or ["✅ вимкнень немає" if data["official"] else "вимкнень поки не заплановано"]) + "\n"

    for kind in (KIND_AVAR, KIND_WORKS):
        for s, e in data["plan"][kind]:
            if e > now:
                t += f"\n{KIND_TITLE[kind]}:\n<b>{s.strftime('%d.%m %H:%M')} – {e.strftime('%d.%m %H:%M')}</b>\n"
    return t.rstrip()


def footer(st, now):
    ok = st.get("last_ok")
    if ok and now - datetime.fromisoformat(ok) > timedelta(hours=STALE_H):
        return f"\n\n⚠️ <i>Сайт обленерго не відповідає з {datetime.fromisoformat(ok).strftime('%H:%M %d.%m')}, графік може бути неактуальним</i>"
    q = st.get("queue")
    return f"\n\n<i>{ADDRESS}" + (f", черга {q}" if q else "") + f"\nОновлено о {hm(now)}</i>"


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
    st["queue"] = data["queue"]

    body = status_body(data, now)
    text = body + footer(st, now)

    # Майбутні відключення (ще не почались) — для порівняння з попереднім запуском
    fut = sorted(key(k, s, e) for k, lst in data["plan"].items() for s, e in lst if s > now)
    prev = [x for x in st.get("future", []) if datetime.fromisoformat(x.split()[1]) > now]
    changed = "future" in st and fut != prev
    st["future"] = fut

    if changed or not st.get("pin_id") or os.environ.get("REPOST") == "true":
        ok = post_status(st, text)
        if changed and ok:
            send("🗓 <b>Графік змінився</b>\nНовий графік — вище ⬆️")
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
                title = "" if kind == KIND_GPV else f"\n{KIND_TITLE[kind]}"
                send(f"⏰ <b>О {hm(s)} ВИМКНУТЬ СВІТЛО</b>{title}\n"
                     f"Через {int(left)} хв\n"
                     f"Світла не буде до <b>{hm(e)}</b> ({dur(s, e)})", silent=night)
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

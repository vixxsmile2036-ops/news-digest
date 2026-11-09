"""Новостной дайджест по расписанию: RSS -> LLM-сжатие -> Telegram + e-mail.
Все настройки в config.yaml. Запуск: `python digest.py` (все дайджесты на сегодня)
или `python digest.py weekly` (конкретный, независимо от дня)."""
import os, re, sys, time, html, smtplib, calendar
from datetime import datetime
from email.mime.text import MIMEText
from urllib.parse import quote
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

LLM_BASE_URL = os.getenv("LLM_BASE_URL") or "https://api.groq.com/openai/v1"
LLM_API_KEY = os.environ["LLM_API_KEY"]
LLM_MODEL = os.getenv("LLM_MODEL") or "llama-3.3-70b-versatile"

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]

PROMPT = """Ты редактор новостной подборки для Telegram-паблика. Раздел: «{topic}».
Ниже новости за период. Выбери {n} самых важных и интересных, дубликаты объедини.
Язык: {language}. Стиль: {style}.

Формат каждой новости СТРОГО такой, новости разделяй пустой строкой:
{emoji_part}<b>Яркий заголовок</b> — суть в 1-2 предложениях. 🔗 <a href="URL">источник</a>

Правила:
- разрешены только теги <b> и <a>, никакого markdown;
- в качестве текста ссылки используй название источника из списка;
- URL бери ровно из списка, ничего не выдумывай и не добавляй фактов, которых там нет.

Новости:
{items}"""


def clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def fetch_topic(topic, d):
    days = d["period_days"]
    urls = [
        f"https://news.google.com/rss/search?q={quote(q)}+when:{days}d&hl=ru&gl=RU&ceid=RU:ru"
        for q in topic.get("queries", [])
    ] + topic.get("feeds", [])
    cutoff = time.time() - days * 86400
    seen, items = set(), []
    for url in urls:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print("RSS error", url, e)
            continue
        for e in feed.entries:
            ts = calendar.timegm(e.published_parsed) if e.get("published_parsed") else time.time()
            title = clean(e.get("title"))
            key = title.lower()[:60]
            if ts < cutoff or key in seen or not title:
                continue
            seen.add(key)
            source = (e.get("source") or {}).get("title") or feed.feed.get("title", "источник")
            items.append((ts, title, source, e.get("link", ""), clean(e.get("summary"))[:300]))
    items.sort(reverse=True)
    return items[: d["max_items_per_topic"]]


def summarize(topic, items, d):
    lines = []
    for _, title, source, link, desc in items:
        extra = f" | {desc}" if desc and desc.lower() != title.lower() else ""
        lines.append(f"- {title}{extra} | источник: {source} | {link}")
    prompt = PROMPT.format(
        topic=topic["name"], n=d["news_per_topic"], language=d["language"],
        style=d["style"] + (". " + topic["hint"] if topic.get("hint") else ""),
        emoji_part="<эмодзи по смыслу> " if d["emoji_per_news"] else "",
        items="\n".join(lines))
    r = requests.post(
        f"{LLM_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        json={"model": LLM_MODEL, "temperature": 0.4,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def build_blocks(d, now):
    blocks = []
    for topic in d["topics"]:
        items = fetch_topic(topic, d)
        if not items:
            continue
        try:
            body = summarize(topic, items, d)
        except Exception as e:
            print("LLM error", topic["name"], e)
            continue
        blocks.append(f"{topic.get('emoji', '📌')} <b>{html.escape(topic['name'])}</b>\n\n{body}")
    if blocks:
        head = f"📰 <b>{html.escape(d['title'])}</b>"
        if d["show_date"]:
            head += f" — {WEEKDAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]} {now.year}"
        blocks[0] = head + "\n\n" + blocks[0]
    return blocks


def send_telegram(blocks, d):
    token = os.getenv("TG_TOKEN")
    chat = d.get("telegram_chat") or os.getenv("TG_CHAT_ID")
    if not (token and chat):
        return print("Telegram не настроен, пропускаю")
    messages, cur = [], ""
    for b in blocks:  # лимит Telegram 4096 символов, режем по разделам
        if len(cur) + len(b) + 2 > 3900 and cur:
            messages.append(cur.strip())
            cur = ""
        cur += b + "\n\n"
    if cur.strip():
        messages.append(cur.strip())
    for m in messages:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": m, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=30)
        r.raise_for_status()
        time.sleep(1)


def send_email(blocks, d, now):
    host, user, pwd = (os.getenv(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"))
    to = d.get("mail_to") or os.getenv("MAIL_TO")
    if not all((host, user, pwd, to)):
        return print("Почта не настроена, пропускаю")
    body = "<br><br>".join(b.replace("\n", "<br>") for b in blocks)
    msg = MIMEText(
        f"<html><body style='font-family:sans-serif;max-width:640px;line-height:1.5'>{body}</body></html>",
        "html", "utf-8")
    msg["Subject"] = f"{d['title']} — {now.strftime('%d.%m.%Y')}"
    msg["From"], msg["To"] = user, to
    with smtplib.SMTP_SSL(host, int(os.getenv("SMTP_PORT") or "465")) as s:
        s.login(user, pwd)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())


def run_digest(name, d, now):
    print(f"== {name}")
    blocks = build_blocks(d, now)
    if not blocks:
        return print("Нет новостей за период")
    if "telegram" in d["channels"]:
        send_telegram(blocks, d)
    if "email" in d["channels"]:
        send_email(blocks, d, now)


if __name__ == "__main__":
    cfg = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "config.yaml"), encoding="utf-8"))
    defaults = cfg.get("defaults", {})
    only = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
    now = datetime.now(ZoneInfo(defaults.get("timezone", "Asia/Yekaterinburg")))
    for name, raw in cfg["digests"].items():
        d = {**defaults, **raw}
        if only:
            if name != only:
                continue
        elif d.get("days") and DAY_KEYS[now.weekday()] not in d["days"]:
            continue  # сегодня не его день
        run_digest(name, d, now)

#!/usr/bin/env python3
"""Новостной бот: читает RSS-ленты и публикует только НОВЫЕ новости в Telegram-канал
вместе с фото новости.

Формат поста: фото + заголовок + (по желанию) краткий пересказ от Claude + ссылка на источник.
Полные тексты статей не копируются.

Запуск:
  - по расписанию (GitHub Actions / cron): один проход за запуск;
  - постоянно на сервере: задайте LOOP_SECONDS=120, скрипт сам будет опрашивать ленты.

Первый запуск ничего не публикует: он запоминает уже существующие новости,
чтобы канал не залило старыми.
"""
import calendar
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import feedparser
import requests

BASE_DIR = Path(__file__).resolve().parent
SEEN_FILE = BASE_DIR / "seen.json"
SEEN_LIMIT = 3000

CAPTION_LIMIT = 1024          # лимит Telegram на подпись к фото
MAX_IMAGE_BYTES = 10 * 1024 * 1024
UA = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}
IMG_EXT = re.compile(r"\.(jpe?g|png|webp)(\?|$)", re.I)


# ---------- настройки ----------

def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def need(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Не задана переменная окружения {name} (см. .env.example)")
    return value


def env_list(name: str) -> list:
    raw = os.environ.get(name, "")
    return [x.strip() for x in re.split(r"[,\n]", raw) if x.strip()]


# ---------- память о том, что уже отправлено ----------

def load_seen():
    if not SEEN_FILE.exists():
        return None
    try:
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def save_seen(seen: list) -> None:
    SEEN_FILE.write_text(
        json.dumps(seen[-SEEN_LIMIT:], ensure_ascii=False), encoding="utf-8"
    )


# ---------- чтение лент ----------

def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def entry_time(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return calendar.timegm(t) if t else None


def is_image(item: dict) -> bool:
    kind = (item.get("type") or item.get("medium") or "").lower()
    url = item.get("url") or item.get("href") or ""
    return kind.startswith("image") or kind == "image" or bool(IMG_EXT.search(url))


def image_from_entry(entry) -> str | None:
    """Ищем картинку в самой ленте: media:content, media:thumbnail, enclosure, <img> в анонсе."""
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key) or []:
            url = m.get("url")
            if url and (key == "media_thumbnail" or is_image(m)):
                return url
    for link in (entry.get("links") or []) + (entry.get("enclosures") or []):
        if link.get("rel") in (None, "enclosure") and is_image(link):
            url = link.get("href") or link.get("url")
            if url:
                return url
    raw = entry.get("summary", "") or ""
    for content in entry.get("content") or []:
        raw += content.get("value", "")
    m = re.search(r"<img[^>]+src=[\"']([^\"']+)", raw, re.I)
    return m.group(1) if m else None


def image_from_page(url: str) -> str | None:
    """Запасной вариант: картинка из мета-тега og:image на странице новости."""
    try:
        resp = requests.get(url, headers=UA, timeout=10)
        resp.raise_for_status()
        head = resp.text[:200000]
    except Exception:  # noqa: BLE001
        return None
    for pattern in (
        r"<meta[^>]+property=[\"']og:image[\"'][^>]+content=[\"']([^\"']+)",
        r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:image[\"']",
    ):
        m = re.search(pattern, head, re.I)
        if m:
            return html.unescape(m.group(1))
    return None


def collect(feeds: list) -> list:
    items = []
    for url in feeds:
        try:
            feed = feedparser.parse(url)
        except Exception as ex:  # noqa: BLE001
            print(f"Не удалось прочитать {url}: {ex}")
            continue
        if not feed.entries:
            print(f"Лента пуста или недоступна: {url}")
            continue
        source = feed.feed.get("title", url)
        for e in feed.entries:
            link = e.get("link")
            title = (e.get("title") or "").strip()
            if not link or not title:
                continue
            items.append(
                {
                    "id": e.get("id") or link,
                    "title": title,
                    "link": link,
                    "summary": strip_html(e.get("summary", "")),
                    "image": image_from_entry(e),
                    "ts": entry_time(e),
                    "source": source,
                }
            )
    return items


# ---------- пересказ через Claude (необязательно) ----------

def summarize(item: dict) -> str:
    if os.environ.get("SUMMARIZE", "0") != "1" or not item["summary"]:
        return ""
    try:
        import anthropic

        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=os.environ.get("CLAUDE_MODEL") or "claude-sonnet-5-5",
            max_tokens=200,
            system=(
                "Ты редактор новостной ленты. По заголовку и анонсу напиши нейтральный "
                "пересказ в 1-2 предложения на русском своими словами. Используй только "
                "факты из присланного текста, ничего не добавляй и не додумывай. "
                "Без markdown и эмодзи. Верни только пересказ."
            ),
            messages=[
                {
                    "role": "user",
                    "content": f"Заголовок: {item['title']}\nАнонс: {item['summary'][:1500]}",
                }
            ],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as ex:  # noqa: BLE001
        print(f"Пересказ не получился, публикую без него: {ex}")
        return ""


def build_text(item: dict, limit: int) -> str:
    """Заголовок + пересказ + ссылка. Ссылка всегда остаётся целой, обрезается только верх."""
    tail = f"\n\n{item['link']}\nИсточник: {item['source']}"
    head = item["title"]
    summary = summarize(item)
    if summary:
        head += f"\n\n{summary}"
    room = limit - len(tail)
    if len(head) > room:
        head = head[: max(room - 1, 0)].rstrip() + "…"
    return head + tail


# ---------- отправка в Telegram ----------

def tg_call(method: str, **kwargs) -> dict:
    token = need("TELEGRAM_BOT_TOKEN")
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/{method}", timeout=60, **kwargs
    )
    try:
        return resp.json()
    except ValueError:
        return {"ok": False, "description": f"HTTP {resp.status_code}"}


def download_image(url: str) -> bytes | None:
    try:
        resp = requests.get(url, headers=UA, timeout=20, stream=True)
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        if ctype and not ctype.lower().startswith("image"):
            return None
        data = resp.raw.read(MAX_IMAGE_BYTES + 1, decode_content=True)
        if not data or len(data) > MAX_IMAGE_BYTES:
            return None
        return data
    except Exception:  # noqa: BLE001
        return None


def send_news(item: dict) -> None:
    chat_id = need("TELEGRAM_CHAT_ID")
    image = item.get("image")
    if not image and os.environ.get("PHOTO_FROM_PAGE", "1") == "1":
        image = image_from_page(item["link"])

    if image:
        caption = build_text(item, CAPTION_LIMIT)
        # 1) пусть Telegram сам скачает картинку по ссылке
        res = tg_call("sendPhoto", data={"chat_id": chat_id, "photo": image, "caption": caption})
        if res.get("ok"):
            return
        # 2) не вышло - скачиваем сами и загружаем файлом
        blob = download_image(image)
        if blob:
            res = tg_call(
                "sendPhoto",
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": ("news.jpg", blob)},
            )
            if res.get("ok"):
                return
        print(f"Фото не отправилось ({res.get('description')}), публикую без фото")

    res = tg_call("sendMessage", data={"chat_id": chat_id, "text": build_text(item, 4000)})
    if not res.get("ok"):
        raise RuntimeError(f"Telegram: {res.get('description')}")


# ---------- основной проход ----------

def run_once() -> None:
    feeds = env_list("FEEDS")
    if not feeds:
        sys.exit("Не заданы ленты: переменная FEEDS (ссылки на RSS через запятую)")

    max_per_run = int(os.environ.get("MAX_PER_RUN") or "5")
    max_age_h = float(os.environ.get("MAX_AGE_HOURS") or "6")

    seen_loaded = load_seen()
    first_run = seen_loaded is None
    seen = seen_loaded or []
    seen_set = set(seen)

    items = collect(feeds)
    new = [i for i in items if i["id"] not in seen_set]

    if first_run:
        save_seen([i["id"] for i in items])
        print(f"Первый запуск: запомнил {len(items)} существующих новостей, ничего не публикую.")
        return

    cutoff = time.time() - max_age_h * 3600
    fresh = [i for i in new if i["ts"] is None or i["ts"] >= cutoff]
    fresh.sort(key=lambda i: i["ts"] or time.time())
    to_post = fresh[-max_per_run:]
    to_post_ids = {i["id"] for i in to_post}

    # устаревшие и не влезшие в лимит помечаем как просмотренные
    for i in new:
        if i["id"] not in to_post_ids:
            seen.append(i["id"])

    posted = 0
    for item in to_post:
        try:
            send_news(item)
        except Exception as ex:  # noqa: BLE001
            print(f"Не отправлено ({ex}), попробую в следующий раз: {item['title'][:60]}")
            continue
        seen.append(item["id"])
        posted += 1
        time.sleep(3)  # чтобы не упереться в лимиты Telegram

    save_seen(seen)
    print(f"Новых: {len(new)}, опубликовано: {posted}")


def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    loop = int(os.environ.get("LOOP_SECONDS") or "0")
    if not loop:
        run_once()
        return
    while True:
        try:
            run_once()
        except SystemExit:
            raise
        except Exception as ex:  # noqa: BLE001
            print(f"Сбой прохода: {ex}")
        time.sleep(loop)


if __name__ == "__main__":
    main()

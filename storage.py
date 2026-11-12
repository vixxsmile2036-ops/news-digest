"""Хранилище подписчиков: секретный Gist (секреты GIST_ID и GIST_TOKEN)
или, если их нет, локальный файл subscribers.json (для запуска на своём компьютере)."""
import json, os, requests

FILE = "subscribers.json"
LOCAL = os.getenv("SUBSCRIBERS_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), FILE)


def _gist():
    gid, token = os.getenv("GIST_ID"), os.getenv("GIST_TOKEN")
    if gid and token:
        return (f"https://api.github.com/gists/{gid}",
                {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    return None


def configured():
    return _gist() is not None


def _normalize(data):
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("users"), dict):
        data["users"] = {}
    return data


def load():
    g = _gist()
    if g:
        url, headers = g
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        f = r.json().get("files", {}).get(FILE)
        if not f:
            return _normalize({})
        text = f.get("content")
        if f.get("truncated"):
            text = requests.get(f["raw_url"], headers=headers, timeout=30).text
        try:
            return _normalize(json.loads(text or "{}"))
        except ValueError:
            return _normalize({})
    if os.path.exists(LOCAL):
        try:
            return _normalize(json.load(open(LOCAL, encoding="utf-8")))
        except ValueError:
            pass
    return _normalize({})


def save(data):
    text = json.dumps(data, ensure_ascii=False, indent=1)
    g = _gist()
    if g:
        url, headers = g
        r = requests.patch(url, headers=headers, json={"files": {FILE: {"content": text}}}, timeout=30)
        r.raise_for_status()
    else:
        with open(LOCAL, "w", encoding="utf-8") as f:
            f.write(text)

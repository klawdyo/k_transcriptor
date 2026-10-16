"""Searches YouTube through the results page (no API key). Standard library only.

Each result: (video id, duration, channel, title, views). YouTube may auto-translate titles of videos in
other languages, so check the real language by reading the transcript before recommending a video.
"""
import json
import re
import urllib.parse
import urllib.request


def search(query: str, limit: int = 12) -> list[tuple[str, str, str, str, str]]:
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query) + "&hl=pt-BR"
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Accept-Language": "pt-BR,pt;q=0.9", "Cookie": "CONSENT=YES+1"})
    html = urllib.request.urlopen(request, timeout=30).read().decode("utf-8", "ignore")
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if not m:
        raise RuntimeError("O YouTube não devolveu dados de busca (tente de novo mais tarde).")
    found: list[tuple[str, str, str, str, str]] = []

    def walk(node):
        if isinstance(node, dict):
            if "videoRenderer" in node:
                v = node["videoRenderer"]
                title = "".join(r.get("text", "") for r in v.get("title", {}).get("runs", []))
                channel = "".join(r.get("text", "") for r in v.get("ownerText", {}).get("runs", []))
                found.append((v["videoId"], v.get("lengthText", {}).get("simpleText", "?"), channel, title,
                              v.get("viewCountText", {}).get("simpleText", "")))
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(json.loads(m.group(1)))
    return found[:limit]

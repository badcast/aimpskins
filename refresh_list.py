import csv
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

URL = "https://aimp.ru/?do=catalog&os=desktop&id=0&pagesize=99999"
OUT = "aimp_skins.csv"

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def split_version(title: str):
    """'Skin v1.2' -> ('Skin', 'v1.2'); 'Skin 3.2' -> ('Skin', '3.2'); 'Skin' -> ('Skin', '')"""
    m = re.search(r"(?:^|\s)(v\d[\w.\-]*)", title, re.I)
    if m:
        return clean(title.replace(m.group(0), " ", 1)), m.group(1)
    m = re.search(r"\s(\d+(?:\.\d+)+)$", title)
    if m:
        return clean(title[:m.start()]), m.group(1)
    return clean(title), ""


def main():
    resp = requests.get(
        URL,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/124 Safari/537.36"},
        timeout=60,
    )
    resp.raise_for_status()
    resp.encoding = "utf-8"

    soup = BeautifulSoup(resp.text, "html.parser")
    rows = []

    for card in soup.select(".card"):
        h1 = card.select_one("h1")
        dl = card.select_one('.card_buttons a[href*="catalog.download"]')
        if not h1 or not dl:
            continue

        name, version = split_version(clean(h1.get_text()))

        author_el = card.select_one("h2 a")
        author = clean(author_el.get_text()) if author_el else ""

        # fallback: publication date from the card ("gr-e | 2026-07-14 | 5.3 MB | ...")
        dm = DATE_RE.search(card.get_text(" "))
        date = dm.group(0) if dm else ""
        version = version or date

        link = urljoin(URL, dl["href"])
        rows.append([len(rows) + 1, name, version, author, link, "SKIP"])

    with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        w.writerow(["number", "name", "version", "author", "link", "sha256"])
        w.writerows(rows)

    empty = sum(1 for r in rows if not r[2])
    print(f"Done: {len(rows)} rows -> {OUT} (empty version: {empty})")


if __name__ == "__main__":
    main()

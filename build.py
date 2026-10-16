#!/usr/bin/env python3
"""Scrape oldlands.ru region maps, inject cell data into index.html and mirror
the map images into ./img (already-downloaded files are skipped).

    python3 build.py              # data + images
    python3 build.py --no-images  # data only
"""
import json
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = "https://oldlands.ru/modules/api/map.php"
ROOT = Path(__file__).parent
INDEX = ROOT / "index.html"
IMG = ROOT / "img"
SITE = "https://oldlands.ru"
DATA_RE = re.compile(r"(/\*DATA\*/).*?(/\*END\*/)", re.S)
IMG_RE = re.compile(r"/image/map/world/(?:day|night)/(\d+)/(\d+)_(\d+)\.jpg")
SPAN_RE = re.compile(r'<span style="color:\s*#([0-9A-Fa-f]+)[^"]*">(.*?)</(?:span|font)>', re.S)
BR_RE = re.compile(r"<br\s*/?>", re.I)
TAG_RE = re.compile(r"<[^>]+>")


def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def fetch(url):
    return fetch_bytes(url).decode("cp1251", errors="replace")


def local_path(site_path):
    """Must mirror localImg() in index.html."""
    for prefix, local in (("/image/map/world/", "world/"), ("/inc/maps/events_img/", "events/")):
        if site_path.startswith(prefix):
            return IMG / (local + site_path[len(prefix):])
    if site_path == "/image/map/map.jpg":
        return IMG / "map.jpg"
    raise ValueError(site_path)


def download_images(regions, cells):
    paths = {"/image/map/map.jpg"}
    paths |= {c["boss"]["img"] for c in cells if c.get("boss", {}).get("img")}
    for r in regions:
        # one-cell margin also covers the seams the page bridges between regions
        for y in range(r["y0"] - 1, r["y1"] + 2):
            for x in range(r["x0"] - 1, r["x1"] + 2):
                for mode in ("day", "night"):
                    paths.add(f"/image/map/world/{mode}/{y}/{x}_{y}.jpg")
    # paths the server doesn't have (margin cells outside the painted world)
    missing_file = IMG / "missing.txt"
    known_missing = set(missing_file.read_text().split()) if missing_file.exists() else set()
    todo = sorted(p for p in paths if p not in known_missing and not local_path(p).exists())
    print(f"images: {len(paths)} total, {len(todo)} to download", file=sys.stderr)
    stats = {"ok": 0, "missing": 0, "failed": 0}

    def get(path):
        try:
            data = fetch_bytes(SITE + path)
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):  # 403 = whole row directory absent
                stats["missing"] += 1
                known_missing.add(path)
                return
            stats["failed"] += 1
            print(f"  {path}: HTTP {e.code}", file=sys.stderr)
            return
        except OSError as e:
            stats["failed"] += 1
            print(f"  {path}: {e}", file=sys.stderr)
            return
        dest = local_path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
        stats["ok"] += 1
        done = stats["ok"] + stats["missing"] + stats["failed"]
        if done % 250 == 0:
            print(f"  {done}/{len(todo)}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=24) as pool:
        list(pool.map(get, todo))
    IMG.mkdir(exist_ok=True)
    missing_file.write_text("\n".join(sorted(known_missing)) + "\n")
    print(f"images: {stats['ok']} downloaded, {stats['missing']} not on server, {stats['failed']} failed"
          + (" (re-run to retry)" if stats["failed"] else ""), file=sys.stderr)


def clean(s):
    return re.sub(r"\s+", " ", TAG_RE.sub("", s)).strip()


def parse_levels(s):
    m = re.match(r"(.+?)\s+(\d+)(?:-(\d+))?$", s)
    if not m:
        return [s, None, None]
    lo = int(m.group(2))
    return [m.group(1).strip(), lo, int(m.group(3) or lo)]


def parse_cell(td):
    m = IMG_RE.search(td)
    x, y = int(m.group(2)), int(m.group(3))
    cell = {"x": x, "y": y}
    lab = re.search(r'color:#90ee90;">([^<]*)<', td)
    if lab and lab.group(1).strip() not in (f"{x}-{y}", f"{y}-{x}"):
        cell["label"] = lab.group(1).strip()
    boss_img = re.search(r"background-image:url\(([^)]+)\)", td)
    for color, body in SPAN_RE.findall(td):
        color = color.lower()
        parts = [clean(p) for p in BR_RE.split(body)]
        parts = [p for p in parts if p]
        if not parts or color == "90ee90":
            continue
        if color == "c0c0c0":
            cell.setdefault("loc", []).append(", ".join(parts))
        elif color == "88bbdd":
            cell["bots"] = [parse_levels(p) for p in parts if p != "Боты:"]
        elif color == "d60c0c":
            name, lvl = parse_levels(parts[0].replace("ур", "").strip())[:2]
            hp = re.search(r"\[(\d+)/(\d+)\]", body)
            cell["boss"] = {"name": name, "lvl": lvl, "hp": int(hp.group(2)) if hp else None,
                            "img": boss_img.group(1) if boss_img else None}
        elif color.startswith("81f715"):
            m2 = re.search(r"(\d+)", parts[0])
            cell["herbs"] = int(m2.group(1)) if m2 else 0
        elif color == "33ccff":
            cell.setdefault("res", []).extend(parts)
        else:
            cell.setdefault("poi", []).extend(parts)
    return cell


def main():
    overview = fetch(BASE)
    region_ids = [int(r) for r in re.findall(r"\?region=(\d+)", overview)]
    cells, regions = {}, []
    for rid in region_ids:
        html = fetch(f"{BASE}?region={rid}")
        tds = html.split("<td ")[1:]
        coords = [(int(m.group(2)), int(m.group(3))) for m in map(IMG_RE.search, tds) if m]
        if not coords:
            print(f"region {rid}: no tiles", file=sys.stderr)
            continue
        xs, ys = [c[0] for c in coords], [c[1] for c in coords]
        regions.append({"id": rid, "x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys)})
        n = 0
        for td in tds:
            if 'class="helper"' in td and IMG_RE.search(td):
                c = parse_cell(td)
                cells[f"{c['x']}_{c['y']}"] = c
                n += 1
        print(f"region {rid}: {len(coords)} tiles, {n} cells", file=sys.stderr)
    data = {"regions": regions, "cells": list(cells.values())}
    src = INDEX.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    out, n = DATA_RE.subn(lambda m: m.group(1) + payload + m.group(2), src)
    if n != 1:
        sys.exit("index.html: /*DATA*/.../*END*/ marker not found")
    INDEX.write_text(out, encoding="utf-8")
    print(f"wrote {len(cells)} cells, {len(regions)} regions -> {INDEX}", file=sys.stderr)
    if "--no-images" not in sys.argv:
        download_images(regions, data["cells"])


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build a static IIIF Image API 3.0 level 0 site plus Presentation 3.0 manifests.

Usage:
    python scripts/build.py --base-url https://USER.github.io/REPO

Inputs:
    source/                 images named "{identifier} ({image id}).tif",
                            e.g. "1_4_1_1_f (SC01_17_01_002).tif"
    metadata.tsv            one row per identifier: LIST, category, STATE, CITY, EDIFICE, VIEW, identifier
    image-map.tsv           optional overrides: image_id <TAB> identifier, for scans whose
                            filename points at the wrong metadata row

Output (all URLs relative to --base-url):
    iiif/{image id}/info.json                    Image API 3.0 info.json (level0) + tiles/sizes
    manifests/images/{image id}.json             one manifest per image
    manifests/edifices/{group}.json              one manifest per edifice (e.g. 1_4_1_2)
    manifests/categories/{slug}.json             one manifest per category
    manifests/all.json                           every image
    collections/{slug}.json                      per-category Collection of edifice manifests
    collection.json                              top-level Collection
    index.html                                   browsable listing
"""

import argparse
import csv
import html
import json
import math
import re
import shutil
import sys
from collections import OrderedDict
from pathlib import Path

from PIL import Image

Image.MAX_IMAGE_PIXELS = None

TILE = 512
SIZES = [150, 300, 600, 1200]  # widths of pre-rendered full-image sizes
QUALITY = 85
FILENAME_RE = re.compile(r"^(?P<label>.+?)\s*\((?P<id>[^)]+)\)$")
IMAGE_EXTS = {".tif", ".tiff", ".jpg", ".jpeg", ".png"}
VIEWER = "https://projectmirador.org/embed/?iiif-content="
FIELDS = [  # (metadata.tsv column, display label)
    ("category", "Category"),
    ("STATE", "State"),
    ("CITY", "City"),
    ("EDIFICE", "Edifice"),
    ("VIEW", "View"),
    ("identifier", "Identifier"),
    ("LIST", "List"),
]


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr)


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def stem(identifier):
    return re.sub(r"\.(tiff?|jpe?g|png)$", "", identifier.strip(), flags=re.I)


def parse_name(path: Path):
    """'1_4_1_1_f (SC01_17_01_002).tif' -> ('SC01_17_01_002', '1_4_1_1_f')."""
    m = FILENAME_RE.match(path.stem)
    if m:
        return m.group("id").strip(), m.group("label").strip()
    ident = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.stem)
    return ident, path.stem


def read_tsv(path: Path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = []
        for row in csv.DictReader(f, delimiter="\t"):
            row = {(k or "").strip(): (v or "").strip() for k, v in row.items() if k}
            if any(row.values()):
                rows.append(row)
        return rows


# ---------------------------------------------------------------- tiles

def save(img: Image.Image, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=QUALITY, optimize=True, progressive=True)


def scale_factors(w, h):
    sfs, sf = [], 1
    while True:
        sfs.append(sf)
        if math.ceil(w / sf) <= TILE and math.ceil(h / sf) <= TILE:
            return sfs
        sf *= 2


def build_image(src: Path, ident: str, out: Path, service_id: str):
    img = Image.open(src)
    img.load()
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    root = out / "iiif" / ident
    sfs = scale_factors(w, h)

    for sf in sfs:
        sw, sh = math.ceil(w / sf), math.ceil(h / sf)
        scaled = img if sf == 1 else img.resize((sw, sh), Image.LANCZOS)
        span = TILE * sf  # region size in full-resolution pixels
        for y in range(0, h, span):
            for x in range(0, w, span):
                rw, rh = min(span, w - x), min(span, h - y)
                tw, th = math.ceil(rw / sf), math.ceil(rh / sf)  # spec: ceil
                ty, tx = y // sf, x // sf
                tile = scaled.crop((tx, ty, tx + tw, ty + th))
                regions = [f"{x},{y},{rw},{rh}"]
                if x == 0 and y == 0 and rw == w and rh == h:
                    regions.append("full")
                # Some clients (OpenSeadragon) round rather than ceil; cover both.
                variants = {(tw, th), (round(rw / sf), round(rh / sf))}
                for vw, vh in variants:
                    t = tile if (vw, vh) == (tw, th) else tile.resize((vw, vh), Image.LANCZOS)
                    for region in regions:
                        for size in (f"{vw},{vh}", f"{vw},"):
                            save(t, root / region / size / "0" / "default.jpg")

    sizes = []
    for sw in SIZES:
        if sw >= w:
            continue
        sh = round(h * sw / w)
        small = img.resize((sw, sh), Image.LANCZOS)
        for size in (f"{sw},{sh}", f"{sw},"):
            save(small, root / "full" / size / "0" / "default.jpg")
        sizes.append({"width": sw, "height": sh})
    save(img, root / "full" / "max" / "0" / "default.jpg")
    save(img, root / "full" / f"{w},{h}" / "0" / "default.jpg")
    sizes.append({"width": w, "height": h})

    info = {
        "@context": "http://iiif.io/api/image/3/context.json",
        "id": service_id,
        "type": "ImageService3",
        "protocol": "http://iiif.io/api/image",
        "profile": "level0",
        "width": w,
        "height": h,
        "sizes": sizes,
        "tiles": [{"width": TILE, "height": TILE, "scaleFactors": sfs}],
    }
    (root / "info.json").write_text(json.dumps(info, indent=2))
    return w, h, sizes


# ---------------------------------------------------------------- presentation

def lang(value):
    return {"none": [value]}


def metadata_pairs(rec):
    pairs = [{"label": {"en": [label]}, "value": lang(rec["row"][col])}
             for col, label in FIELDS if rec["row"].get(col)]
    pairs.append({"label": {"en": ["Image ID"]}, "value": lang(rec["id"])})
    return pairs


def shared_metadata(recs):
    """Metadata pairs whose value is the same for every record (for group manifests)."""
    out = []
    for col, label in FIELDS:
        if col in ("VIEW", "identifier"):
            continue
        values = {r["row"].get(col, "") for r in recs}
        if len(values) == 1 and (v := values.pop()):
            out.append({"label": {"en": [label]}, "value": lang(v)})
    return out


def thumbnail(rec, min_width=300):
    s = next((s for s in rec["sizes"] if s["width"] >= min_width), rec["sizes"][-1])
    return [{
        "id": f"{rec['service']}/full/{s['width']},{s['height']}/0/default.jpg",
        "type": "Image",
        "format": "image/jpeg",
        "width": s["width"],
        "height": s["height"],
        "service": [{"id": rec["service"], "type": "ImageService3", "profile": "level0"}],
    }]


def canvas(base, rec):
    cid = f"{base}/canvas/{rec['id']}"
    return {
        "id": cid,
        "type": "Canvas",
        "label": lang(rec["view"]),
        "width": rec["width"],
        "height": rec["height"],
        "metadata": metadata_pairs(rec),
        "thumbnail": thumbnail(rec),
        "items": [{
            "id": f"{cid}/page",
            "type": "AnnotationPage",
            "items": [{
                "id": f"{cid}/page/annotation",
                "type": "Annotation",
                "motivation": "painting",
                "target": cid,
                "body": {
                    "id": f"{rec['service']}/full/max/0/default.jpg",
                    "type": "Image",
                    "format": "image/jpeg",
                    "width": rec["width"],
                    "height": rec["height"],
                    "service": [{"id": rec["service"], "type": "ImageService3", "profile": "level0"}],
                },
            }],
        }],
    }


def manifest(base, mid, label, recs, metadata=None, summary=None):
    m = {
        "@context": "http://iiif.io/api/presentation/3/context.json",
        "id": mid,
        "type": "Manifest",
        "label": lang(label),
    }
    if summary:
        m["summary"] = lang(summary)
    if metadata:
        m["metadata"] = metadata
    m["thumbnail"] = thumbnail(recs[0])
    m["items"] = [canvas(base, r) for r in recs]
    return m


def ref(obj):
    """Reference to a Manifest/Collection for use inside a Collection's items."""
    r = {"id": obj["id"], "type": obj["type"], "label": obj["label"]}
    if "thumbnail" in obj:
        r["thumbnail"] = obj["thumbnail"]
    return r


def collection(cid, label, items, summary=None):
    c = {
        "@context": "http://iiif.io/api/presentation/3/context.json",
        "id": cid,
        "type": "Collection",
        "label": lang(label),
    }
    if summary:
        c["summary"] = lang(summary)
    c["items"] = [ref(i) for i in items]
    return c


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- index page

def link(href, text):
    return f"<a href='{html.escape(href)}'>{html.escape(text)}</a>"


def viewer_link(base, rel):
    return link(VIEWER + f"{base}/{rel}", "Mirador")


def write_index(out: Path, base: str, cats, edifice_files, image_files, missing):
    parts = []
    for cat, edifices in cats.items():
        parts.append(
            f"<h2>{html.escape(cat)}</h2><p class='links'>"
            f"{link(f'manifests/categories/{slug(cat)}.json', 'category manifest')} &middot; "
            f"{link(f'collections/{slug(cat)}.json', 'collection')} &middot; "
            f"{viewer_link(base, f'manifests/categories/{slug(cat)}.json')}</p>"
        )
        for key, recs in edifices.items():
            ef = edifice_files[key]
            parts.append(
                f"<section><h3>{html.escape(recs[0]['edifice'])}</h3><p class='links'><code>{html.escape(key)}</code> &middot; "
                f"{link(ef, 'edifice manifest')} &middot; {viewer_link(base, ef)}</p><ul>"
            )
            for r in recs:
                s = r["sizes"][0]
                imf = image_files[r["id"]]
                parts.append(
                    f"<li><img src='iiif/{r['id']}/full/{s['width']},{s['height']}/0/default.jpg' alt='' loading='lazy'>"
                    f"<div><strong>{html.escape(r['view'])}</strong><br>"
                    f"<code>{html.escape(r['key'])}</code> &middot; <code>{html.escape(r['id'])}</code><br>"
                    f"{link(f'iiif/{r['id']}/info.json', 'info.json')} &middot; {link(imf, 'manifest')} &middot; "
                    f"{viewer_link(base, imf)}</div></li>"
                )
            parts.append("</ul></section>")
    if missing:
        parts.append("<h2>Listed in metadata without an image</h2><ul class='plain'>")
        parts += [f"<li><code>{html.escape(k)}</code> {html.escape(v)}</li>" for k, v in missing]
        parts.append("</ul>")

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IIIF Level 0 Images</title>
<style>
body{{font:15px/1.5 system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 16px;color:#222;background:#fff}}
h2{{margin-top:2.5rem;border-bottom:2px solid #222}} h3{{margin-bottom:0}} .links{{margin-top:.25rem}}
ul{{list-style:none;padding:0;display:grid;gap:10px}} ul.plain{{display:block}}
li{{display:flex;gap:12px;align-items:center;border:1px solid #ddd;border-radius:6px;padding:8px}}
ul.plain li{{border:0;padding:2px 0;display:block}}
img{{width:150px;height:auto;flex:none;background:#eee}} code{{font-size:13px}}
</style></head><body>
<h1>IIIF Level 0 Images</h1>
<p>Static IIIF Image API 3.0 (level 0) images with Presentation 3.0 manifests.</p>
<p>{link('collection.json', 'Top-level collection')} &middot; {viewer_link(base, 'collection.json')}<br>
{link('manifests/all.json', 'Manifest of every image')} &middot; {viewer_link(base, 'manifests/all.json')}</p>
{chr(10).join(parts)}
</body></html>
"""
    (out / "index.html").write_text(page)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True, help="public URL of the site root, e.g. https://user.github.io/repo")
    ap.add_argument("--src", default="source", type=Path)
    ap.add_argument("--metadata", default="metadata.tsv", type=Path)
    ap.add_argument("--image-map", default="image-map.tsv", type=Path)
    ap.add_argument("--out", default="_site", type=Path)
    ap.add_argument("--label", default="Sample IIIF Manifest", help="label for manifests/all.json and collection.json")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    rows = OrderedDict((stem(r["identifier"]), r) for r in read_tsv(args.metadata) if r.get("identifier"))
    overrides = {r["image_id"]: stem(r["identifier"]) for r in read_tsv(args.image_map)
                 if r.get("image_id") and r.get("identifier")}
    if not rows:
        warn(f"no metadata rows found in {args.metadata}")

    if args.out.exists():
        shutil.rmtree(args.out)
    args.out.mkdir(parents=True)

    files = sorted(p for p in args.src.iterdir() if p.suffix.lower() in IMAGE_EXTS)
    recs, seen = [], set()
    for f in files:
        ident, file_key = parse_name(f)
        if ident in seen:
            raise SystemExit(f"duplicate image id {ident!r} from {f.name}")
        seen.add(ident)
        key = overrides.get(ident, file_key)
        row = rows.get(key)
        if row is None:
            warn(f"{f.name}: no metadata row for {key!r}")
            row = {}
        service = f"{base}/iiif/{ident}"
        print(f"{ident}  <-  {f.name}" + (f"  (mapped to {key})" if key != file_key else ""))
        w, h, sizes = build_image(f, ident, args.out, service)
        recs.append({
            "id": ident, "key": key, "row": row, "width": w, "height": h, "sizes": sizes, "service": service,
            "edifice": row.get("EDIFICE") or "Unknown edifice",
            "view": row.get("VIEW") or key,
            "category": row.get("category") or "Uncategorized",
            "group": key.rsplit("_", 1)[0] if "_" in key else key,
        })

    # Sort into metadata order (then image id), so groups follow the spreadsheet.
    order = {k: i for i, k in enumerate(rows)}
    recs.sort(key=lambda r: (order.get(r["key"], len(order)), r["key"], r["id"]))
    by_key = {}
    for r in recs:
        by_key.setdefault(r["key"], []).append(r)
    for key, rs in by_key.items():
        if len(rs) > 1:
            warn(f"{key!r} is used by {len(rs)} images: {', '.join(r['id'] for r in rs)} "
                 f"(fix in {args.image_map} if one is mislabeled)")
    missing = [(k, f"{r.get('EDIFICE', '')} — {r.get('VIEW', '')}") for k, r in rows.items() if k not in by_key]
    for k, desc in missing:
        warn(f"metadata row {k!r} has no image ({desc})")

    # Group: category -> edifice group -> records
    cats = OrderedDict()
    for r in recs:
        cats.setdefault(r["category"], OrderedDict()).setdefault(r["group"], []).append(r)

    image_files, edifice_files = {}, {}
    for r in recs:
        rel = f"manifests/images/{r['id']}.json"
        m = manifest(base, f"{base}/{rel}", f"{r['edifice']} — {r['view']}", [r], metadata=metadata_pairs(r))
        write_json(args.out / rel, m)
        image_files[r["id"]] = rel

    cat_manifests, cat_collections = [], []
    for cat, edifices in cats.items():
        edifice_manifests = []
        for key, rs in edifices.items():
            rel = f"manifests/edifices/{key}.json"
            m = manifest(base, f"{base}/{rel}", rs[0]["edifice"], rs, metadata=shared_metadata(rs),
                         summary=f"{len(rs)} image{'s' * (len(rs) != 1)}")
            write_json(args.out / rel, m)
            edifice_files[key] = rel
            edifice_manifests.append(m)

        cat_recs = [r for rs in edifices.values() for r in rs]
        rel = f"manifests/categories/{slug(cat)}.json"
        m = manifest(base, f"{base}/{rel}", cat, cat_recs, metadata=shared_metadata(cat_recs),
                     summary=f"{len(cat_recs)} images of {len(edifices)} edifices")
        write_json(args.out / rel, m)
        cat_manifests.append(m)

        rel = f"collections/{slug(cat)}.json"
        c = collection(f"{base}/{rel}", cat, edifice_manifests, summary=f"{len(edifices)} edifices")
        write_json(args.out / rel, c)
        cat_collections.append(c)

    all_m = manifest(base, f"{base}/manifests/all.json", args.label, recs, summary=f"{len(recs)} images")
    write_json(args.out / "manifests/all.json", all_m)
    write_json(args.out / "collection.json",
               collection(f"{base}/collection.json", args.label, cat_collections + [all_m]))

    write_index(args.out, base, cats, edifice_files, image_files, missing)
    (args.out / ".nojekyll").touch()
    n_ed = sum(len(e) for e in cats.values())
    print(f"Built {len(recs)} images, {len(recs)} image manifests, {n_ed} edifice manifests, "
          f"{len(cats)} category manifests into {args.out}/")


if __name__ == "__main__":
    main()

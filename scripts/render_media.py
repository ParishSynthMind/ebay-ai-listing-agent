"""Render the README images headless with Playwright, from the real run output in examples/local-run/.

    python scripts/render_media.py   (needs: pip install playwright && playwright install chromium)

1. media/listing-preview.png   three drafted listings as a seller would review them: ready, held for review, blocked
2. media/draft-run.png         the dry-run draft of the sample CSV, as printed
3. media/list-reprice.png      list, approve, run again, orders, mark shipped, repricing and the report, as printed
Claude and Gemini were real in that run; eBay was the in-memory sandbox mock (see the README).
"""
import base64
import html
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "examples" / "local-run"
MEDIA = ROOT / "media"
PHOTOS = ROOT / "samples" / "photos"
MONO = "'SF Mono', Menlo, Consolas, monospace"
SANS = "-apple-system, Helvetica, Arial, sans-serif"


def colour(line: str) -> str:
    t = html.escape(line)
    if line.startswith("$"):
        return f"<span style='color:#7ee787'>{t}</span>"
    if "-> published" in line or "-> revised" in line or line.startswith("[ready]") or "applied:" in line \
            or " shipped fulfillment" in line or line.startswith("1 shipped"):
        return f"<span style='color:#3fb950'>{t}</span>"
    if "error:" in line or line.startswith("[needs_fix]") or "skipped" in line:
        return f"<span style='color:#f0883e'>{t}</span>"
    if "note:" in line or "warning:" in line:
        return f"<span style='color:#8b949e'>{t}</span>"
    if "check:" in line or "review:" in line or line.startswith("[needs_review]") or "approve" in line:
        return f"<span style='color:#d29922'>{t}</span>"
    return t


def terminal(lines, title):
    body = "\n".join(colour(x) for x in lines)
    return f"""<html><body style="margin:0;background:#0d1117"><div id="wrap" style="display:inline-block;padding:22px 28px;background:#0d1117">
<div style="font-family:{SANS};color:#8b949e;font-size:13px;margin-bottom:10px">{html.escape(title)}</div>
<pre style="margin:0;font-family:{MONO};font-size:13px;line-height:1.45;color:#c9d1d9">{body}</pre></div></body></html>"""


def section(lines, start, stop=None):
    i = next(n for n, x in enumerate(lines) if x.startswith(start))
    j = next((n for n, x in enumerate(lines) if n > i and stop and x.startswith(stop)), len(lines))
    return lines[i:j]


def shorten(line: str, n: int = 150) -> str:
    return line if len(line) <= n else line[: n - 3] + "..."


PHOTO_FOR = {"nike": "nike_vomero.jpg", "canon": "canon_ae1.jpg", "mug": "white_mug.jpg", "adidas": "adidas_top_ten.jpg"}


def card(d) -> str:
    L = d["listing"]
    key = next(k for k in PHOTO_FOR if k in L["title"].lower())
    img = base64.b64encode((PHOTOS / PHOTO_FOR[key]).read_bytes()).decode()
    rows = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(', '.join(v))}</td></tr>" for k, v in L["aspects"].items())
    checks = "".join(f"<li>{html.escape(c)}</li>" for c in L["ai"]["check"][:4])
    probs = "".join(f"<li>{html.escape(p)}</li>" for p in d["problems"] if p.startswith("error"))
    n_err = len([p for p in d["problems"] if p.startswith("error")])
    badge = {"ready": ("#1a7f37", "Ready to list"),
             "needs_review": ("#9a6700", "Held for a person to review")}.get(d["status"], ("#bc4c00", f"Blocked before eBay: {n_err} error"))
    review = "".join(f"<li>{html.escape(r)}</li>" for r in d.get("review", []))
    return f"""<div class="card">
  <div class="top"><img src="data:image/jpeg;base64,{img}">
  <div class="meta"><div class="badge" style="background:{badge[0]}">{html.escape(badge[1])}</div>
    <div class="title">{html.escape(L['title'])}</div>
    <div class="sub">{len(L['title'])}/80 characters &middot; {html.escape(L['category_path'])} ({L['category_id']})</div>
    <div class="price">US ${L['price']:.2f}</div>
    <div class="sub">{html.escape(L['condition'].replace('_', ' ').title())} &middot; AI range ${L['ai']['price_low']}-{L['ai']['price_high']} &middot; {html.escape(L['ai']['provider'])} {html.escape(L['ai']['model'])}</div>
    <div class="why">{html.escape(L['ai']['price_reasoning'][:230])}</div></div></div>
  <table>{rows}</table>
  {'<div class="h">Stopped by validation</div><ul class="err">' + probs + '</ul>' if probs else ''}
  {'<div class="h">Why it is held</div><ul class="rev">' + review + '</ul>' if review else ''}
  <div class="h">Seller to check</div><ul>{checks}</ul>
  <div class="h">Description (HTML, cleaned)</div><div class="desc">{L['description_html'][:700]}</div>
</div>"""


def preview(drafts) -> str:
    style = f"""body{{margin:0;background:#f3f4f6}} #wrap{{display:inline-flex;gap:22px;padding:24px;font-family:{SANS};background:#f3f4f6}}
.card{{width:500px;background:#fff;border:1px solid #d0d7de;border-radius:10px;padding:18px}}
.top{{display:flex;gap:16px}} .top img{{width:200px;height:170px;object-fit:cover;border-radius:6px;border:1px solid #eee}}
.badge{{display:inline-block;color:#fff;font-size:12px;font-weight:600;padding:3px 9px;border-radius:12px}}
.title{{font-size:17px;font-weight:600;margin:8px 0 4px;color:#111}} .sub{{font-size:12px;color:#57606a}}
.price{{font-size:22px;font-weight:700;margin:8px 0 2px}} .why{{font-size:12px;color:#444;margin-top:6px;font-style:italic}}
table{{border-collapse:collapse;width:100%;margin-top:14px;font-size:13px}} th{{text-align:left;color:#57606a;font-weight:500;width:38%}}
th,td{{border-bottom:1px solid #eaeef2;padding:4px 6px}} .h{{font-size:12px;font-weight:700;text-transform:uppercase;color:#57606a;margin:12px 0 4px}}
ul{{margin:0;padding-left:18px;font-size:12.5px;color:#333}} ul.err{{color:#bc4c00}} ul.rev{{color:#9a6700}} .desc{{font-size:12.5px;color:#333;border-left:3px solid #eaeef2;padding-left:10px}}
.desc h3{{font-size:14px;margin:0 0 4px}}"""
    return f"<html><head><style>{style}</style></head><body><div id='wrap'>{''.join(card(d) for d in drafts)}</div></body></html>"


def main():
    MEDIA.mkdir(exist_ok=True)
    lines = (RUN / "terminal.txt").read_text().splitlines()
    drafts = json.loads((RUN / "drafts-claude.json").read_text())
    pick = [next(d for d in drafts if k in d["listing"]["title"].lower()) for k in ("canon", "mug", "adidas")]
    draft_lines = [shorten(x) for x in section(lines, "$ ebay-lister draft --csv samples", "$ ebay-lister list")]
    rest = section(lines, "$ ebay-lister list --csv samples/items.csv", "$ ebay-lister draft --csv camera-row")
    keep = [shorten(x, 120) for x in rest if x.startswith(("$", "[", "  ->", "  review:", "4 items", "AI:", "eBay:", "12-", "AI-",
                                                           "0 orders", "1 orders", "2 orders", "1 shipped", "1 already", "dry run",
                                                           "applied", "4 SKUs")) or x == ""]
    pages = [
        ("listing-preview.png", preview(pick)),
        ("draft-run.png", terminal(draft_lines, "Dry run on the sample CSV: real Claude Sonnet 5.5 reading the photos; eBay Taxonomy answered by the sandbox mock")),
        ("list-reprice.png", terminal(keep, "List, approve, run again, orders, mark shipped, repricing: real Claude drafts, eBay Inventory / Fulfillment answered by the sandbox mock")),
    ]
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(device_scale_factor=2)
        for name, doc in pages:
            page.set_content(doc)
            page.locator("#wrap").screenshot(path=str(MEDIA / name))
            print("wrote", MEDIA / name)
        b.close()


if __name__ == "__main__":
    main()

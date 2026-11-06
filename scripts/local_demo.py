"""A real run: real Claude (and Gemini for the provider switch) reading the sample photos, with the in-memory
eBay sandbox mock (ebay_lister/mock_ebay.py) standing in for eBay.

Runs the CLI exactly as a user would: setup, category lookup, a dry-run draft of the CSV, a real list run,
the same list run again (nothing duplicated), orders, a repricing dry run and apply, and the report.
Writes what it printed to examples/local-run/. Needs ANTHROPIC_API_KEY (and GEMINI_API_KEY for the switch).
  python scripts/local_demo.py
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from ebay_lister import cli
from ebay_lister.lister import read_csv
from ebay_lister.mock_ebay import FakeEbay

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "examples" / "local-run"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="ebay-demo-"))
    cfg = yaml.safe_load((ROOT / "config" / "lister.yaml").read_text())
    cfg["state_dir"] = str(work / "state")
    cfg["ai"]["cost_cap_usd"] = 0.50
    (work / "lister.yaml").write_text(yaml.safe_dump(cfg))
    fake = FakeEbay()
    fake.refresh_tokens.add("v^1.1#demo-seller-refresh")  # as if the sandbox seller had already consented
    vals = {"client_id": fake.client_id, "client_secret": fake.client_secret, "runame": fake.runame,
            "refresh_token": "v^1.1#demo-seller-refresh"}
    transcript: list[str] = []

    def run(*argv: str, show: str | None = None) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["--config", str(work / "lister.yaml"), *argv], session=fake, secrets=vals.get)
        transcript.append(f"$ ebay-lister {show or ' '.join(argv)}")
        transcript.extend(buf.getvalue().rstrip().splitlines())
        if code:
            transcript.append(f"(exit code {code})")
        transcript.append("")
        print(f"$ ebay-lister {show or ' '.join(argv)}\n{buf.getvalue()}")

    csv_path = ROOT / "samples" / "items.csv"
    run("setup-sandbox")
    run("check")
    run("categories", "35mm film camera")
    run("draft", "--csv", str(csv_path), "--out", str(work / "prev"), show="draft --csv samples/items.csv   (dry run, nothing sent)")
    shutil.copy(work / "prev" / "drafts.json", OUT / "drafts-claude.json")
    run("list", "--csv", str(csv_path), "--out", str(work / "prev"), show="list --csv samples/items.csv")
    run("list", "--csv", str(csv_path), "--out", str(work / "prev"), show="list --csv samples/items.csv   (run again: nothing changed)")

    skus = [i.sku for i in read_csv(csv_path)]
    nike, canon, mug = skus[0], skus[1], skus[2]
    week = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    fake.add_order(mug, "White mug", 12.0, created=week, quantity=2)
    fake.add_order(canon, "Canon AE-1", 150.0, status="FULFILLED", created=week, buyer="testuser_buyer2")
    run("orders")
    run("orders", "--open")

    rules = {"defaults": {"undercut": 0.50, "max_step_pct": 10, "stale_days": 14, "stale_drop_pct": 5, "round_99": True},
             "skus": {nike: {"floor": 60, "ceiling": 160}, canon: {"floor": 80, "ceiling": 250}}}
    (work / "rules.yaml").write_text(yaml.safe_dump(rules))
    (work / "competitors.csv").write_text(f"sku,price\n{nike},71.00\n{nike},79.95\n{canon},40.00\n")
    run("reprice", "--rules", str(work / "rules.yaml"), "--competitors", str(work / "competitors.csv"),
        show="reprice --rules rules.yaml --competitors competitors.csv")
    run("reprice", "--rules", str(work / "rules.yaml"), "--competitors", str(work / "competitors.csv"), "--apply",
        show="reprice --rules rules.yaml --competitors competitors.csv --apply")
    run("report")

    # the provider switch: the same camera row through Gemini, nothing sent
    (work / "one.csv").write_text("\n".join(csv_path.read_text().splitlines()[:1] + [csv_path.read_text().splitlines()[2]]) + "\n")
    shutil.copy(csv_path.parent / "photos" / "canon_ae1.jpg", work / "canon_ae1.jpg")
    (work / "one.csv").write_text((work / "one.csv").read_text().replace("photos/canon_ae1.jpg", "canon_ae1.jpg"))
    run("draft", "--csv", str(work / "one.csv"), "--provider", "gemini", "--out", str(work / "gem"),
        show="draft --csv camera-row.csv --provider gemini")
    shutil.copy(work / "gem" / "drafts.json", OUT / "drafts-gemini.json")

    calls: dict[str, int] = {}
    for m, p in fake.log:
        key = f"{m} {p.split('/v1')[0]}/v1" if "/v1" in p else f"{m} {p}"
        calls[key] = calls.get(key, 0) + 1
    summary = {"ebay_calls_by_api": calls, "listings": {k: {"title": fake.items[v.body["sku"]]["product"]["title"],
                                                              "status": v.status, "listingId": v.listingId,
                                                              "price": v.body["pricingSummary"]["price"]["value"]}
                                                          for k, v in fake.offers.items()},
               "delete_requests": sum(1 for m, _ in fake.log if m == "DELETE")}
    (OUT / "ebay-mock-state.json").write_text(json.dumps(summary, indent=2))
    (OUT / "terminal.txt").write_text("\n".join(transcript) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()

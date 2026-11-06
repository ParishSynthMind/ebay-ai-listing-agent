"""Shared fixtures: the mock eBay sandbox, a seller who has already consented, and a scripted AI."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ebay_lister.cli import build
from ebay_lister.llm import IDENTIFY_SCHEMA, Costs, Drafter
from ebay_lister.mock_ebay import FakeEbay

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"

SHOE = {
    "title": "Nike Air Zoom Pegasus 40 Mens Running Shoes Size 10 Blue White Mesh",
    "condition": "USED_EXCELLENT",
    "condition_description": "Worn twice indoors, soles clean.",
    "aspects": [
        {"name": "Brand", "values": ["Nike"], "source": "photo"},
        {"name": "US Shoe Size", "values": ["10"], "source": "note"},
        {"name": "Department", "values": ["Men"], "source": "note"},
        {"name": "Type", "values": ["Athletic"], "source": "standard"},
        {"name": "Style", "values": ["Running Shoe"], "source": "photo"},
        {"name": "Color", "values": ["Blue", "White"], "source": "photo"},
        {"name": "Model", "values": ["Air Zoom Pegasus 40"], "source": "photo"},
    ],
    "description_html": "<h3>Nike Air Zoom Pegasus 40</h3><p>Men's US 10, worn twice.</p><ul><li>Mesh upper</li></ul>",
    "price": {"value": 62.0, "currency": "USD", "low": 50, "high": 75, "reasoning": "Used Pegasus 40s in this condition sell around 55 to 70."},
    "check": [],
}


def ident(product="running shoes", query="mens running shoes"):
    return {"product": product, "search_query": query, "brand": None, "model": None}


class ScriptedDrafter(Drafter):
    """Returns canned replies in order: (identify reply, draft reply) per item. Records what it was sent."""

    provider = "scripted"

    def __init__(self, replies, cap=1.0):
        super().__init__(Costs(cap, 2.0, 10.0), "scripted-model")
        self.replies = list(replies)
        self.sent = []

    def _call(self, system, text, images, schema):
        self.sent.append({"system": system, "text": text, "images": len(images), "schema": schema})
        kind = "identify" if schema is IDENTIFY_SCHEMA else "draft"
        while self.replies:
            k, reply = self.replies.pop(0)
            if k == kind:
                return reply, 1000, 400
        raise AssertionError(f"no scripted {kind} reply left")


@pytest.fixture
def fake():
    f = FakeEbay()
    f.refresh_tokens.add("v^1.1#seller-refresh")
    return f


@pytest.fixture
def secrets(fake):
    vals = {"client_id": fake.client_id, "client_secret": fake.client_secret, "runame": fake.runame,
            "refresh_token": "v^1.1#seller-refresh"}
    return lambda name: vals.get(name)


@pytest.fixture
def cfg(tmp_path):
    c = yaml.safe_load((ROOT / "config" / "lister.yaml").read_text())
    c["state_dir"] = str(tmp_path / "state")
    return c


@pytest.fixture
def ctx(cfg, fake, secrets):
    return build(cfg, session=fake, secrets=secrets, sleep=lambda s: None)


@pytest.fixture
def ready_ctx(ctx):
    """A seller with business policies and a location already set up."""
    from ebay_lister.lister import setup_sandbox

    setup, _ = setup_sandbox(ctx.account, ctx.inventory, ctx.cfg)
    setup.save(ctx.setup_path)
    return ctx


@pytest.fixture
def shoe_item():
    from ebay_lister.lister import ItemInput

    return ItemInput(sku="SHOE-001", note="nike pegasus 40 mens 10, worn twice", photos=[FIX / "shoe.jpg"],
                     image_urls=["https://i.example.com/shoe1.jpg"], floor=40.0, ceiling=90.0)

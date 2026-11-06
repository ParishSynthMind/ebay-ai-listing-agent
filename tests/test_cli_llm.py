import copy
import json
from types import SimpleNamespace

import pytest
import yaml
from PIL import Image

from ebay_lister import cli
from ebay_lister.llm import (
    DRAFT_SCHEMA,
    IDENTIFY_SCHEMA,
    ClaudeDrafter,
    Costs,
    GeminiDrafter,
    aspect_brief,
    load_image,
    make_drafter,
)
from ebay_lister.mock_ebay import CATEGORIES

from .conftest import FIX, SHOE, ScriptedDrafter, ident


@pytest.fixture
def run(cfg, fake, secrets, tmp_path, monkeypatch, capsys):
    p = tmp_path / "lister.yaml"
    p.write_text(yaml.safe_dump(cfg))
    scripted = {"replies": []}
    monkeypatch.setattr(cli, "make_drafter", lambda *a, **k: ScriptedDrafter(scripted["replies"]))

    def go(*argv, replies=None):
        scripted["replies"] = list(replies or [])
        code = cli.main(["--config", str(p), *argv], session=fake, secrets=secrets)
        return code, capsys.readouterr().out
    return go


def test_check_before_and_after_setup(run):
    code, out = run("check")
    assert code == 0 and "category tree for EBAY_US: 0" in out and "not done (run setup-sandbox)" in out
    run("setup-sandbox")
    code, out = run("check")
    assert "location ai-lister-wh1, 3 policies" in out and "opted in: True" in out


def test_check_without_seller_consent(cfg, fake, tmp_path, capsys):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(cfg))
    vals = {"client_id": fake.client_id, "client_secret": fake.client_secret}
    code = cli.main(["--config", str(p), "check"], session=fake, secrets=vals.get)
    assert code == 1 and "consent-url" in capsys.readouterr().out


def test_consent_and_exchange_from_the_redirect_url(cfg, fake, tmp_path, capsys):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(cfg))
    vals = {"client_id": fake.client_id, "client_secret": fake.client_secret, "runame": fake.runame}
    assert cli.main(["--config", str(p), "consent-url"], session=fake, secrets=vals.get) == 0
    assert "auth.sandbox.ebay.com/oauth2/authorize" in capsys.readouterr().out
    code = fake.issue_code()
    url = "https://signin.sandbox.ebay.com/ws/eBayISAPI.dll?ThirdPartyAuthSucessFailure&isAuthSuccessful=true&code=" + code.replace("#", "%23").replace("^", "%5E") + "&expires_in=299"
    assert cli.main(["--config", str(p), "exchange-code", url], session=fake, secrets=vals.get) == 0
    assert "refresh token saved" in capsys.readouterr().out and len(fake.refresh_tokens) == 2


def test_categories_and_aspects(run):
    code, out = run("categories", "35mm film camera")
    assert "15232" in out and "Cameras & Photo > Film Photography > Film Cameras" in out
    code, out = run("aspects", "15232")
    assert "REQUIRED Brand" in out and "SELECTION_ONLY" in out


def test_draft_is_a_dry_run(run, fake, tmp_path):
    code, out = run("draft", "--photos", str(FIX), "--note", "nike pegasus 10", "--image-url", "https://x/1.jpg",
                    "--out", str(tmp_path / "prev"), replies=[("identify", ident()), ("draft", copy.deepcopy(SHOE))])
    assert code == 0 and "[ready]" in out and "(67/80)" in out
    assert not fake.items and not fake.offers
    saved = json.loads((tmp_path / "prev" / "drafts.json").read_text())
    assert saved[0]["listing"]["aspects"]["Brand"] == ["Nike"]


def test_list_publishes_then_report_and_orders(run, fake):
    run("setup-sandbox")
    code, out = run("list", "--note", "nike pegasus 10", "--image-url", "https://x/1.jpg", "--sku", "S1",
                    replies=[("identify", ident()), ("draft", copy.deepcopy(SHOE))])
    assert "-> published" in out and "eBay:" in out
    fake.add_order("S1", "Nike", 62.0)
    code, out = run("orders", "--open")
    assert "1 orders still to ship" in out and "1 x S1" in out
    code, out = run("report")
    assert "S1" in out and "published" in out


def test_list_no_publish(run, fake):
    run("setup-sandbox")
    code, out = run("list", "--note", "x", "--image-url", "https://x/1.jpg", "--sku", "S1", "--no-publish", "--category", "15709",
                    replies=[("draft", copy.deepcopy(SHOE))])
    assert "-> offer_ready" in out and all(o.status == "UNPUBLISHED" for o in fake.offers.values())


def test_reprice_command(run, tmp_path, fake):
    run("setup-sandbox")
    run("list", "--note", "x", "--image-url", "https://x/1.jpg", "--sku", "S1", "--category", "15709", replies=[("draft", copy.deepcopy(SHOE))])
    rules = tmp_path / "r.yaml"
    rules.write_text(yaml.safe_dump({"skus": {"S1": {"floor": 50}}}))
    comp = tmp_path / "c.csv"
    comp.write_text("sku,price\nS1,58.00\nS1,61\n")
    code, out = run("reprice", "--rules", str(rules), "--competitors", str(comp))
    assert "62.00 -> 57.49" in out or "62.00 -> 56.99" in out
    assert "dry run, nothing sent" in out
    code, out = run("reprice", "--rules", str(rules), "--competitors", str(comp), "--apply")
    assert "applied: 1 price changes" in out


def test_draft_needs_input(run):
    code, _ = run("draft")
    assert code == 2


def test_ebay_error_exit_code(run):
    code, _ = run("aspects", "11450")
    assert code == 1


# --- providers -----------------------------------------------------------------------
class FakeAnthropic:
    def __init__(self, reply, stop="end_turn"):
        self.calls = []
        msgs = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=msgs)
        self.reply, self.stop = reply, stop

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason=self.stop, usage=SimpleNamespace(input_tokens=1200, output_tokens=300),
                               content=[SimpleNamespace(type="text", text=json.dumps(self.reply))])


def test_claude_request_shape():
    c = FakeAnthropic(ident())
    d = ClaudeDrafter(Costs(1, 2, 10), client=c)
    img = load_image(FIX / "shoe.jpg")
    assert d.identify("note", [img]) == ident()
    kw = c.calls[0]
    assert kw["model"] == "claude-sonnet-5-5"
    assert kw["output_config"]["format"]["schema"] is IDENTIFY_SCHEMA
    content = kw["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/jpeg"
    assert content[-1]["type"] == "text" and d.costs.spent == pytest.approx(1200 * 2e-6 + 300 * 1e-5)


@pytest.mark.parametrize("stop,msg", [("refusal", "declined"), ("max_tokens", "cut off")])
def test_claude_refusal_and_cutoff(stop, msg):
    d = ClaudeDrafter(Costs(1, 2, 10), client=FakeAnthropic({}, stop))
    with pytest.raises(RuntimeError, match=msg):
        d.identify("x", [])


class FakeGenai:
    def __init__(self, reply, finish="STOP"):
        self.calls = []
        self.models = SimpleNamespace(generate_content=self._gen)
        self.reply, self.finish = reply, finish

    def _gen(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(text=json.dumps(self.reply), candidates=[SimpleNamespace(finish_reason=self.finish)],
                               usage_metadata=SimpleNamespace(prompt_token_count=900, candidates_token_count=200, thoughts_token_count=50))


def test_gemini_request_shape():
    g = FakeGenai(SHOE)
    d = GeminiDrafter(Costs(1, 0.3, 2.5), client=g)
    out = d.draft("note", [load_image(FIX / "shoe.jpg")], {"categoryId": "15709", "path": "Athletic Shoes"}, CATEGORIES["15709"]["aspects"])
    assert out["title"] == SHOE["title"]
    kw = g.calls[0]
    assert kw["config"]["response_json_schema"] is DRAFT_SCHEMA and kw["config"]["response_mime_type"] == "application/json"
    assert d.costs.output_tokens == 250  # thinking tokens are billed as output


def test_gemini_safety_block():
    d = GeminiDrafter(Costs(1, 0.3, 2.5), client=FakeGenai({}, "FinishReason.SAFETY"))
    with pytest.raises(RuntimeError, match="declined"):
        d.identify("x", [])


def test_make_drafter_switch():
    assert make_drafter("claude", 1, client=FakeAnthropic({})).provider == "claude"
    g = make_drafter("gemini", 1, client=FakeGenai({}))
    assert g.provider == "gemini" and g.model == "gemini-3.5-flash-lite" and g.costs.price_in == 0.30
    with pytest.raises(ValueError):
        make_drafter("openai", 1)


def test_load_image_shrinks_big_photos(tmp_path):
    p = tmp_path / "big.png"
    Image.new("RGBA", (4000, 3000), (10, 20, 30, 255)).save(p)
    mime, data = load_image(p)
    from io import BytesIO
    assert mime == "image/jpeg" and max(Image.open(BytesIO(data)).size) == 1568


def test_aspect_brief_lists_required_first_with_closed_values():
    b = aspect_brief(CATEGORIES["15709"]["aspects"]).splitlines()
    assert b[0].startswith("- Brand [REQUIRED")
    assert any("Department [REQUIRED; one value" in x and "ONLY these values: Men, Women, Unisex Adult" in x for x in b)
    assert b[-1].startswith("- Shoe Width [optional")

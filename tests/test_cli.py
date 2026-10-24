import csv
import io
import json

import pytest

from furima_finder import cli
from furima_finder.http import FetchError

from .conftest import FakeSource, make_listing


def sold_history():
    return [make_listing("Nintendo Switch 有機EL", 29000 + (i % 3) * 1000, sold=True) for i in range(12)]


def fake_sources(on_sale=None):
    on_sale = on_sale if on_sale is not None else [
        make_listing("Switch 有機EL ホワイト 美品", 21000, id="m1"),
        make_listing("Switch 有機EL <ネオン>", 25000, id="m2", thumbnail="https://example.com/a.jpg"),
    ]
    return {"mercari": FakeSource(sold=sold_history(), on_sale=on_sale)}


def run(argv, sources=None, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdout=out, stderr=err, sources=sources or fake_sources(), **kwargs)
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize(
    "value, expected",
    [("15", 0.15), ("0.15", 0.15), ("15%", 0.15), (0.2, 0.2), (20, 0.2), ("1", 0.01), ("0", 0.0)],
)
def test_parse_ratio(value, expected):
    assert cli.parse_ratio(value) == pytest.approx(expected)


@pytest.mark.parametrize("value", ["abc", "150", "-5", True])
def test_parse_ratio_rejects(value):
    with pytest.raises(ValueError):
        cli.parse_ratio(value)


def test_search_prints_ranked_recommendations():
    code, out, err = run(["Switch 有機EL", "--no-color"])
    assert code == 0
    assert "■「Switch 有機EL」" in out
    assert "相場 ¥30,000（売り切れ 12件の中央値" in out
    assert "販売中 2件を確認 → おすすめ 2件" in out
    assert out.index("Switch 有機EL ホワイト 美品") < out.index("Switch 有機EL <ネオン>")
    assert " 1. " in out and "相場比 -30%" in out
    assert "https://jp.mercari.com/item/m1" in out
    assert "\033[" not in out
    assert "売り切れ価格を取得中" in err


def test_outputs_html_csv_and_json(tmp_path):
    html_path, csv_path, json_path = tmp_path / "r.html", tmp_path / "r.csv", tmp_path / "r.json"
    code, _, err = run(["Switch 有機EL", "--html", str(html_path), "--csv", str(csv_path), "--json", str(json_path), "--resale"])
    assert code == 0

    page = html_path.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    assert "Switch 有機EL &lt;ネオン&gt;" in page  # エスケープされている
    assert 'src="https://example.com/a.jpg"' in page
    assert page.count('<article class="card"') == 2
    assert "転売の目安" in page
    assert "HTMLレポートを保存しました" in err

    raw = csv_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # Excel 用の BOM
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    assert rows[0][:3] == ["キーワード", "順位", "スコア"]
    assert rows[1][4] == "Switch 有機EL ホワイト 美品"
    assert len(rows) == 3

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data[0]["keyword"] == "Switch 有機EL"
    assert data[0]["market"]["overall"]["median"] == 30000
    assert data[0]["recommendations"][0]["profit"] == 30000 * 0.9 - 750 - 21000


def test_no_results_message():
    _, out, _ = run(["Switch 有機EL"], sources=fake_sources(on_sale=[]))
    assert "条件に合うおすすめはありませんでした" in out


def test_total_failure_exits_with_1():
    broken = {"mercari": FakeSource(error=FetchError("api.mercari.jp から取得できませんでした（HTTP 403: …）"))}
    code, out, err = run(["Switch"], sources=broken)
    assert code == 1
    assert "取得エラー: メルカリ（売り切れ）" in out
    assert "出品を取得できませんでした" in out
    assert "取得に失敗した検索があります" in err


def test_requires_keyword_or_config(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([], sources=fake_sources())
    assert exc.value.code == 2
    assert "検索キーワードか --config" in capsys.readouterr().err


def test_invalid_option_value(capsys):
    with pytest.raises(SystemExit):
        cli.main(["Switch", "--min-discount", "abc"], sources=fake_sources())
    assert "0.15・15・15% のような形で指定してください" in capsys.readouterr().err


CONFIG = """
[defaults]
min_discount = "20%"
exclude = ["ジャンク"]
details = 0

[[search]]
keyword = "Switch 有機EL"
max-price = 26000
exclude = "箱のみ 空箱"

[[search]]
keyword = "iPad mini 6"
condition = "good"
sources = "mercari"
min_discount = 0.3
"""


def test_config_file_precedence(tmp_path):
    path = tmp_path / "watch.toml"
    path.write_text(CONFIG, encoding="utf-8")
    args = cli.build_parser().parse_args(["--config", str(path), "PS5", "--min-match", "0.8"])
    plans = cli.build_plans(args)
    assert [plan.keyword for plan in plans] == ["PS5", "Switch 有機EL", "iPad mini 6"]
    ps5, switch, ipad = plans
    assert ps5.settings.min_discount == pytest.approx(0.2)
    assert ps5.details == 0 and ps5.pages == 2
    assert switch.max_price == 26000
    assert switch.exclude == ["ジャンク", "箱のみ", "空箱"]
    assert ipad.settings.min_discount == pytest.approx(0.3)
    assert ipad.condition == "good" and ipad.settings.condition_filtered
    assert all(plan.settings.min_match == 0.8 for plan in plans)  # コマンドラインが最優先


@pytest.mark.parametrize(
    "content, message",
    [
        ('[[search]]\nkeyword = "a"\nunknown = 1\n', "不明な項目「unknown」"),
        ('[[search]]\nmax_price = 100\n', "keyword がありません"),
        ('[[search]]\nkeyword = "a"\npages = 0\n', "pages の値が正しくありません"),
        ('[[search]]\nkeyword = "a"\nsources = "yahoo"\n', "sources の値が正しくありません"),
        ('[defaults]\nkeyword = "a"\n', "[defaults] には keyword を書けません"),
        ("[[search]\n", "書式が正しくありません"),
        ('[other]\nx = 1\n', "不明な項目があります: other"),
    ],
)
def test_config_errors(tmp_path, content, message):
    path = tmp_path / "bad.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(cli.ConfigError, match=message.replace("[", r"\[")):
        cli.load_config(path)


def test_example_config_is_valid():
    from pathlib import Path

    defaults, searches = cli.load_config(Path(__file__).parent.parent / "watchlist.example.toml")
    assert searches and all("keyword" in entry for entry in searches)


def test_watch_shows_only_new_listings_and_notifies(monkeypatch):
    sources = fake_sources()
    source = sources["mercari"]
    sent = []
    monkeypatch.setattr(cli, "send_webhook", lambda url, message: sent.append((url, message)))
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            source.on_sale.append(make_listing("Switch 有機EL 新着", 19000, id="m3"))
        else:
            raise KeyboardInterrupt

    code, out, err = run(
        ["Switch 有機EL", "--watch", "1", "--webhook", "https://discord.com/api/webhooks/x"], sources=sources, sleep=fake_sleep
    )
    assert code == 0
    assert sleeps == [300.0, 300.0]  # 最短5分
    assert "最短5分" in err and "監視を終了しました" in err
    first, second = out.split("■ 新着「Switch 有機EL」")
    assert "Switch 有機EL ホワイト 美品" in first
    assert "Switch 有機EL 新着" in second and "ホワイト" not in second
    assert len(sent) == 2
    assert sent[0][1].startswith("【フリマおすすめ】Switch 有機EL")
    assert sent[1][1].startswith("【フリマ新着おすすめ】Switch 有機EL")
    assert "m3" in sent[1][1] and "m1" not in sent[1][1]


def test_watch_reports_errors_in_later_rounds():
    sources = fake_sources()
    source = sources["mercari"]
    calls = []

    def fake_sleep(seconds):
        calls.append(seconds)
        if len(calls) == 1:
            source.error = FetchError("api.mercari.jp から取得できませんでした（HTTP 503: …）")
        else:
            raise KeyboardInterrupt

    code, _, err = run(["Switch 有機EL", "--watch", "10"], sources=sources, sleep=fake_sleep)
    assert code == 0
    assert "取得エラー（Switch 有機EL）: メルカリ（売り切れ）" in err
    assert "新着の候補はありませんでした" in err


def test_ctrl_c_during_search_exits_quietly():
    class Interrupting(FakeSource):
        def search(self, query):
            raise KeyboardInterrupt

    code, _, err = run(["Switch"], sources={"mercari": Interrupting()})
    assert code == 130
    assert "中断しました" in err

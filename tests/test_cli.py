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
    code, _, err = run(["Switch 有機EL", "--html", str(html_path), "--csv", str(csv_path), "--json", str(json_path)])
    assert code == 0

    page = html_path.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    assert "Switch 有機EL &lt;ネオン&gt;" in page  # エスケープされている
    assert 'src="https://example.com/a.jpg"' in page
    assert page.count('<article class="card"') == 2
    assert "古物商" not in page
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
    assert data[0]["recommendations"][0]["profit"] is None


def test_resale_mode_ranks_by_profit(tmp_path):
    html_path, csv_path, json_path = tmp_path / "r.html", tmp_path / "r.csv", tmp_path / "r.json"
    code, out, _ = run(
        ["Switch 有機EL", "--resale", "--no-color", "--html", str(html_path), "--csv", str(csv_path), "--json", str(json_path)]
    )
    assert code == 0
    # 売値の目安 30,000円 × 0.9 − 送料 750円 − 仕入れ 21,000円
    assert "仕入れ ¥21,000 → 利益 ¥5,250（利益率 25%）" in out
    assert "直近30日で12件売れている" in out
    assert "利益率が低い 1件" in out  # 25,000円の出品は利益率5%

    page = html_path.read_text(encoding="utf-8")
    assert '<span class="badge profit">利益 ¥5,250</span>' in page
    assert '<option value="profit">利益が大きい順</option>' in page
    assert '<option value="long">値上がり傾向順</option>' in page
    assert "古物商許可" in page

    rows = list(csv.reader(io.StringIO(csv_path.read_bytes().decode("utf-8-sig"))))
    header = rows[0]
    assert rows[1][header.index("利益")] == "5250"
    assert rows[1][header.index("利益率(%)")] == "25.0"
    assert rows[1][header.index("直近30日の販売数")] == "12"
    assert rows[1][header.index("長期の値動き(%)")] == "" and rows[1][header.index("年代")] == ""

    recommendation = json.loads(json_path.read_text(encoding="utf-8"))[0]["recommendations"][0]
    assert (recommendation["expected_sale"], recommendation["profit"], recommendation["roi"]) == (30000, 5250, 0.25)


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
        ('[[search]]\nkeyword = "a"\nsources = "amazon"\n', "sources の値が正しくありません"),
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


@pytest.mark.parametrize("name", ["watchlist.example.toml", "watchlist.pokeca.example.toml", "pokeca.toml"])
def test_example_configs_are_valid(name):
    from pathlib import Path

    path = Path(__file__).parent.parent / name
    args = cli.build_parser().parse_args(["--config", str(path)])
    plans = cli.build_plans(args)
    assert plans and all(plan.keyword for plan in plans)


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


def plans_for(argv, tmp_path=None, config=None):
    if config is not None:
        path = tmp_path / "c.toml"
        path.write_text(config, encoding="utf-8")
        argv = [*argv, "--config", str(path)]
    return cli.build_plans(cli.build_parser().parse_args(argv))


def test_pokeca_profile_defaults_and_overrides():
    (plan,) = plans_for(["リザードンex SAR", "--profile", "pokeca"])
    assert plan.profile == "pokeca"
    assert plan.settings.resale is True
    assert (plan.settings.min_profit, plan.settings.resale_shipping, plan.settings.buyer_shipping) == (1000, 230, 300)
    (plan,) = plans_for(["リザードンex SAR", "--profile", "ポケカ", "--no-resale", "--min-profit", "2000"])
    assert plan.profile == "pokeca"
    assert plan.settings.resale is False and plan.settings.min_profit == 2000
    (plan,) = plans_for(["Switch"])
    assert plan.profile == "default" and plan.settings.resale is False and plan.settings.min_profit == 500


def test_profile_from_config_file(tmp_path):
    config = '[defaults]\nprofile = "pokeca"\nmin_profit = 1500\n\n[[search]]\nkeyword = "ポケモンカード SAR"\nscan = true\nscan_cards = 30\n'
    (plan,) = plans_for([], tmp_path, config)
    assert (plan.profile, plan.scan, plan.scan_cards) == ("pokeca", True, 30)
    assert plan.settings.min_profit == 1500  # [defaults] はプロファイルの既定値より優先


def test_scan_requires_a_card_profile(capsys):
    with pytest.raises(SystemExit):
        cli.main(["ポケモンカード SAR", "--scan"], sources=fake_sources())
    assert "--profile pokeca" in capsys.readouterr().err


def test_unknown_profile_is_rejected(capsys):
    with pytest.raises(SystemExit):
        cli.main(["x", "--profile", "yugioh"], sources=fake_sources())
    assert "default / pokeca のどれかで指定してください" in capsys.readouterr().err


def test_scan_end_to_end_output(tmp_path):
    from datetime import timedelta

    from .conftest import make_listing as listing

    on_sale = [listing("リザードンex SAR 201/165", 6000, id="m9", condition=2, age=timedelta(hours=1))]
    sold = [listing(f"リザードンex SAR 201/165 {i}", 10000, sold=True, condition=2) for i in range(8)]
    sources = {"mercari": FakeSource(sold=sold, on_sale=on_sale)}
    code, out, err = run(["ポケモンカード SAR", "--profile", "pokeca", "--scan", "--no-color", "--details", "0"], sources=sources)
    assert code == 0
    assert "■「ポケモンカード SAR」のスキャン" in out
    assert "カード番号ごと（旧裏はカード名ごと）に売り切れ価格を調べ" in out
    assert "仕入れ ¥6,000 → 利益 ¥2,770（利益率 46%）" in out
    assert "売値の目安 ¥10,000（未鑑定の相場・直近14日の中央値）" in out
    assert "[1/1] 201/165 の売り切れ価格を取得中" in err



def test_old_card_output_shows_era_and_long_term_trend(tmp_path):
    from datetime import timedelta

    from .conftest import NOW
    from .conftest import make_listing as listing

    on_sale = [listing("旧裏 リザードン LV.76", 6000, id="m9", condition=3, age=timedelta(hours=1))]
    sold = [
        listing(f"旧裏 リザードン {i}", price, sold=True, condition=3, updated_at=NOW - timedelta(days=days))
        for i, (price, days) in enumerate([(10000, 2), (10000, 5), (10000, 9), (10000, 20)] + [(8000, 120)] * 4)
    ]
    sources = {"mercari": FakeSource(sold=sold, on_sale=on_sale)}
    html_path, csv_path, json_path = tmp_path / "r.html", tmp_path / "r.csv", tmp_path / "r.json"
    code, out, _ = run(
        ["旧裏 リザードン", "--profile", "pokeca", "--no-color", "--details", "0",
         "--html", str(html_path), "--csv", str(csv_path), "--json", str(json_path)],
        sources=sources,
    )
    assert code == 0
    assert "年代の目安: 旧裏（1996〜2001年）" in out
    assert "3か月〜1年前比 +25%" in out
    assert "長い目で見て値上がりしている（3か月〜1年前の相場 ¥8,000 から +25%）" in out
    assert "古い年代のカード（旧裏・1996〜2001年）" in out

    page = html_path.read_text(encoding="utf-8")
    assert '<span class="chip">旧裏（1996〜2001年）</span>' in page
    assert '<span class="chip">値上がり傾向</span>' in page
    assert 'data-long="0.2500"' in page

    rows = list(csv.reader(io.StringIO(csv_path.read_bytes().decode("utf-8-sig"))))
    header = rows[0]
    assert rows[1][header.index("年代")] == "旧裏"
    assert rows[1][header.index("長期の値動き(%)")] == "25.0"

    data = json.loads(json_path.read_text(encoding="utf-8"))[0]
    assert data["market"]["era"] == "旧裏"
    assert data["recommendations"][0]["era"] == "旧裏"
    assert data["recommendations"][0]["long_trend"] == 0.25


def test_open_shows_the_report_in_the_browser_once(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    html_path = tmp_path / "r.html"
    code, _, _ = run(["Switch 有機EL", "--html", str(html_path), "--open"])
    assert code == 0
    assert opened == [html_path.resolve().as_uri()]

    opened.clear()
    calls = []

    def fake_sleep(seconds):
        calls.append(seconds)
        if len(calls) == 2:
            raise KeyboardInterrupt

    run(["Switch 有機EL", "--html", str(html_path), "--open", "--watch", "15"], sleep=fake_sleep)
    assert opened == [html_path.resolve().as_uri()]  # 監視中は最初の1回だけ


def test_open_without_html_uses_default_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    run(["Switch 有機EL", "--open"])
    assert (tmp_path / "report.html").exists()
    assert opened == [(tmp_path / "report.html").as_uri()]


def test_config_with_bom_and_wrong_encoding(tmp_path):
    bom = tmp_path / "bom.toml"
    bom.write_bytes('[[search]]\nkeyword = "ポケモンカード SAR"\n'.encode("utf-8-sig"))
    assert cli.load_config(bom)[1][0]["keyword"] == "ポケモンカード SAR"
    sjis = tmp_path / "sjis.toml"
    sjis.write_bytes('[[search]]\nkeyword = "ポケモンカード"\n'.encode("cp932"))
    with pytest.raises(cli.ConfigError, match="UTF-8 で保存し直して"):
        cli.load_config(sjis)


@pytest.mark.parametrize("name", ["pokeca_search.bat", "pokeca_watch.bat", "check_sites.bat"])
def test_windows_batch_files(name):
    from pathlib import Path

    raw = (Path(__file__).parent.parent / name).read_bytes()
    raw.decode("ascii")  # 文字コードの違いで壊れないよう ASCII だけで書く
    assert raw.count(b"\r\n") == raw.count(b"\n")  # 改行は CRLF
    text = raw.decode("ascii")
    assert 'cd /d "%~dp0"' in text  # ダブルクリックしても自分のフォルダで動く
    assert ("--config pokeca.toml" in text and "--open" in text) or "--probe" in text

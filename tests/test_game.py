from datetime import timedelta

import pytest

from furima_finder import cli, text
from furima_finder.eras import GAME_ERAS, detect_game_era
from furima_finder.finder import SearchPlan, run_scan, run_search
from furima_finder.profiles import GAME, game_group, game_model_key, game_shipping
from furima_finder.scoring import Settings

from .conftest import FakeSource, make_listing

CONSOLE_QUERY = text.tokens("Nintendo Switch 本体 PS5 本体")
SETTINGS = Settings(resale=True, resale_shipping=230, min_profit=1000, min_match=0.75)


@pytest.mark.parametrize(
    "title, key",
    [
        ("Nintendo Switch 2 本体 新品未開封", "Switch 2"),
        ("Nintendo Switch 有機ELモデル ホワイト", "Switch 有機EL"),
        ("Nintendo Switch Lite ターコイズ 本体", "Switch Lite"),
        ("Nintendo Switch 本体 新型 バッテリー強化版 HAC-001(-01)", "Switch 新型"),
        ("Nintendo Switch 本体 旧型 HAC-001", "Switch 旧型"),
        ("Nintendo Switch 本体", None),  # どのモデルか分からない
        ("Nintendo Switch 2台 有機EL", "Switch 有機EL"),  # 「2台」は Switch 2 ではない
        ("PS5 本体 CFI-2000A01", "PS5 新型 ディスク"),
        ("PS5 本体 CFI-2000B01 デジタル・エディション", "PS5 新型 デジタル"),
        ("PlayStation5 初期型 ディスクドライブ搭載 CFI-1200A", "PS5 初期型 ディスク"),
        ("PS5 Pro 本体 CFI-7000B01", "PS5 Pro"),
        ("PS5 本体 ディスクドライブ搭載", None),  # 新型か初期型か分からない
        ("PS5 プロコン DualSense Edge", None),  # 「プロコン」は Pro ではない
        ("PS4 Pro CUH-7200 本体", "PS4 Pro"),
        ("PS4 本体 CUH-2200 薄型", "PS4 薄型"),
        ("Xbox Series X 本体", "Xbox Series X"),
        ("Steam Deck OLED 512GB", "Steam Deck OLED"),
    ],
)
def test_model_key(title, key):
    assert game_model_key(title) == key


@pytest.mark.parametrize(
    "title, condition, group",
    [
        ("Nintendo Switch 2 本体 新品未開封", None, "hw_new"),
        ("Nintendo Switch 有機ELモデル ホワイト", 3, "hw_used"),
        ("Nintendo Switch 有機EL 本体のみ", 3, "hw_loose"),
        ("スーパーファミコン 本体 ジャンク", 5, "hw_junk"),
        ("マリオカート8 デラックス", 1, "sw_new"),  # 状態「新品、未使用」
        ("マリオカート8 デラックス 新品同様", 2, "sw_used"),
        ("マリオカート8 デラックス ケースなし", 3, "sw_loose"),
        ("ゲームボーイ ポケットモンスター 赤 ソフトのみ", 4, "sw_loose"),
        ("Nintendo Switch 2 マリオカート ワールド", 3, "sw_used"),  # 機種名が入っていても本体ではない
        ("Nintendo Switch 有機EL ドック", 3, "other"),
        ("マリオカート8 デラックス ハンドル", 3, "other"),
    ],
)
def test_groups(title, condition, group):
    assert game_group(title, condition) == group


@pytest.mark.parametrize(
    "title, query, reason",
    [
        ("Nintendo Switch 有機ELモデル ホワイト 本体", CONSOLE_QUERY, None),
        ("Nintendo Switch 2 マリオカート ワールド", CONSOLE_QUERY, "ソフト（本体ではない）"),
        ("Nintendo Switch 有機EL ドック", CONSOLE_QUERY, "周辺機器・アクセサリー"),
        ("Nintendo Switch 本体 ソフト付き", CONSOLE_QUERY, "本体とソフトのセット"),
        ("Switch 本体 BAN済み", CONSOLE_QUERY, "アカウント・BANの問題"),
        ("Switch 本体 BANなし 動作確認済み 有機EL", CONSOLE_QUERY, None),
        ("Nintendo Switch 有機EL 本体 香港版", CONSOLE_QUERY, "海外版"),
        ("Nintendo Switch 2 本体 予約品", CONSOLE_QUERY, "予約品・抽選（手元にない商品）"),
        ("ニンテンドークラシックミニ ファミコン 本体", CONSOLE_QUERY, "ミニ（復刻版）"),
        ("PS4 本体 改造", CONSOLE_QUERY, "改造品"),
        # ソフトを探しているとき
        ("マリオカート8 デラックス Switch ソフト", text.tokens("マリオカート8 デラックス"), None),
        ("マリオカート8 デラックス 予約特典付き", text.tokens("マリオカート8 デラックス"), None),
        ("Nintendo Switch 本体 マリオカート8 デラックス セット", text.tokens("マリオカート8 デラックス"), "本体（ソフトではない）"),
        ("マリオカート8 デラックス ダウンロード版", text.tokens("マリオカート8 デラックス"), "ダウンロード版・コード"),
        ("マリオカート8 デラックス ソフト 3本セット", text.tokens("マリオカート8 デラックス"), "まとめ売り・セット"),
        # モデルを指定したとき
        ("Nintendo Switch Lite 本体", text.tokens("Nintendo Switch 有機EL"), "別のモデル"),
        ("Nintendo Switch 本体", text.tokens("Nintendo Switch 有機EL"), "モデルが分からない"),
    ],
)
def test_exclusions(title, query, reason):
    assert GAME.exclusion_reason(title, query) == reason


def test_shipping_depends_on_what_is_sold():
    def ship(title):
        return game_shipping(make_listing(title, 30000), 230)

    assert ship("マリオカート8 デラックス") == 230
    assert ship("Nintendo Switch 有機ELモデル 本体") == 1000
    assert ship("PS5 本体 CFI-2000A01") == 1500
    assert GAME.resale_shipping(make_listing("PS5 本体 CFI-2000A01", 50000), 230) == 1500


@pytest.mark.parametrize(
    "title, era",
    [
        ("ゲームボーイ ポケットモンスター 赤", "retro"),
        ("スーパーファミコン 本体", "retro"),
        ("ゲームボーイアドバンスSP 本体", "y2000s"),
        ("PS2 本体 SCPH-70000", "y2000s"),
        ("New 3DS LL 本体", "y2010s"),
        ("Wii U 本体", "y2010s"),
        ("Nintendo Switch 有機EL", "switch"),
        ("Nintendo Switch 2 本体", "current"),
        ("PS5 本体 CFI-2000A01", "current"),
    ],
)
def test_console_generations(title, era):
    assert detect_game_era([title]) is GAME_ERAS[era]


def test_older_generations_get_more_points():
    points = [GAME_ERAS[key].points for key in ("retro", "y2000s", "y2010s", "switch", "current")]
    assert points == sorted(points, reverse=True)
    assert detect_game_era(["マリオカート8 デラックス"]) is None  # 機種が書かれていない


class KeywordSource(FakeSource):
    """売り切れ検索では、キーワードごとに用意した出品を返す。"""

    def __init__(self, sold_by_keyword, **kwargs):
        super().__init__(**kwargs)
        self.sold_by_keyword = sold_by_keyword

    def search(self, query):
        self.queries.append(query)
        if query.sold:
            return self.sold_by_keyword.get(query.keyword, [])
        return [x for x in self.on_sale if not query.price_min or x.price >= query.price_min]


def item(title, price, **kwargs):
    return make_listing(title, price, condition=kwargs.pop("condition", 3), **kwargs)


def test_console_scan_compares_each_model_with_its_own_market(now):
    on_sale = [
        item("Nintendo Switch 有機ELモデル ホワイト 本体", 22000, id="oled", age=timedelta(hours=1)),
        item("PS5 本体 CFI-2000A01", 45000, id="disc", age=timedelta(hours=2)),
        item("PS5 本体 CFI-2000B01 デジタル", 38000, id="digital", age=timedelta(hours=3)),
        item("Nintendo Switch 2 マリオカート ワールド", 6000, id="soft"),
        item("Nintendo Switch 本体", 15000, id="unknown"),
    ]
    ps5_sold = [item(f"PS5 本体 CFI-2000A01 {i}", 60000, sold=True) for i in range(5)]
    ps5_sold += [item(f"PS5 本体 CFI-2000B01 デジタル {i}", 52000, sold=True) for i in range(5)]
    sold = {
        "Nintendo Switch 有機EL": [item(f"Nintendo Switch 有機EL 本体 {i}", 30000, sold=True) for i in range(5)]
        + [item("Nintendo Switch 有機EL ドック", 5000, sold=True)],  # 周辺機器は相場に入れない
        "PS5 本体": ps5_sold,
    }
    source = KeywordSource(sold, on_sale=on_sale)
    plan = SearchPlan(
        keyword="Nintendo Switch 本体", extra_keywords=["PS5 本体"], profile="game", scan=True, details=0,
        settings=SETTINGS,
    )
    result = run_scan(plan, {"mercari": source}, now=now)

    # PS5 のディスク版とデジタル版は同じ「PS5 本体」の売り切れから、手元で分けて相場を出す（取得は1回）
    assert [q.keyword for q in source.queries if q.sold] == ["Nintendo Switch 有機EL", "PS5 本体"]
    by_id = {e.listing.id: e for e in result.recommendations}
    assert by_id["oled"].expected_sale == 30000
    assert by_id["disc"].expected_sale == 60000
    assert by_id["digital"].expected_sale == 52000
    assert by_id["disc"].profit == 54000 - 1500 - 45000  # 据え置き機の送料 1,500円
    assert by_id["oled"].profit == 27000 - 1000 - 22000
    assert by_id["oled"].era is GAME_ERAS["switch"]
    assert result.excluded["ソフト（本体ではない）"] == 1
    assert result.excluded["モデルが分からない"] == 1


def test_software_search_keeps_titles_and_conditions_apart(now):
    sold = [item(f"マリオカート ワールド Switch2 {i}", 7000, sold=True) for i in range(6)]
    sold += [item(f"マリオカート ワールド 新品未開封 {i}", 9000, sold=True) for i in range(5)]
    sold += [item("マリオカート8 デラックス", 4500, sold=True) for _ in range(5)]  # 別のソフト
    sold += [item("Nintendo Switch 2 本体 マリオカート ワールド セット", 55000, sold=True)]  # 本体とのセット
    on_sale = [
        item("マリオカート ワールド Switch2 ソフト", 4500, id="used"),
        item("マリオカート ワールド 新品未開封", 6500, id="new"),
    ]
    source = FakeSource(sold=sold, on_sale=on_sale)
    plan = SearchPlan(keyword="マリオカート ワールド", profile="game", details=0, settings=SETTINGS)
    result = run_search(plan, {"mercari": source}, now=now)

    assert result.market.groups["sw_used"].median == 7000
    assert result.market.groups["sw_new"].median == 9000
    by_id = {e.listing.id: e for e in result.recommendations}
    assert by_id["used"].expected_sale == 7000 and by_id["used"].reference_label == "ソフト・中古の相場"
    assert by_id["new"].expected_sale == 9000


def test_profile_aliases_and_defaults():
    (plan,) = cli.build_plans(cli.build_parser().parse_args(["マリオカート8 デラックス", "--profile", "ゲーム"]))
    assert plan.profile == "game" and plan.settings.resale is True
    assert (plan.settings.min_match, plan.settings.resale_shipping, plan.settings.min_profit) == (0.75, 230, 1000)

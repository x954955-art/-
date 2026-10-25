import pytest

from furima_finder.eras import ERAS, detect_era, era_votes


@pytest.mark.parametrize(
    "title, era",
    [
        ("ポケモンカード 旧裏 リザードン 初版", "kyuura"),
        ("リザードン マークなし LV.76", "kyuura"),
        ("リザードンex SAR 201/165 sv2a", "sv"),
        ("ミュウex SAR [M6a 129/103](拡張パック「30th CELEBRATION」)", "mega"),
        ("ピカチュウ プロモ 001/SV-P", "sv"),
        ("ピカチュウ 025/S-P", "s"),
        ("ピカチュウ 025/DPt-P", "dp_legend"),
        ("リーリエ SR SM11b 068/049", "sm"),
        ("ギラティナV SR s12a", "s"),
        ("リザードンGX SSR", "sm"),
        ("ピカチュウVMAX HR", "s"),
        ("リザードン MUR", "mega"),
        ("ルギア LEGEND", "dp_legend"),
        ("ブラッキー ゴールドスター", "e_pcg"),
        ("ポケモンカードe リザードン 1ED", "e_pcg"),
        ("MリザードンEX XY", "xy"),
        ("リザードンex SAR", "sv_mega"),
    ],
)
def test_detect_era_from_a_title(title, era):
    assert detect_era([title]) is ERAS[era]


@pytest.mark.parametrize(
    "title",
    [
        "リザードン",
        "PSA10 リザードン",
        "ARS10 ミュウ",  # 鑑定会社の ARS をレアリティの AR と読まない
        "レギュレーションマークなし リザードン",  # 旧裏の「マークなし」とは別
        "旧裏 リザードン sv2a",  # 同じ強さの手がかりが食い違う
    ],
)
def test_unknown_or_conflicting_era(title):
    assert detect_era([title]) is None


def test_votes_across_listings_of_the_same_card():
    assert detect_era(["リザードンex SAR"] * 5 + ["リザードンex SAR sv2a"]) is ERAS["sv"]  # SAR の票は SV に足す
    assert detect_era(["ミュウ SAR M6a"] + ["ミュウ SAR"] * 3) is ERAS["mega"]
    assert detect_era(["旧裏 リザードン"] * 3 + ["リザードン sv2a"]) is ERAS["kyuura"]  # 多数決
    assert era_votes("リザードン sv2a")["sv"] == 3
    assert detect_era([]) is None


def test_older_eras_get_more_points():
    points = [ERAS[key].points for key in ("kyuura", "e_pcg", "bw", "sm", "s", "sv", "mega")]
    assert points == sorted(points, reverse=True)
    assert ERAS["kyuura"].points == 4 and ERAS["sv"].points == 0
    assert ERAS["kyuura"].description == "旧裏（1996〜2001年）"

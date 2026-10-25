"""カードの年代（シリーズ）の判定。

ポケモンカード: タイトルにあるシリーズ名・弾の記号（sv2a・s12a・sm11b など）・プロモ番号（001/SV-P など）・
その年代にしかないレアリティや種類（SAR・CSR・GX・VMAX など）から、おおよその年代を推定する。
遊戯王: タイトルに書かれた「初期」「2期」…「12期」から年代を決める。
古い年代のカードは再販されず、きれいな状態のものが減っていくため値上がりしやすい傾向がある。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from . import text


@dataclass(frozen=True)
class Era:
    key: str
    label: str
    years: str
    points: int  # 転売目線の採点の「年代」の点数（古いほど高い・最大4点）

    @property
    def description(self) -> str:
        return f"{self.label}（{self.years}）"


ERAS = {
    era.key: era
    for era in (
        Era("kyuura", "旧裏", "1996〜2001年", 4),
        Era("e_pcg", "e・ADV・PCG", "2001〜2006年", 4),
        Era("dp_legend", "DP・LEGEND", "2006〜2010年", 4),
        Era("bw", "BW", "2010〜2013年", 3),
        Era("xy", "XY", "2013〜2016年", 3),
        Era("sm", "SM", "2016〜2019年", 2),
        Era("s", "剣盾", "2019〜2022年", 1),
        Era("sv", "SV", "2023〜2025年", 0),
        Era("mega", "MEGA", "2025年〜", 0),
        # SAR・AR のように SV と MEGA のどちらにもあり、どちらか決められないとき
        Era("sv_mega", "SV・MEGA", "2023年〜", 0),
    )
}
MODERN = frozenset({"sv", "mega", "sv_mega"})

_p = text.compile_pattern
_PROMO_CODE = _p(r"(?<![a-z])(sv|sm|s|xy|bw|l|dpt|dp|pcg|adv|m)-p(?![a-z])")
_PROMO_ERAS = {
    "sv": "sv", "sm": "sm", "s": "s", "xy": "xy", "bw": "bw", "l": "dp_legend",
    "dpt": "dp_legend", "dp": "dp_legend", "pcg": "e_pcg", "adv": "e_pcg", "m": "mega",
}
PROMO_WEIGHT = 3

# （パターン, 年代, 重み）。重みは手がかりの確かさ:
#   弾の記号・プロモ番号・「旧裏」 3 / シリーズ名・その年代だけの種類やレアリティ 2 / SAR・AR 1
_SIGNALS = (
    # 弾の記号（sv2a・s12a・sm11b・xy8・cp6・bw7・m2a・dpt3・pcg5 など）
    (_p(r"(?<![a-z0-9])sv\d{1,2}[a-z]?(?![a-z0-9])"), "sv", 3),
    (_p(r"(?<![a-z0-9])sm\d{1,2}[a-z+]?(?![a-z0-9])"), "sm", 3),
    (_p(r"(?<![a-z0-9])s\d{1,2}[a-z]?(?![a-z0-9])"), "s", 3),
    (_p(r"(?<![a-z0-9])(xy\d{1,2}|cp[1-6])(?![a-z0-9])"), "xy", 3),
    (_p(r"(?<![a-z0-9])bw\d{1,2}(?![a-z0-9])"), "bw", 3),
    (_p(r"(?<![a-z0-9])m\d{1,2}[a-z]?(?![a-z0-9])"), "mega", 3),
    (_p(r"(?<![a-z0-9])dpt?\d(?![a-z0-9])"), "dp_legend", 3),
    (_p(r"(?<![a-z0-9])(pcg|adv)\d{1,2}(?![a-z0-9])"), "e_pcg", 3),
    # シリーズ名・その年代にしかない種類やレアリティ
    (_p(r"旧裏"), "kyuura", 3),
    (_p(r"(?<!レギュ)(?<!レギュレーション)マーク\s*(なし|無し|無)"), "kyuura", 2),  # 第1弾の初版
    (
        _p(r"(?<![a-z])(e-?card|adv|pcg)(?![a-z])|カード\s*e(?![a-z])|(?<![a-z])eカード|デルタ種|ゴールドスター|クリスタルタイプ"),
        "e_pcg",
        2,
    ),
    (_p(r"(?<![a-z])(dpt?|legend)(?![a-z])|(?<![a-z])lv\.?\s*x(?![a-z])"), "dp_legend", 2),
    (_p(r"(?<![a-z])bw(?![a-z0-9])"), "bw", 2),
    (_p(r"(?<![a-z])(xy|break)(?![a-z0-9])"), "xy", 2),
    (_p(r"(?<![a-z])(sm|gx|tag\s*team)(?![a-z0-9])|サン\s*&?\s*ムーン|タッグチーム|プリズムスター"), "sm", 2),
    (_p(r"(?<![a-z])(vmax|vstar|vunion|chr|csr|25th)(?![a-z])|剣盾|ソード\s*&?\s*シールド|かがやく"), "s", 2),
    (_p(r"(?<![a-z])(sv|bwr|classic)(?![a-z0-9])|スカーレット|バイオレット|クラシック"), "sv", 2),
    (_p(r"(?<![a-z])(mega|mur)(?![a-z])"), "mega", 2),
    (_p(r"(?<![a-z])(sar|ar)(?![a-z])"), "sv_mega", 1),
)


def era_votes(title: str) -> Counter[str]:
    """1つのタイトルにある年代の手がかり（年代ごとの重みの合計）。"""
    normalized = text.normalize(title)
    votes: Counter[str] = Counter()
    for match in _PROMO_CODE.finditer(normalized):
        votes[_PROMO_ERAS[match.group(1)]] += PROMO_WEIGHT
    for pattern, key, weight in _SIGNALS:
        if pattern.search(normalized):
            votes[key] += weight
    return votes


def detect_era(titles: Iterable[str]) -> Era | None:
    """タイトル（同じカードの出品なら複数まとめて）から年代を推定する。手がかりがないか食い違うときは None。"""
    votes: Counter[str] = Counter()
    for title in titles:
        votes.update(era_votes(title))
    generic = votes.pop("sv_mega", 0)
    if generic:
        # SAR・AR は SV と MEGA のどちらにもあるので、どちらかの手がかりがあればそちらに足す
        specific = "sv" if votes["sv"] >= votes["mega"] else "mega"
        votes[specific if votes[specific] else "sv_mega"] += generic
    ranked = votes.most_common(2)
    if not ranked or (len(ranked) == 2 and ranked[0][1] == ranked[1][1]):
        return None
    return ERAS[ranked[0][0]]


# ---------------------------------------------------------------- 遊戯王

# 遊戯王は発売時期を「初期（1期）」「2期」…「12期」と呼ぶ。「初期傷」は時期ではない
_YGO_PERIOD = _p(r"(?<!\d)(1[0-2]|[1-9])\s*期|初期(?!\s*(傷|キズ|不良))")
YGO_ERAS = {
    period: Era(f"ygo{period}", label, years, points)
    for period, label, years, points in (
        ("1", "初期", "1999〜2000年ごろ", 4),
        ("2", "2期", "2000〜2002年ごろ", 4),
        ("3", "3期", "2002〜2004年ごろ", 4),
        ("4", "4期", "2004〜2006年ごろ", 4),
        ("5", "5期", "2006〜2008年ごろ", 4),
        ("6", "6期", "2008〜2010年ごろ", 4),
        ("7", "7期", "2010〜2012年ごろ", 3),
        ("8", "8期", "2012〜2014年ごろ", 3),
        ("9", "9期", "2014〜2017年ごろ", 2),
        ("10", "10期", "2017〜2020年ごろ", 2),
        ("11", "11期", "2020〜2023年ごろ", 1),
        ("12", "12期", "2023年〜", 0),
    )
}


def detect_ygo_era(titles: Iterable[str]) -> Era | None:
    """遊戯王のタイトル（同じカードなら複数まとめて）に書かれた「初期」「N期」の多数決。なければ None。"""
    votes: Counter[str] = Counter()
    for title in titles:
        for match in _YGO_PERIOD.finditer(text.normalize(title)):
            votes[match.group(1) or "1"] += 1
    ranked = votes.most_common(2)
    if not ranked or (len(ranked) == 2 and ranked[0][1] == ranked[1][1]):
        return None
    return YGO_ERAS[ranked[0][0]]

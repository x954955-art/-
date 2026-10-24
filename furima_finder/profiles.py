"""商品ジャンルごとの判定ルール（プロファイル）。

- default: 一般的な中古品向け。相場は状態（新品・未使用 / 美品 / 傷・汚れあり）ごとに分ける。
- pokeca : ポケモンカード向け。鑑定（PSA10 など）と BOX のシュリンク有無で相場を分け、
           オリパ・偽物の可能性・海外版・まとめ売り・サプライ品を除外する。転売目線が既定。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import text
from .models import Listing


@dataclass(frozen=True)
class Profile:
    name: str
    label: str
    # タイトルと状態から、相場を分ける単位（グループ）を決める
    group_of: Callable[[str, int | None], str | None]
    group_labels: Mapping[str, str]
    # True なら、同じグループの売り切れがないと比較しない（PSA10 を未鑑定の相場と比べない等）
    strict_groups: bool = False
    exclusion_rules: tuple[text.Rule, ...] = ()
    extra_exclusion: Callable[[str, list[str]], str | None] | None = None
    strong_risk_words: tuple[str, ...] = ()
    mild_risk_words: tuple[str, ...] = ()
    # このプロファイルを選んだときの既定値（コマンドラインや設定ファイルの値が優先）
    defaults: Mapping[str, Any] = field(default_factory=dict)
    # スキャン用: タイトルから「どのカードか」とレアリティを取り出す
    card_key: Callable[[str], str | None] | None = None
    rarity: Callable[[str], str | None] | None = None
    # 転売時に大きめの送料がかかるグループ（BOX など）とその最低送料
    large_groups: frozenset[str] = frozenset()
    large_shipping: int = 0

    def group(self, listing: Listing) -> str | None:
        return self.group_of(listing.title, listing.condition)

    def exclusion_reason(self, title: str, query_tokens: list[str], *, include_reserved: bool = True) -> str | None:
        reason = text.exclusion_reason(
            title, query_tokens, include_reserved=include_reserved, extra_rules=self.exclusion_rules
        )
        if reason is None and self.extra_exclusion is not None:
            reason = self.extra_exclusion(title, query_tokens)
        return reason

    def risk_level(self, title: str, query_tokens: list[str]) -> tuple[int, str | None]:
        return text.risk_level(
            title, query_tokens, strong_extra=self.strong_risk_words, mild_extra=self.mild_risk_words
        )

    def is_strong_risk(self, word: str) -> bool:
        return word in text.STRONG_RISK_WORDS or word in self.strong_risk_words

    def description_risks(self, description: str, query_tokens: list[str]) -> list[str]:
        return text.description_risks(
            description, query_tokens, extra_words=self.strong_risk_words + self.mild_risk_words
        )

    def resale_shipping(self, listing: Listing, base: int) -> int:
        if self.group(listing) in self.large_groups:
            return max(base, self.large_shipping)
        return base


# ---------------------------------------------------------------- 一般

CONDITION_GROUPS = {
    "new": ("新品・未使用", frozenset({1})),
    "good": ("美品", frozenset({2, 3})),
    "used": ("傷・汚れあり", frozenset({4, 5, 6})),
}


def condition_group(title: str, condition: int | None) -> str | None:
    for key, (_, members) in CONDITION_GROUPS.items():
        if condition in members:
            return key
    return None


DEFAULT = Profile(
    name="default",
    label="一般",
    group_of=condition_group,
    group_labels={key: label for key, (label, _) in CONDITION_GROUPS.items()},
)


# ---------------------------------------------------------------- ポケモンカード

_p = text.compile_pattern
# カード番号: 201/165 のような「3桁/3桁」と、001/SV-P のようなプロモ番号
_CARD_NUMBER = _p(r"(?<!\d)(\d{3})\s*/\s*(\d{3}|[a-z]{1,4}-p)(?![\da-z])")
_RARITY = _p(r"(?<![a-z])(sar|sr|ar|ur|hr|chr|csr|ssr|rrr|rr|mur|bwr|ma|fur|ace)(?![a-z])")
_PROMO = _p(r"プロモ|promo")
_PSA10 = _p(r"(?<![a-z])psa\s*10(?!\d)")
_PSA9 = _p(r"(?<![a-z])psa\s*9(?![\d.])")
_GRADED = _p(r"(?<![a-z])(psa|bgs|cgc)(?![a-z])|(?<![a-z])ars\s*\d|(?<!未)鑑定")
# 「PSA10狙い」「鑑定用」のように、まだ鑑定されていないカード
_NOT_GRADED_YET = _p(
    r"(psa|鑑定)\s*\d*\s*(狙|ねらい|候補|レベル|級|クラス|相当|向け|用|前|に出|可能|期待|いけ|出せ)|未鑑定"
)
_BOX = _p(r"(?<![a-z])box(?![a-z])|ボックス|箱")
_PACK = _p(r"パック")
_SHRINK_NO = _p(r"シュリンク\s*(なし|無し|無)")
_SHRINK_YES = _p(r"シュリンク\s*(付|つき|あり|有)|未開封シュリンク")

POKECA_GROUPS = {
    "raw": "未鑑定",
    "psa10": "PSA10",
    "psa9": "PSA9",
    "graded": "鑑定品（その他）",
    "box_shrink": "BOX（シュリンク付き）",
    "box_noshrink": "BOX（シュリンクなし）",
    "box": "BOX（シュリンク不明）",
    "other": "カード以外",
}
BOX_GROUPS = frozenset({"box_shrink", "box_noshrink", "box", "other"})


def _looks_like_card(normalized: str) -> bool:
    return bool(
        _CARD_NUMBER.search(normalized)
        or _RARITY.search(normalized)
        or _PROMO.search(normalized)
        or _GRADED.search(normalized)
    )


def _looks_like_box(normalized: str) -> bool:
    return bool(_BOX.search(normalized) or _PACK.search(normalized))


def pokeca_group(title: str, condition: int | None) -> str:
    normalized = text.normalize(title)
    if _looks_like_card(normalized):
        if _NOT_GRADED_YET.search(normalized):
            return "raw"
        if _PSA10.search(normalized):
            return "psa10"
        if _PSA9.search(normalized):
            return "psa9"
        if _GRADED.search(normalized):
            return "graded"
        return "raw"
    if _BOX.search(normalized):
        if _SHRINK_NO.search(normalized):
            return "box_noshrink"
        if _SHRINK_YES.search(normalized):
            return "box_shrink"
        return "box"
    return "other"


def pokeca_card_key(title: str) -> str | None:
    """カード番号（例: 201/165, 001/SV-P）。番号が書かれていなければ None。"""
    match = _CARD_NUMBER.search(text.normalize(title))
    return f"{match.group(1)}/{match.group(2)}".upper() if match else None


def pokeca_rarity(title: str) -> str | None:
    match = _RARITY.search(text.normalize(title))
    return match.group(1).upper() if match else None


def _pokeca_kind_mismatch(title: str, query_tokens: list[str]) -> str | None:
    """カードを探しているのにBOXやサプライ品、BOXを探しているのに単品カード、を外す。"""
    query = " ".join(query_tokens)
    normalized = text.normalize(title)
    if _looks_like_box(query):
        return "単品カード" if _looks_like_card(normalized) else None
    if _looks_like_card(query) and not _looks_like_card(normalized):
        return "BOX・パック" if _looks_like_box(normalized) else "カード以外・カードを特定できない"
    return None


_rule = text.rule
POKECA_RULES = (
    _rule(r"オリパ|オリジナルパック", "オリパ", "オリパ"),
    _rule(
        r"プロキシ|proxy|レプリカ|コピー品|非正規|非公式|ファンアート|カスタムカード|オリジナルカード|自作",
        "非公式品・偽物の可能性",
    ),
    _rule(r"英語版|(?<![a-z])(eng|english)(?![a-z])", "海外版", "英語"),
    _rule(r"韓国版|(?<![a-z])korean(?![a-z])", "海外版", "韓国"),
    _rule(r"中国版|簡体字|繁体字|台湾版|(?<![a-z])chinese(?![a-z])", "海外版", "中国"),
    _rule(r"海外版|北米版|欧州版", "海外版", "海外"),
    # 「おまとめ割」「まとめ買い歓迎」は値引きの案内なので除外しない
    _rule(r"(?<!お)まとめ(?!\s*(買|割|可|歓迎|値引))|引退|大量|ストレージ", "まとめ売り・セット", "まとめ"),
    _rule(r"(?<!\d)[2-9]\s*枚|\d{2,}\s*枚|枚セット|セット売り", "まとめ売り・セット", "枚"),
    _rule(r"(?<!\d)[2-9]\s*(box|ボックス|箱)", "まとめ売り・セット", "まとめ"),
    _rule(r"構築済|デッキ(?!\s*(シールド|ケース|パーツ|用|に|で))", "まとめ売り・セット", "デッキ"),
    _rule(
        r"(スリーブ|ローダー|プレイマット|ラバーマット|バインダー|ファイル|デッキケース|デッキシールド)(のみ|だけ)",
        "サプライ品のみ",
        "スリーブ",
    ),
)

POKECA = Profile(
    name="pokeca",
    label="ポケモンカード",
    group_of=pokeca_group,
    group_labels=POKECA_GROUPS,
    strict_groups=True,
    exclusion_rules=POKECA_RULES,
    extra_exclusion=_pokeca_kind_mismatch,
    strong_risk_words=(
        "折れ", "折り目", "破れ", "水濡れ", "濡れ", "凹み", "へこみ", "シワ",
        "再シュリンク", "サーチ済", "サーチ品",
    ),
    mild_risk_words=(
        "初期傷", "白欠け", "白かけ", "傷あり", "傷有", "キズあり", "スレあり", "スレ有", "角スレ",
        "反り", "ヨレ", "色あせ", "色褪せ", "日焼け", "汚れあり",
    ),
    defaults={
        "resale": True,
        "buyer_shipping": 300,
        "resale_shipping": 230,  # ネコポス 210円 + 資材
        "min_profit": 1000,
    },
    card_key=pokeca_card_key,
    rarity=pokeca_rarity,
    large_groups=BOX_GROUPS,
    large_shipping=520,  # 宅急便コンパクト・ゆうパケットプラス程度
)


PROFILES = {"default": DEFAULT, "pokeca": POKECA}
ALIASES = {"一般": "default", "pokemon": "pokeca", "ポケカ": "pokeca", "ポケモンカード": "pokeca"}


def get_profile(name: str) -> Profile:
    key = ALIASES.get(name) or name.lower()
    if key not in PROFILES:
        raise ValueError(f"未対応のプロファイルです: {name}（{', '.join(PROFILES)}）")
    return PROFILES[key]


def most_common_rarity(profile: Profile, titles: Iterable[str]) -> str | None:
    if profile.rarity is None:
        return None
    counts = Counter(r for r in map(profile.rarity, titles) if r)
    return counts.most_common(1)[0][0] if counts else None

"""商品ジャンルごとの判定ルール（プロファイル）。

- default: 一般的な中古品向け。相場は状態（新品・未使用 / 美品 / 傷・汚れあり）ごとに分ける。
- pokeca : ポケモンカード向け。鑑定（PSA10 など）・初版・BOX のシュリンク有無で相場を分け、
           オリパ・偽物の可能性・海外版・まとめ売り・サプライ品を除外する。転売目線が既定。
           年代（旧裏〜MEGA）も推定し、カード番号のない旧裏はポケモンの名前でカードを見分ける。
- onepiece: ワンピースカード向け。カード番号（OP01-120 など）とパラレル・スーパーパラレル（コミパラ）・
           SP・TR の違いでカードを見分け、鑑定と BOX のテープ有無で相場を分ける。転売目線が既定。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import text
from .eras import MODERN, Era, detect_era
from .models import Listing
from .pokemon_names import OLD_POKEMON


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
    # 年代（旧裏・SM・SV など）の推定。古い年代ほど値上がりしやすいとみて採点に使う
    era_of: Callable[[Iterable[str]], Era | None] | None = None
    # スキャンでどのカードか分からなかった出品の理由
    unknown_card_reason: Callable[[str], str] | None = None
    # スキャン用: card_key で分からないとき、タイトルと検索語からカードを推測する（写真で確認が必要）
    guess_card_key: Callable[[str, list[str]], str | None] | None = None
    # スキャン用: カード（card_key）とその出品のタイトルから、売り切れを探すキーワードを決める
    sold_keyword: Callable[[str, list[str]], str] | None = None

    def group(self, listing: Listing) -> str | None:
        return self.group_of(listing.title, listing.condition)

    def missing_card_reason(self, title: str) -> str:
        return self.unknown_card_reason(title) if self.unknown_card_reason else "カード番号なし"

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
    # 旧裏 第1弾の初版（マークなし）や 1ED は、同じカードでも別の相場
    "raw_1ed": "初版・未鑑定",
    "psa10_1ed": "初版・PSA10",
    "psa9_1ed": "初版・PSA9",
    "graded_1ed": "初版・鑑定品（その他）",
    "box_shrink": "BOX（シュリンク付き）",
    "box_noshrink": "BOX（シュリンクなし）",
    "box": "BOX（シュリンク不明）",
    "other": "カード以外",
}
BOX_GROUPS = frozenset({"box_shrink", "box_noshrink", "box", "other"})

# ---- 旧裏（カード番号のない古いカード）

_KYUURA = _p(r"旧裏|(?<!レギュ)(?<!レギュレーション)マーク\s*(なし|無し|無)")
# 古いカードの手がかり（LV は旧裏とDPの頃の書き方）
_OLD_HINT = _p(
    r"旧裏|初版|(?<!レギュ)(?<!レギュレーション)マーク\s*(なし|無し|無)|(?<![a-z])lv\.?\s*\d"
    r"|(?<![a-z0-9])1\s*ed(?![a-z])|1st\s*edition|ファースト\s*エディション|当時物"
)
_FIRST_EDITION = _p(
    r"初版(?!\s*(では|じゃ|でな))|(?<!レギュ)(?<!レギュレーション)マーク\s*(なし|無し|無)"
    r"|(?<![a-z0-9])1\s*ed(ition)?(?![a-z])|1st\s*edition|ファースト\s*エディション"
)
# 相場を知らない人が古いカードに付けがちな言葉（「旧裏」とは書かずに「昔の」「当時物」など）。「初期傷」は除く
_OLD_GUESS = _p(
    r"昔|古い|初期(?!\s*(傷|キズ|不良))|当時|懐かし|レトロ|平成|実家|押し?入れ|発掘|子供の頃"
    r"|(?<!\d)199[6-9](?![\d円])"
)
# 旧裏より後のカードにしかない種類（「昔のポケモンカード リザードンex」は旧裏ではない）
_NEWER_KIND = _p(r"(?<![a-z])(ex|gx|vmax|vstar|vunion|break)(?![a-z])|[ァ-ヺー]v(?![a-z])")
# 1枚のカードではない商品（パック・BOX・サプライなど）。「拡張パック第1弾」はカードの弾の名前なので除く
_NOT_SINGLE = _p(
    r"未開封|(?<![a-z])box(?![a-z])|ボックス|箱|(?<!拡張)パック|スターター|デッキ|ファイル|スリーブ"
    r"|サプライ|プレイマット|ローダー|バインダー|ジャンボ|ポスター|シール"
)
_KANA = re.compile(r"[ァ-ヺー]")
_OLD_NAMES = {text.normalize(name): name for name in OLD_POKEMON}
# 長い名前から試す（「ミュウツー」を「ミュウ」、「レアコイル」を「コイル」と読まないため）
_OLD_NAME = re.compile("|".join(re.escape(name) for name in sorted(_OLD_NAMES, key=len, reverse=True)))
# 名前の前に付いて別のカードになるもの（「わるいリザードン」「ひかるギャラドス」「カスミのトサキント」）。
# 正規化でカタカナになった検索語（「ワルイリザードン」）でも読めるよう、両方の書き方を持つ
_NAME_PREFIXES = {
    spelling: word for word in ("わるい", "ひかる", "やさしい") for spelling in (word, text.normalize(word))
}
_POSSESSIVES = ("の", text.normalize("の"))
_OWNERS = {
    text.normalize(word): word
    for word in (
        "タケシ", "カスミ", "マチス", "エリカ", "ナツメ", "キョウ", "カツラ", "サカキ", "ロケット団", "R団",
        "ハヤト", "ツクシ", "アカネ", "マツバ", "シジマ", "ミカン", "ヤナギ", "イブキ", "ワタル",
        "カンナ", "シバ", "キクコ", "カリン",
    )
}
# 名前にくっついていても区切りとみなす言葉（「ポケモンカードリザードン」「リザードンホロ」など）
_LEFT_WORDS = tuple(text.normalize(word) for word in ("カード", "ゲーム", "ポケモン", "ポケカ", "キラ", "ホロ", "プロモ", "レア"))
_RIGHT_WORDS = tuple(
    text.normalize(word) for word in ("ホロ", "キラ", "プロモ", "レア", "カード", "ポケモン", "ポケカ", "マーク", "の")
)


def _left_ok(before: str) -> bool:
    """名前の直前が言葉の区切りか（「ファンクラブ」の「クラブ」のように、別の言葉の一部ではないか）。"""
    return not before or not _KANA.match(before[-1]) or before.endswith(_LEFT_WORDS)


def _name_prefix(before: str) -> str | None:
    """名前の前に付く「わるい」「カスミの」など。付かなければ空文字、別の言葉の一部なら None。"""
    for spelling, word in _NAME_PREFIXES.items():
        if before.endswith(spelling) and _left_ok(before[: -len(spelling)]):
            return word
    if before.endswith(_POSSESSIVES):
        head = before[:-1]
        for kana, word in _OWNERS.items():
            if head.endswith(kana) and _left_ok(head[: -len(kana)]):
                return word + "の"
        return "" if _left_ok(head) else None  # 「旧裏のリザードン」の「の」
    return "" if _left_ok(before) else None


def old_card_name(title: str) -> str | None:
    """タイトルに書かれた第1・第2世代のポケモンの名前（「わるい」「カスミの」などが付けば込み）。
    見つからないか、違う名前が2つ以上あれば None。"""
    # ひらがなはカタカナにしない（「リザードンです」の「です」を名前の続きの言葉と読まないため）
    base = unicodedata.normalize("NFKC", title).lower()
    names = set()
    for match in _OLD_NAME.finditer(base):
        prefix = _name_prefix(base[: match.start()])
        after = base[match.end() :]
        if prefix is None or (after and _KANA.match(after[0]) and not after.startswith(_RIGHT_WORDS)):
            continue
        names.add(prefix + _OLD_NAMES[match.group()])
    return names.pop() if len(names) == 1 else None


def _is_old_card(normalized: str, title: str) -> bool:
    """カード番号のない古いカード（旧裏・初版・当時物・昔の などの手がかりと、ポケモンの名前がある）。"""
    return bool(
        (_OLD_HINT.search(normalized) or _OLD_GUESS.search(normalized))
        and not _NOT_SINGLE.search(normalized)
        and old_card_name(title)
    )


def _first_edition(normalized: str) -> bool:
    """初版（旧裏 第1弾のマークなし・1ED）。SV以降のカードに書かれた「初版」は区別しない。"""
    if not _FIRST_EDITION.search(normalized):
        return False
    era = detect_era([normalized])
    return era is None or era.key not in MODERN


def _looks_like_card(normalized: str, title: str | None = None) -> bool:
    """title は正規化する前のタイトル（あればポケモンの名前を読むのに使う）。"""
    return bool(
        _CARD_NUMBER.search(normalized)
        or _RARITY.search(normalized)
        or _PROMO.search(normalized)
        or _GRADED.search(normalized)
        or _is_old_card(normalized, title or normalized)
    )


def _looks_like_box(normalized: str) -> bool:
    return bool(_BOX.search(normalized) or _PACK.search(normalized))


def _grade(normalized: str) -> str:
    if _NOT_GRADED_YET.search(normalized):
        return "raw"
    if _PSA10.search(normalized):
        return "psa10"
    if _PSA9.search(normalized):
        return "psa9"
    if _GRADED.search(normalized):
        return "graded"
    return "raw"


def pokeca_group(title: str, condition: int | None) -> str:
    normalized = text.normalize(title)
    if _looks_like_card(normalized, title):
        grade = _grade(normalized)
        return f"{grade}_1ed" if _first_edition(normalized) else grade
    if _BOX.search(normalized):
        if _SHRINK_NO.search(normalized):
            return "box_noshrink"
        if _SHRINK_YES.search(normalized):
            return "box_shrink"
        return "box"
    return "other"


def pokeca_card_key(title: str) -> str | None:
    """どのカードか。カード番号（例: 201/165, 001/SV-P）か、番号のない旧裏ならカード名
    （例: 旧裏 わるいリザードン）。分からなければ None。"""
    normalized = text.normalize(title)
    match = _CARD_NUMBER.search(normalized)
    if match:
        return f"{match.group(1)}/{match.group(2)}".upper()
    if _KYUURA.search(normalized) and not _NOT_SINGLE.search(normalized):
        name = old_card_name(title)
        if name:
            return f"旧裏 {name}" + (" プロモ" if _PROMO.search(normalized) else "")
    return None


def pokeca_guess_key(title: str, query_tokens: list[str]) -> str | None:
    """「旧裏」と書かれていなくても、「昔の」「当時物」などの言葉とポケモンの名前があれば旧裏とみなす
    （相場を知らない人の出品向け。本当に旧裏かは写真で確認が必要）。古いカードを探すときだけ使う。"""
    query = " ".join(query_tokens)
    if not (_KYUURA.search(query) or _OLD_GUESS.search(query)):
        return None
    normalized = text.normalize(title)
    if (
        not _OLD_GUESS.search(normalized)
        or _NOT_SINGLE.search(normalized)
        or _NEWER_KIND.search(normalized)
        or _RARITY.search(normalized)
    ):
        return None
    era = detect_era([normalized])
    if era is not None and era.key != "kyuura":
        return None
    name = old_card_name(title)
    return f"旧裏 {name}" + (" プロモ" if _PROMO.search(normalized) else "") if name else None


def _pokeca_unknown_card(title: str) -> str:
    normalized = text.normalize(title)
    old = _KYUURA.search(normalized) or _OLD_GUESS.search(normalized)
    return "カード名を特定できない" if old else "カード番号なし"


def pokeca_rarity(title: str) -> str | None:
    match = _RARITY.search(text.normalize(title))
    return match.group(1).upper() if match else None


def _pokeca_kind_mismatch(title: str, query_tokens: list[str]) -> str | None:
    """カードを探しているのにBOXやサプライ品、BOXを探しているのに単品カード、
    カード番号（旧裏ならカード名）を指定しているのに別のカード、を外す。"""
    query = " ".join(query_tokens)
    normalized = text.normalize(title)
    if _looks_like_box(query):
        return "単品カード" if _looks_like_card(normalized, title) else None
    if _looks_like_card(query) and not _looks_like_card(normalized, title):
        return "BOX・パック" if _looks_like_box(normalized) else "カード以外・カードを特定できない"
    wanted = pokeca_card_key(query)
    if wanted is not None:
        key = pokeca_card_key(title)
        if key is not None and key != wanted:
            return "別のカード"
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
    era_of=detect_era,
    unknown_card_reason=_pokeca_unknown_card,
    guess_card_key=pokeca_guess_key,
)


# ---------------------------------------------------------------- ワンピースカード

# カード番号: OP01-120（ブースター）・ST10-013（スタートデッキ）・EB01-061・PRB01-001、プロモは P-001
_OP_NUMBER = _p(r"(?<![a-z0-9])(op|st|eb|prb)\s*-?\s*(\d{1,2})(?:\s*[-‐−–ー]\s*|\s+)(\d{3})(?!\d)")
_OP_PROMO_NUMBER = _p(r"(?<![a-z0-9])p\s*[-‐−–ー]\s*(\d{3})(?!\d)")
# 同じカード番号でも絵柄や加工が違うと値段が大きく違うので、別のカードとして相場を調べる（上から順に判定）
_OP_VERSIONS = {
    "スーパーパラレル": _p(r"スーパーパラ(レル)?|コミパラ|コミックパラ(レル)?|(マンガ|漫画)(背景|柄|レア|版)"),
    "TR": _p(r"(?<![a-z])tr(?![a-z])|トレジャーレア"),
    "SP": _p(r"(?<![a-z])sp(?![a-z])|スペシャルカード"),
    # 「パラレルではありません」「非パラ」「パラなし」は通常版
    "パラレル": _p(r"パラレル(?!\s*(では|じゃ|で(は)?な|なし|無し))|(?<![ァ-ヺー非])パラ(?![ァ-ヺー])"),
}
_OP_CARD_HINT = _p(r"(?<![a-z])(sec|sr|sp|tr)(?![a-z])|シークレット|パラレル|コミパラ|スーパーパラ|リーダー|プロモ")
_OP_BOX = _p(r"(?<![a-z])box(?![a-z])|ボックス|箱|カートン")
_TAPE_NO = _p(r"テープ\s*(なし|無し|無)|テープ\s*(剥がれ|はがれ|破れ)|開封済")
_TAPE_YES = _p(r"テープ\s*(付|つき|あり|有)")

ONEPIECE_GROUPS = {
    "raw": "未鑑定",
    "psa10": "PSA10",
    "psa9": "PSA9",
    "graded": "鑑定品（その他）",
    "box_tape": "BOX（テープ付き）",
    "box_notape": "BOX（テープなし）",
    "box": "BOX（テープ不明）",
    "other": "カード以外",
}
ONEPIECE_BOX_GROUPS = frozenset({"box_tape", "box_notape", "box", "other"})


def _op_version(normalized: str) -> str | None:
    return next((name for name, pattern in _OP_VERSIONS.items() if pattern.search(normalized)), None)


def onepiece_card_key(title: str) -> str | None:
    """どのカードか: カード番号（例: OP01-120, P-001）と、パラレル・スーパーパラレル・SP・TR の違い
    （例: OP01-120 パラレル）。番号が書かれていなければ None。"""
    normalized = text.normalize(title)
    match = _OP_NUMBER.search(normalized)
    if match:
        series, set_number, card_number = match.groups()
        key = f"{series.upper()}{int(set_number):02d}-{card_number}"
    else:
        promo = _OP_PROMO_NUMBER.search(normalized)
        if promo is None:
            return None
        key = f"P-{promo.group(1)}"
    version = _op_version(normalized)
    return f"{key} {version}" if version else key


def onepiece_sold_keyword(key: str, titles: list[str]) -> str:
    """売り切れを探すキーワード。種類は出品でいちばん多い書き方にする（「コミパラ」と「スーパーパラレル」など）。"""
    number, _, version = key.partition(" ")
    if not version:
        return number
    pattern = _OP_VERSIONS[version]
    words = Counter(match.group() for title in titles if (match := pattern.search(text.normalize(title))))
    if not words:
        return key
    word = words.most_common(1)[0][0]
    return f"{number} {word.upper() if word.isascii() else word}"  # 正規化で小文字になった「sp」は「SP」に戻す


def _op_looks_like_card(normalized: str) -> bool:
    return bool(
        _OP_NUMBER.search(normalized)
        or _OP_PROMO_NUMBER.search(normalized)
        or _OP_CARD_HINT.search(normalized)
        or _GRADED.search(normalized)
    )


def _op_looks_like_box(normalized: str) -> bool:
    return bool(_OP_BOX.search(normalized) or _PACK.search(normalized))


def onepiece_group(title: str, condition: int | None) -> str:
    normalized = text.normalize(title)
    if _op_looks_like_card(normalized):
        return _grade(normalized)
    if _OP_BOX.search(normalized):
        if _TAPE_NO.search(normalized):
            return "box_notape"
        if _TAPE_YES.search(normalized):
            return "box_tape"
        return "box"
    return "other"


def _onepiece_kind_mismatch(title: str, query_tokens: list[str]) -> str | None:
    """カードを探しているのにBOXやサプライ品、BOXを探しているのに単品カード、
    カード番号を指定しているのに別のカード（パラレルと通常版の違いも含む）、を外す。"""
    query = " ".join(query_tokens)
    normalized = text.normalize(title)
    if _op_looks_like_box(query):
        return "単品カード" if _op_looks_like_card(normalized) else None
    if _op_looks_like_card(query) and not _op_looks_like_card(normalized):
        return "BOX・パック" if _op_looks_like_box(normalized) else "カード以外・カードを特定できない"
    wanted = onepiece_card_key(query)
    if wanted is not None:
        key = onepiece_card_key(title)
        if key is not None and key != wanted:
            return "別のカード"
    return None


ONEPIECE_RULES = POKECA_RULES + (
    _rule(r"アジア版|(?<![a-z])asia(?![a-z])", "海外版", "アジア"),
    _rule(r"カートン", "まとめ売り・セット", "カートン"),
    _rule(r"ドン\s*!!|ドン\s*カード|(?<![a-z])don\s*!!", "ドン!!カード", "ドン"),
)

ONEPIECE = Profile(
    name="onepiece",
    label="ワンピースカード",
    group_of=onepiece_group,
    group_labels=ONEPIECE_GROUPS,
    strict_groups=True,
    exclusion_rules=ONEPIECE_RULES,
    extra_exclusion=_onepiece_kind_mismatch,
    strong_risk_words=POKECA.strong_risk_words + ("テープ剥がれ", "テープはがれ", "テープ破れ"),
    mild_risk_words=POKECA.mild_risk_words,
    defaults=dict(POKECA.defaults),
    card_key=onepiece_card_key,
    large_groups=ONEPIECE_BOX_GROUPS,
    large_shipping=520,
    sold_keyword=onepiece_sold_keyword,
)


PROFILES = {"default": DEFAULT, "pokeca": POKECA, "onepiece": ONEPIECE}
ALIASES = {
    "一般": "default",
    "pokemon": "pokeca",
    "ポケカ": "pokeca",
    "ポケモンカード": "pokeca",
    "one_piece": "onepiece",
    "ワンピース": "onepiece",
    "ワンピースカード": "onepiece",
    "ワンピカ": "onepiece",
}


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

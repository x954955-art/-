"""商品ジャンルごとの判定ルール（プロファイル）。

- default: 一般的な中古品向け。相場は状態（新品・未使用 / 美品 / 傷・汚れあり）ごとに分ける。
- pokeca : ポケモンカード向け。鑑定（PSA10 など）・初版・BOX のシュリンク有無で相場を分け、
           オリパ・偽物の可能性・海外版・まとめ売り・サプライ品を除外する。転売目線が既定。
           年代（旧裏〜MEGA）も推定し、カード番号のない旧裏はポケモンの名前でカードを見分ける。
- onepiece: ワンピースカード向け。カード番号（OP01-120 など）とパラレル・スーパーパラレル（コミパラ）・
           SP・TR の違いでカードを見分け、鑑定と BOX のテープ有無で相場を分ける。転売目線が既定。
- yugioh : 遊戯王向け。カード番号（QCCU-JP001 など）とレアリティの組み合わせでカードを見分ける。
- game   : ゲーム機・ソフト向け。本体・ソフト・周辺機器を見分け、本体はモデル（有機EL・Lite・PS5 の
           ディスク版/デジタル版 など）ごと、相場は新品未開封・中古・箱なし・ジャンクに分ける。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import text
from .eras import MODERN, Era, detect_era, detect_game_era, detect_ygo_era
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
    # 転売時の送料を出品ごとに決める（ゲーム機の大きさなど）。なければ large_groups と large_shipping で決める
    shipping_of: Callable[[Listing, int], int] | None = None

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
        if self.shipping_of is not None:
            return self.shipping_of(listing, base)
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


def _shrink_box_group(normalized: str) -> str:
    """BOX ならシュリンクの有無で分け、BOX でなければ「カード以外」。"""
    if _BOX.search(normalized):
        if _SHRINK_NO.search(normalized):
            return "box_noshrink"
        if _SHRINK_YES.search(normalized):
            return "box_shrink"
        return "box"
    return "other"


def pokeca_group(title: str, condition: int | None) -> str:
    normalized = text.normalize(title)
    if _looks_like_card(normalized, title):
        grade = _grade(normalized)
        return f"{grade}_1ed" if _first_edition(normalized) else grade
    return _shrink_box_group(normalized)


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


# ---------------------------------------------------------------- 遊戯王

# カード番号: QCCU-JP001・RC04-JP010・20CP-JPS01 のような「弾の記号-JP番号」（日本語版）
_YGO_NUMBER = _p(r"(?<![a-z0-9])([a-z0-9]{2,5})\s*[-‐−–ー]\s*jp\s*([a-z]?\d{3}|[a-z]\d{2})(?![0-9])")
# {K} はカタカナと中黒（「スーパー・ヴィークロイド」のようなカード名の一部をレアリティと読まないため）
_K = "[ァ-ヺー・]"
# レアリティ。同じカード番号でもレアリティで値段が大きく違うので、別のカードとして相場を調べる。
# 上から順に判定し、見つかった部分は消してから次を調べる（「プリシク」を「シク」と読まないため）
_YGO_RARITIES = {
    label: _p(pattern.replace("{K}", _K))
    for label, pattern in (
        (
            "QCSE",
            r"(?<![a-z0-9])qcse(?![a-z0-9])|qc\s*(シク|シークレット|se)|ク[ォオ]ー?ター\s*センチュリー(\s*シークレット)?(\s*レア)?"
            r"|ク[ォオ]シク|25th\s*(シク|シークレット|se)(\s*レア)?",
        ),
        ("20thSE", r"20th\s*(シク|シークレット|se|ser)(\s*レア)?|(?<!\d)20\s*シク"),
        ("PSE", r"(?<![a-z0-9])psec?(?![a-z0-9])|プリシク|プリズマティック(\s*シークレット)?(\s*レア)?|プリズマ\s*(シク|シークレット)"),
        ("ESE", r"エクシク|エクストラ\s*シ(ク|ークレット)(\s*レア)?|(?<![a-z0-9])ese(?![a-z0-9])"),
        ("GSE", r"ゴルシク|ゴールド\s*シ(ク|ークレット)(\s*レア)?|(?<![a-z0-9])gse(?![a-z0-9])"),
        ("UPR", r"ウルパラ|ウルトラ\s*パラ(レル)?(\s*レア)?|(?<![a-z0-9])upr(?![a-z0-9])"),
        ("UL", r"レリーフ|アルティメット\s*レア|(?<!{K})アルティメット(?!{K})|(?<![a-z0-9])ul(?![a-z0-9])"),
        ("HR", r"ホログラフィック(\s*レア)?|(?<!{K})ホロ(?!{K})|(?<![a-z0-9])hr(?![a-z0-9])"),
        ("CR", r"コレクターズ(\s*レア)?|コレレア|(?<![a-z0-9])cr(?![a-z0-9])"),
        ("GR", r"ゴールド\s*レア|(?<![a-z0-9])gr(?![a-z0-9])"),
        ("KC", r"kc\s*(ウルトラ|レア)(\s*レア)?|(?<![a-z0-9])kc(?![a-z0-9])"),
        ("M", r"ミレニアム\s*レア"),
        ("SE", r"シークレット(\s*レア)?|(?<!{K})シク(?!{K})|(?<![a-z0-9])(se|ser|scr)(?![a-z0-9])"),
        ("UR", r"ウルトラ\s*レア|(?<!{K})ウルトラ(?!{K})|(?<![a-z0-9])ur(?![a-z0-9])"),
        ("SR", r"スーパー\s*レア|(?<!{K})スーパー(?!{K})|(?<![a-z0-9])sr(?![a-z0-9])"),
        ("NP", r"ノーパラ|ノーマル\s*パラ(レル)?(\s*レア)?|(?<!{K})パラ(レル)?(?!{K})|(?<![a-z0-9])np(?![a-z0-9])"),
        ("R", r"(?<!{K})レア(?!{K})"),
        ("N", r"(?<!{K})ノーマル(?!{K})"),
    )
}
_YGO_GENERIC_RARITIES = frozenset({"R", "N"})  # ほかのレアリティと一緒に書かれていれば無視する
_YGO_FOREIGN_NUMBER = r"(?<![a-z0-9])[a-z0-9]{2,5}\s*-\s*(en|ae|kr|de|fr|it|pt|sp)\d{3}(?![0-9])"

YUGIOH_GROUPS = {
    "raw": "未鑑定",
    "psa10": "PSA10",
    "psa9": "PSA9",
    "graded": "鑑定品（その他）",
    "box_shrink": "BOX（シュリンク付き）",
    "box_noshrink": "BOX（シュリンクなし）",
    "box": "BOX（シュリンク不明）",
    "other": "カード以外",
}


def _ygo_number(normalized: str) -> str | None:
    match = _YGO_NUMBER.search(normalized)
    return f"{match.group(1).upper()}-JP{match.group(2).upper()}" if match else None


def _ygo_rarity_labels(normalized: str) -> list[str]:
    """書かれているレアリティ（優先順）。"""
    labels = []
    for label, pattern in _YGO_RARITIES.items():
        if pattern.search(normalized):
            labels.append(label)
            normalized = pattern.sub(" ", normalized)
    return labels


def _ygo_rarity(normalized: str) -> str | None:
    """出品のレアリティ。違うレアリティが2つ以上書かれていれば（まとめ売りなど）None。"""
    labels = _ygo_rarity_labels(normalized)
    specific = [label for label in labels if label not in _YGO_GENERIC_RARITIES]
    if len(specific) > 1:
        return None
    return specific[0] if specific else (labels[0] if labels else None)


def yugioh_card_key(title: str) -> str | None:
    """どのカードか: カード番号（例: QCCU-JP001）とレアリティ（例: QCSE）。どちらかが分からなければ None。"""
    normalized = text.normalize(title)
    number, rarity = _ygo_number(normalized), _ygo_rarity(normalized)
    return f"{number} {rarity}" if number and rarity else None


def yugioh_sold_keyword(key: str, titles: list[str]) -> str:
    """売り切れを探すキーワード。レアリティは出品でいちばん多い書き方にする（「プリシク」と「PSE」など）。"""
    number, _, rarity = key.partition(" ")
    pattern = _YGO_RARITIES[rarity]
    words = Counter(match.group() for title in titles if (match := pattern.search(text.normalize(title))))
    if not words:
        return key
    word = words.most_common(1)[0][0]
    return f"{number} {word.upper() if word.isascii() else word}"


def _ygo_looks_like_card(normalized: str) -> bool:
    specific = set(_ygo_rarity_labels(normalized)) - _YGO_GENERIC_RARITIES
    return bool(_YGO_NUMBER.search(normalized) or specific or _GRADED.search(normalized))


def yugioh_group(title: str, condition: int | None) -> str:
    normalized = text.normalize(title)
    return _grade(normalized) if _ygo_looks_like_card(normalized) else _shrink_box_group(normalized)


def _ygo_unknown_card(title: str) -> str:
    return "レアリティが分からない" if _YGO_NUMBER.search(text.normalize(title)) else "カード番号なし"


def _ygo_kind_mismatch(title: str, query_tokens: list[str]) -> str | None:
    """カードを探しているのにBOXやサプライ品、BOXを探しているのに単品カード、
    カード番号やレアリティを指定しているのに別のカード・別のレアリティ、を外す。"""
    query = " ".join(query_tokens)
    normalized = text.normalize(title)
    if _looks_like_box(query):
        return "単品カード" if _ygo_looks_like_card(normalized) else None
    if _ygo_looks_like_card(query) and not _ygo_looks_like_card(normalized):
        return "BOX・パック" if _looks_like_box(normalized) else "カード以外・カードを特定できない"
    wanted_number = _ygo_number(query)
    if wanted_number is not None:
        number = _ygo_number(normalized)
        if number is not None and number != wanted_number:
            return "別のカード"
    # キーワードを複数まとめて探すときは、どれかのレアリティに当てはまれば残す
    wanted = set(_ygo_rarity_labels(query)) - _YGO_GENERIC_RARITIES
    rarity = _ygo_rarity(normalized)
    if wanted and rarity is not None and rarity not in _YGO_GENERIC_RARITIES and rarity not in wanted:
        return "別のレアリティ"
    return None


YUGIOH_RULES = POKECA_RULES + (
    _rule(r"アジア版|旧アジア|(?<![a-z])asia(?![a-z])", "海外版", "アジア"),
    _rule(_YGO_FOREIGN_NUMBER, "海外版"),
    _rule(r"ラッシュ\s*・?\s*デュエル|(?<![a-z])rd\s*/", "ラッシュデュエル（別のゲーム）", "ラッシュ"),
)

YUGIOH = Profile(
    name="yugioh",
    label="遊戯王",
    group_of=yugioh_group,
    group_labels=YUGIOH_GROUPS,
    strict_groups=True,
    exclusion_rules=YUGIOH_RULES,
    extra_exclusion=_ygo_kind_mismatch,
    strong_risk_words=POKECA.strong_risk_words,
    mild_risk_words=POKECA.mild_risk_words,
    defaults=dict(POKECA.defaults),
    card_key=yugioh_card_key,
    large_groups=BOX_GROUPS,
    large_shipping=520,
    era_of=detect_ygo_era,
    unknown_card_reason=_ygo_unknown_card,
    sold_keyword=yugioh_sold_keyword,
)


# ---------------------------------------------------------------- ゲーム機・ソフト

# 本体の出品だと分かる言葉。ソフトのタイトルにも「Switch 2」などの機種名は入るので、機種名だけでは本体とみなさない
_GAME_HW = _p(
    r"本体|ゲーム機|モデル|(?<![a-z])(hac|heg|hdh)-001|(?<![a-z])cfi-\d{4}|(?<![a-z])cuh-\d{4}"
    r"|(ジョイコン|joy-?con|ドック|コントローラー|付属品)\s*(付|つき|あり|有|完備|セット|一式)"
    r"|steam\s*deck|スチーム\s*デック|rog\s*ally"  # パッケージのソフトがない機種は名前だけで本体とみなす
)
_GAME_SW = _p(r"ソフト|カセット|パッケージ|ケース\s*(なし|無し)|箱説")
_GAME_ACC = _p(
    r"カバー|ケース|フィルム|保護|ジョイコン|joy-?con|コントローラー|プロコン|ドック|アダプター|充電|ケーブル|スタンド"
    r"|グリップ|ポーチ|バッグ|スキン|ステッカー|microsd|sdカード|ヘッドセット|イヤホン|キーボード|amiibo|アミーボ"
    r"|ハンドル|ホイール"
)
_GAME_NEW = _p(r"新品(?!\s*同様)|未開封|未使用(?!\s*に近い)")
_GAME_JUNK = _p(r"ジャンク|故障|動作不良|部品取り|起動しない|電源\s*(が)?\s*入らない|通電のみ")
_GAME_LOOSE = {
    "hw": _p(r"本体\s*(のみ|だけ)|箱\s*(なし|無し|欠品)|付属品\s*(なし|無し)"),
    "sw": _p(r"(ソフト|カセット)\s*(のみ|だけ)|ケース\s*(なし|無し)|箱説?\s*(なし|無し)|(?<![ァ-ヺー])裸"),
}
GAME_GROUPS = {
    "hw_new": "本体・新品未開封",
    "hw_used": "本体・中古",
    "hw_loose": "本体・箱や付属品なし",
    "hw_junk": "本体・ジャンク",
    "sw_new": "ソフト・新品未開封",
    "sw_used": "ソフト・中古",
    "sw_loose": "ソフト・箱やケースなし",
    "sw_junk": "ソフト・ジャンク",
    "other": "周辺機器など",
}
_GAME_KIND_LABELS = {"hw": "本体（ソフトではない）", "sw": "ソフト（本体ではない）", "acc": "周辺機器・アクセサリー"}

# モデル。同じ機種でもモデル（有機EL・Lite・PS5 のディスク版/デジタル版 など）で値段が違うので、別の商品として相場を調べる
_SWITCH_FAMILY = _p(r"switch|スイッチ|(?<![a-z])(hac|heg|hdh)-001")
_SWITCH_MODELS = (
    ("Switch 2", _p(r"(switch|スイッチ)\s*2(?![\d台個本])")),
    ("Switch 有機EL", _p(r"有機\s*el|oled|(?<![a-z])heg-001")),
    ("Switch Lite", _p(r"(?<![a-z])lite(?![a-z])|(?<![ァ-ヺー])ライト(?![ァ-ヺー])|(?<![a-z])hdh-001")),
    ("Switch 新型", _p(r"新型|新モデル|バッテリー\s*(強化|持続|長持ち)|hac-001\s*\(?\s*-\s*01|(?<![a-z])xkj")),
    ("Switch 旧型", _p(r"旧型|初期型|(?<![a-z])xaj|hac-001(?!\s*\(?\s*-\s*01)")),
)
_PS5_FAMILY = _p(r"ps\s*5(?!\d)|playstation\s*5|プレイステーション\s*5|プレステ\s*5|(?<![a-z])cfi-\d{4}")
_PS4_FAMILY = _p(r"ps\s*4(?!\d)|playstation\s*4|プレイステーション\s*4|プレステ\s*4|(?<![a-z])cuh-\d{4}")
_GAME_PRO = _p(r"(?<![a-z])pro(?![a-z])|(?<![ァ-ヺー])プロ(?![ァ-ヺー])|cfi-7\d{3}|cuh-7\d{3}")
_PS_DIGITAL = _p(r"デジタル|digital|cfi-\d{4}\s*b")
_PS_DISC = _p(r"ディスク|disc|ドライブ搭載|cfi-\d{4}\s*a")
_PS5_SLIM = _p(r"cfi-2\d{3}|slim|スリム|新型|軽量|小型")
_PS5_FIRST = _p(r"cfi-1[0-2]\d{2}|初期型|旧型")
_PS4_SLIM = _p(r"cuh-2\d{3}|slim|スリム|薄型")
_PS4_FIRST = _p(r"cuh-1\d{3}|初期型|旧型")
_XBOX_X = _p(r"xbox\s*series\s*x")
_XBOX_S = _p(r"xbox\s*series\s*s")
_STEAM_DECK = _p(r"steam\s*deck|スチーム\s*デック")
_STEAM_OLED = _p(r"oled|有機\s*el")
_STEAM_LCD = _p(r"lcd|液晶")
# 売り切れを探すキーワード（モデルの見分けは手元でするので、取りこぼしの少ない言葉にする）
_GAME_SEARCH_WORDS = {
    "Switch 2": "Nintendo Switch 2",
    "Switch 有機EL": "Nintendo Switch 有機EL",
    "Switch Lite": "Nintendo Switch Lite",
    "Switch 新型": "Nintendo Switch 新型",
    "Switch 旧型": "Nintendo Switch 旧型",
    "PS5 Pro": "PS5 Pro",
    "PS4 Pro": "PS4 Pro",
    "Xbox Series X": "Xbox Series X",
    "Xbox Series S": "Xbox Series S",
}
_BIG_CONSOLE = _p(
    r"ps\s*[345](?!\d)|playstation\s*[345]|プレステ\s*[345]|プレイステーション\s*[345]|xbox\s*(series\s*x|one|360)"
    r"|wii\s*u|ゲームキューブ"
)
GAME_CONSOLE_SHIPPING = 1000  # 宅急便 60〜80サイズ程度（Switch・携帯機・レトロ機）
GAME_BIG_CONSOLE_SHIPPING = 1500  # 100〜140サイズ程度（PS5 などの据え置き機）


def game_model_key(title: str) -> str | None:
    """ゲーム機のモデル（例: Switch 有機EL, PS5 新型 ディスク）。分からなければ None。"""
    normalized = text.normalize(title)
    if _SWITCH_FAMILY.search(normalized):
        return next((key for key, pattern in _SWITCH_MODELS if pattern.search(normalized)), None)
    if _PS5_FAMILY.search(normalized):
        if _GAME_PRO.search(normalized):
            return "PS5 Pro"
        generation = "新型" if _PS5_SLIM.search(normalized) else "初期型" if _PS5_FIRST.search(normalized) else None
        edition = "デジタル" if _PS_DIGITAL.search(normalized) else "ディスク" if _PS_DISC.search(normalized) else None
        return f"PS5 {generation} {edition}" if generation and edition else None
    if _PS4_FAMILY.search(normalized):
        if _GAME_PRO.search(normalized):
            return "PS4 Pro"
        return "PS4 薄型" if _PS4_SLIM.search(normalized) else "PS4 初期型" if _PS4_FIRST.search(normalized) else None
    if _XBOX_X.search(normalized):
        return "Xbox Series X"
    if _XBOX_S.search(normalized):
        return "Xbox Series S"
    if _STEAM_DECK.search(normalized):
        if _STEAM_OLED.search(normalized):
            return "Steam Deck OLED"
        return "Steam Deck LCD" if _STEAM_LCD.search(normalized) else None
    return None


def _game_kind(normalized: str) -> str:
    """hw（本体）/ sw（ソフト）/ acc（周辺機器・アクセサリーだけ）。"""
    if _GAME_HW.search(normalized):
        return "hw"
    if _GAME_SW.search(normalized):
        return "sw"
    if _GAME_ACC.search(normalized):
        return "acc"
    return "sw"  # 「マリオカート8 デラックス」のように、タイトルだけのものはソフト


def game_group(title: str, condition: int | None) -> str:
    normalized = text.normalize(title)
    kind = _game_kind(normalized)
    if kind == "acc":
        return "other"
    if _GAME_JUNK.search(normalized):
        state = "junk"
    elif condition == 1 or _GAME_NEW.search(normalized):
        state = "new"
    elif _GAME_LOOSE[kind].search(normalized):
        state = "loose"
    else:
        state = "used"
    return f"{kind}_{state}"


def game_sold_keyword(key: str, titles: list[str]) -> str:
    if key in _GAME_SEARCH_WORDS:
        return _GAME_SEARCH_WORDS[key]
    family = key.split()[0]
    return {"PS5": "PS5 本体", "PS4": "PS4 本体", "Steam": "Steam Deck"}.get(family, key)


def game_shipping(listing: Listing, base: int) -> int:
    """売るときの送料。ソフトは base（ネコポスなど）、本体は大きさで決める。"""
    normalized = text.normalize(listing.title)
    if _game_kind(normalized) != "hw":
        return base
    return max(base, GAME_BIG_CONSOLE_SHIPPING if _BIG_CONSOLE.search(normalized) else GAME_CONSOLE_SHIPPING)


def _game_unknown_model(title: str) -> str:
    return "モデルが分からない"


def _game_kind_mismatch(title: str, query_tokens: list[str]) -> str | None:
    """本体を探しているのにソフトや周辺機器、ソフトを探しているのに本体、
    モデルを指定しているのに別のモデル（またはモデルが分からないもの）、を外す。"""
    query = " ".join(query_tokens)
    normalized = text.normalize(title)
    wanted_model = game_model_key(query)
    if wanted_model or _GAME_HW.search(query):
        wanted_kind = "hw"
    elif _GAME_ACC.search(query) and not _GAME_SW.search(query):
        wanted_kind = "acc"
    else:
        wanted_kind = "sw"
    kind = _game_kind(normalized)
    if kind != wanted_kind:
        return _GAME_KIND_LABELS[kind]
    if wanted_model is not None:
        model = game_model_key(title)
        if model is None:
            return "モデルが分からない"
        if model != wanted_model:
            return "別のモデル"
    return None


GAME_RULES = (
    _rule(
        r"ダウンロード\s*(版|コード|番号)|(?<![a-z])dl\s*(版|コード|カード)|プロダクト\s*コード|コード\s*(のみ|だけ|通知)"
        r"|シリアル\s*コード",
        "ダウンロード版・コード",
        "ダウンロード",
    ),
    _rule(
        r"アカウント\s*(付|つき|販売|譲渡|売り)|(?<![a-z])ban(?![a-z])(?!\s*(なし|無し|歴なし|されていない|ではない|対策))",
        "アカウント・BANの問題",
        "アカウント",
    ),
    _rule(r"海外版|北米版|欧州版|輸入版|中国版|香港版|韓国版|アジア版|台湾版|(?<![a-z])(us|eu|uk)\s*版", "海外版", "海外"),
    _rule(r"予約\s*(品|中|販売|受付)|発売前|抽選\s*(券|応募|権)", "予約品・抽選（手元にない商品）"),
    _rule(
        r"(?<!お)まとめ(?!\s*(買|割|可|歓迎|値引))|大量|\d+\s*本\s*セット|(ソフト|カセット)\s*\d+\s*本|セット売り",
        "まとめ売り・セット",
        "まとめ",
    ),
    _rule(r"(ソフト|カセット)\s*(付|つき|セット)|同梱(?!\s*(なし|無し|物|品))", "本体とソフトのセット", "セット"),
    _rule(
        r"(ドック|ジョイコン|joy-?con|コントローラー|プロコン|アダプター|ケーブル|充電器|スタンド|フィルム|ポーチ|カバー)\s*(のみ|だけ)",
        "周辺機器のみ",
        "周辺",
    ),
    _rule(r"本体\s*(なし|無し)", "本体なし"),
    _rule(r"クラシック\s*ミニ|(?<![ァ-ヺー])ミニ(?![ァ-ヺー])", "ミニ（復刻版）", "ミニ"),
    _rule(r"改造", "改造品", "改造"),
)

GAME = Profile(
    name="game",
    label="ゲーム機・ソフト",
    group_of=game_group,
    group_labels=GAME_GROUPS,
    strict_groups=True,
    exclusion_rules=GAME_RULES,
    extra_exclusion=_game_kind_mismatch,
    strong_risk_words=(
        "液晶割れ", "画面割れ", "割れ", "ヒビ", "読み込み不良", "読み込まない", "読み込みエラー",
        "バッテリー膨張", "膨張", "充電できない", "充電不良", "ボタン不良", "スティック不良", "通電のみ",
    ),
    mild_risk_words=(
        "ドリフト", "勝手に動く", "ドット抜け", "焼け", "黄ばみ", "日焼け", "傷あり", "傷有", "キズあり",
        "擦り傷", "スレ", "異音", "使用感", "汚れあり",
    ),
    defaults={
        "resale": True,
        "resale_shipping": 230,  # ソフト（ネコポス 210円 + 資材）。本体は大きさで 1,000〜1,500円
        "min_profit": 1000,
        # 「マリオカート ワールド」で「マリオカート8」が混ざらないよう、キーワードの4分の3以上がタイトルにあるものだけ
        "min_match": 0.75,
    },
    card_key=game_model_key,
    era_of=detect_game_era,
    unknown_card_reason=_game_unknown_model,
    sold_keyword=game_sold_keyword,
    shipping_of=game_shipping,
)


PROFILES = {"default": DEFAULT, "pokeca": POKECA, "onepiece": ONEPIECE, "yugioh": YUGIOH, "game": GAME}
ALIASES = {
    "一般": "default",
    "pokemon": "pokeca",
    "ポケカ": "pokeca",
    "ポケモンカード": "pokeca",
    "one_piece": "onepiece",
    "ワンピース": "onepiece",
    "ワンピースカード": "onepiece",
    "ワンピカ": "onepiece",
    "遊戯王": "yugioh",
    "遊戯王カード": "yugioh",
    "遊戯王ocg": "yugioh",
    "ygo": "yugioh",
    "games": "game",
    "ゲーム": "game",
    "ゲーム機": "game",
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

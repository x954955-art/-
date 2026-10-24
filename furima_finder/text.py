"""タイトル・説明文の正規化と、キーワードによる判定ルール。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

_HIRA_TO_KATA = {code: code + 0x60 for code in range(0x3041, 0x3097)}


def normalize(value: str) -> str:
    """全角/半角・大文字/小文字・ひらがな/カタカナの違いを吸収する。"""
    return unicodedata.normalize("NFKC", value).lower().translate(_HIRA_TO_KATA)


def tokens(query: str) -> list[str]:
    return normalize(query).split()


def match_ratio(query_tokens: list[str], title: str) -> float:
    """検索語のうちタイトルに含まれる語の割合（0〜1）。"""
    if not query_tokens:
        return 1.0
    normalized = normalize(title)
    compact = normalized.replace(" ", "")
    hits = sum(1 for token in query_tokens if token in normalized or token in compact)
    return hits / len(query_tokens)


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """判定対象と同じ正規化をパターンにもかけてコンパイルする（大文字のエスケープは使わないこと）。"""
    return re.compile(normalize(pattern))


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern[str]
    label: str
    # 検索語にこの語が含まれていればルールを適用しない（例: ケースを探しているなら「ケースのみ」は除外しない）
    unless_query_has: str = ""

    def applies(self, normalized_text: str, query_tokens: list[str]) -> re.Match[str] | None:
        if self.unless_query_has:
            skip = normalize(self.unless_query_has)
            if any(skip in token for token in query_tokens):
                return None
        return self.pattern.search(normalized_text)


def rule(pattern: str, label: str, unless_query_has: str = "") -> Rule:
    return Rule(compile_pattern(pattern), label, unless_query_has)


# 特定の人向け・購入できない出品（相場には含めるが、購入候補からは外す）
RESERVED_RULES = [
    rule(r"(様|さま|さん|ちゃん)\s*専用", "専用・購入不可"),
    rule(r"専用\s*(出品|ページ|です)", "専用・購入不可"),
    rule(r"^\s*[【\[(]\s*専用\s*[】\])]", "専用・購入不可"),
    rule(r"^\s*専用(\s|$|!)", "専用・購入不可"),
    rule(r"購入(不可|禁止|しないで)", "専用・購入不可"),
    rule(r"取り?置き", "専用・購入不可"),
    rule(r"(予約|売約)済", "専用・購入不可"),
]

# 商品そのものではない出品（相場にも購入候補にも使わない）
ACCESSORY_ONLY_RULES = [
    rule(r"(空|外|化粧)?箱(のみ|だけ)|空箱", "箱・付属品のみ", "箱"),
    rule(r"説明書(のみ|だけ)", "箱・付属品のみ", "説明書"),
    rule(r"ケース(のみ|だけ)", "箱・付属品のみ", "ケース"),
    rule(r"カバー(のみ|だけ)", "箱・付属品のみ", "カバー"),
    rule(r"付属品(のみ|だけ)", "箱・付属品のみ", "付属品"),
]
NOT_THE_ITEM_RULES = [
    rule(r"(写真|画像)(のみ|だけ)", "写真のみ", "写真"),
    rule(r"レンタル", "レンタル", "レンタル"),
]

# 状態に問題がありそうな出品（購入候補には残すが減点する）
STRONG_RISK_WORDS = [
    "ジャンク", "部品取り", "故障", "不動", "動作不良", "破損", "水没",
    "起動しない", "電源が入らない", "電源入らない", "通電しない",
]
MILD_RISK_WORDS = [
    "動作未確認", "未確認", "訳あり", "訳有", "わけあり", "難あり", "難有",
    "欠品", "現状品", "ノークレーム", "ノーリターン", "ncnr",
]

# 直後に続くと否定になる言い回し。「折れや水濡れはありません」「ジャンク品ではありません」
# 「折れもなく」のような形に対応する。「初期傷はありますが」「白欠けが少ない」は否定ではない
# （pattern.match(text, pos) で使うので先頭の ^ は不要）
_NEGATION = compile_pattern(
    r"((や|・|/|と|、)\s*[^。、\s]{1,8}?)*"  # 「〜や水濡れ」のような並び
    r"\s*(品|跡|等|など|類)?\s*(で|じゃ)?\s*(は|も|が)?\s*(一切|特に|全く|まったく)?\s*"
    r"(なし|無し|ない|無い|なく|ありません|ございません|ていない|ていません|ておりません"
    r"|していません|しておりません|見当たりません)"
)
# 「本体と箱のみ」のように本体が付いている出品は「箱のみ」扱いしない
_HAS_MAIN_UNIT = compile_pattern(r"本体(?!\s*(なし|無し|無|ナシ))")


def exclusion_reason(
    title: str,
    query_tokens: list[str],
    *,
    include_reserved: bool = True,
    extra_rules: Iterable[Rule] = (),
) -> str | None:
    """購入候補から外すべき出品ならその理由を返す。extra_rules はジャンル別の追加ルール。"""
    normalized = normalize(title)
    rules = list(extra_rules) + NOT_THE_ITEM_RULES
    if not _HAS_MAIN_UNIT.search(normalized):
        rules += ACCESSORY_ONLY_RULES
    if include_reserved:
        rules += RESERVED_RULES
    for rule in rules:
        if rule.applies(normalized, query_tokens):
            return rule.label
    return None


def _find_words(
    normalized_text: str, words: Iterable[str], query_tokens: list[str], *, skip_negated: bool
) -> list[str]:
    found = []
    for word in words:
        needle = normalize(word)
        if any(needle in token for token in query_tokens):
            continue
        for match in re.finditer(re.escape(needle), normalized_text):
            if skip_negated and _NEGATION.match(normalized_text, match.end()):
                continue
            found.append(word)
            break
    return found


def risk_level(
    title: str,
    query_tokens: list[str],
    *,
    strong_extra: Iterable[str] = (),
    mild_extra: Iterable[str] = (),
) -> tuple[int, str | None]:
    """タイトルのリスク度。2=ジャンク等、1=動作未確認等、0=なし。"""
    normalized = normalize(title)
    strong = _find_words(normalized, [*STRONG_RISK_WORDS, *strong_extra], query_tokens, skip_negated=True)
    if strong:
        return 2, strong[0]
    mild = _find_words(normalized, [*MILD_RISK_WORDS, *mild_extra], query_tokens, skip_negated=True)
    if mild:
        return 1, mild[0]
    return 0, None


def description_risks(description: str, query_tokens: list[str], *, extra_words: Iterable[str] = ()) -> list[str]:
    """説明文に出てくる注意ワード（「ジャンクではありません」のような否定は除く）。"""
    normalized = normalize(description)
    words = _find_words(
        normalized, [*STRONG_RISK_WORDS, *MILD_RISK_WORDS, *extra_words], query_tokens, skip_negated=True
    )
    # 「動作未確認」と「未確認」のように重なるものは長いほうだけ残す
    return [w for w in words if not any(w != other and normalize(w) in normalize(other) for other in words)]

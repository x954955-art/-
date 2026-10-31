"""開示タイトルからイベント種別（キーワード）を判定する。

- タイトルは NFKC 正規化してから照合する（全角/半角の揺れを吸収）
- 1つの開示が複数キーワードに該当することがある（例: 「店舗閉鎖及び特別損失の計上」）
- 訂正・経過開示・中止などの「初回の悪材料ではない」開示は除外する
"""
from __future__ import annotations

import re
import unicodedata

# 表示順はユーザー指定の順
KEYWORD_PATTERNS: dict[str, list[str]] = {
    "事業撤退": [r"撤退"],
    "事業譲渡": [r"事業(?:の一部)?の?譲渡"],
    "店舗閉鎖": [r"閉店", r"店舗[^、。]{0,8}閉鎖"],
    "子会社の解散": [r"(?:子|孫)会社[^、。]{0,15}(?:解散|清算)"],
    "特別損失": [r"特別損失"],
    "減損": [r"減損"],
    "構造改革": [r"構造改革"],
    "価格改定": [r"価格の?改[定訂]", r"料金の?改[定訂]", r"値上げ", r"価格の?引き?上げ"],
}
KEYWORDS: list[str] = list(KEYWORD_PATTERNS)

# 初回の悪材料開示ではないもの
EXCLUDE_PATTERNS: list[str] = [
    r"訂正",  # 訂正開示（元の開示と二重計上になる）
    r"開示事項の経過",  # 経過開示
    r"結了",  # 清算結了など手続き完了の報告
    # イベント自体の中止・延期・撤回
    r"(?:譲渡|閉鎖|閉店|解散|清算|撤退|改定|改訂|値上げ|構造改革)[^、。]{0,6}(?:中止|取り?止め|撤回|延期)",
]

_KW_RE = {k: [re.compile(p) for p in ps] for k, ps in KEYWORD_PATTERNS.items()}
_EX_RE = [re.compile(p) for p in EXCLUDE_PATTERNS]


def normalize(title: str) -> str:
    return unicodedata.normalize("NFKC", title or "").strip()


def is_excluded(title: str) -> bool:
    t = normalize(title)
    return any(r.search(t) for r in _EX_RE)


def match_keywords(title: str, apply_exclusions: bool = True) -> tuple[str, ...]:
    """該当するキーワードのタプル（該当なし・除外対象なら空）。"""
    t = normalize(title)
    if not t or (apply_exclusions and any(r.search(t) for r in _EX_RE)):
        return ()
    return tuple(k for k, regs in _KW_RE.items() if any(r.search(t) for r in regs))

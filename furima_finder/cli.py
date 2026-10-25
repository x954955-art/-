"""コマンドラインの入口。"""

from __future__ import annotations

import argparse
import os
import sys
import time
import tomllib
import webbrowser
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

import requests

from . import __version__
from .finder import CONDITION_FILTERS, SearchPlan, SearchResult, run_plan
from .http import HttpClient
from .notify import format_message, send_webhook
from .probe import run_probe
from .profiles import PROFILES, get_profile
from .report import JST, item_lines, print_result, write_csv, write_html, write_json
from .scoring import Settings
from .sources import AVAILABLE, Source, create_sources

MIN_WATCH_MINUTES = 5.0
MIN_REQUEST_INTERVAL = 1.0

DEFAULTS: dict[str, Any] = {
    "exclude": [],
    "min_price": None,
    "max_price": None,
    "condition": "any",
    "sources": ["mercari"],
    "pages": 2,
    "sold_pages": 2,
    "market_price": None,
    "details": 10,
    "floor_ratio": 0.3,
    "min_discount": 0.10,
    "min_match": 0.5,
    "buyer_shipping": 800,
    "resale": False,
    "fee_rate": 0.10,
    "resale_shipping": 750,
    "min_profit": 500,
    "min_roi": 0.15,
    "min_sales": 3,
    "profile": "default",
    "scan": False,
    "scan_cards": 20,
}

EXAMPLES = """\
例:
  python -m furima_finder "Nintendo Switch 有機EL" --min-price 15000
  python -m furima_finder "iPad mini 6 64GB" --max-price 45000 --condition good --html report.html

  # ポケモンカード: 転売して利益が出るカードを探す
  python -m furima_finder --profile pokeca --scan "ポケモンカード SAR" --max-price 30000 --html pokeca.html
  python -m furima_finder --profile pokeca "リザードンex SAR 201/165"
  python -m furima_finder --config watchlist.pokeca.example.toml --watch 15 --webhook https://discord.com/api/webhooks/...

  # ワンピースカード: パラレル・SEC などのカードを、カード番号と種類ごとに相場を調べて探す
  python -m furima_finder --profile onepiece --scan "ワンピースカード パラレル" --max-price 50000 --html onepiece.html
  python -m furima_finder --profile onepiece "シャンクス SEC パラレル OP01-120"
"""


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------- 値の変換

def parse_ratio(value: Any) -> float:
    """0.15 / 15 / "15%" のどれでも 0.15（15%）として受け付ける。"""
    if isinstance(value, bool):
        raise ValueError(value)
    if isinstance(value, str):
        raw = value.strip()
        number = float(raw.rstrip("%"))
        if raw.endswith("%") or number >= 1:
            number /= 100
    else:
        number = float(value)
        if number >= 1:
            number /= 100
    if not 0 <= number < 1:
        raise ValueError(value)
    return number


def _fraction(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(value)
    number = float(value)
    if not 0 <= number <= 1:
        raise ValueError(value)
    return number


def _non_negative(value: Any) -> int:
    """0以上の整数。金額は "1,000円" のような書き方も受け付ける。"""
    if isinstance(value, bool):
        raise ValueError(value)
    if isinstance(value, str):
        value = value.replace(",", "").replace("円", "").replace("¥", "").strip()
    number = int(value)
    if number < 0:
        raise ValueError(value)
    return number


def _positive(value: Any) -> int:
    number = _non_negative(value)
    if number < 1:
        raise ValueError(value)
    return number


def _words(value: Any) -> list[str]:
    if isinstance(value, str):
        return value.split()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [word for item in value for word in item.split()]
    raise ValueError(value)


def _sources(value: Any) -> list[str]:
    if isinstance(value, str):
        names = [name.strip() for name in value.split(",") if name.strip()]
    elif isinstance(value, list):
        names = [str(name).strip() for name in value]
    else:
        raise ValueError(value)
    if not names or any(name not in AVAILABLE for name in names):
        raise ValueError(value)
    return list(dict.fromkeys(names))


def _condition(value: Any) -> str:
    if value not in CONDITION_FILTERS:
        raise ValueError(value)
    return value


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(value)
    return value


def _profile(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(value)
    return get_profile(value.strip()).name


COERCE: dict[str, Callable[[Any], Any]] = {
    "exclude": _words,
    "min_price": _non_negative,
    "max_price": _non_negative,
    "condition": _condition,
    "sources": _sources,
    "pages": _positive,
    "sold_pages": _positive,
    "market_price": _non_negative,
    "details": _non_negative,
    "floor_ratio": parse_ratio,
    "min_discount": parse_ratio,
    "min_match": _fraction,
    "buyer_shipping": _non_negative,
    "resale": _bool,
    "fee_rate": parse_ratio,
    "resale_shipping": _non_negative,
    "min_profit": _non_negative,
    "min_roi": parse_ratio,
    "min_sales": _non_negative,
    "profile": _profile,
    "scan": _bool,
    "scan_cards": _positive,
}


# ---------------------------------------------------------------- 設定ファイル

def _coerce_entry(entry: Any, where: str) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ConfigError(f"{where} の書き方が正しくありません")
    values: dict[str, Any] = {}
    for key, value in entry.items():
        name = key.replace("-", "_")
        if name == "keyword":
            # 1つなら "…"、まとめて探すなら ["…", "…"]
            words = value if isinstance(value, list) else [value]
            if not words or not all(isinstance(word, str) and word.strip() for word in words):
                raise ConfigError(f"{where}: keyword が空です")
            values["keyword"] = [word.strip() for word in words] if isinstance(value, list) else value.strip()
            continue
        if name not in COERCE:
            raise ConfigError(f"{where}: 不明な項目「{key}」があります")
        try:
            values[name] = COERCE[name](value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{where}: {key} の値が正しくありません（{value!r}）") from exc
    return values


def load_config(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        # メモ帳が付けることのある BOM は読み飛ばす
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ConfigError(f"設定ファイルが見つかりません: {path}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"設定ファイル {path} を UTF-8 で保存し直してください（メモ帳なら「名前を付けて保存」で文字コードを UTF-8 に）"
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"設定ファイルの書式が正しくありません: {exc}") from exc
    unknown = sorted(set(data) - {"defaults", "search"})
    if unknown:
        raise ConfigError(f"設定ファイルに不明な項目があります: {', '.join(unknown)}")
    defaults = _coerce_entry(data.get("defaults", {}), "[defaults]")
    if "keyword" in defaults:
        raise ConfigError("[defaults] には keyword を書けません（[[search]] に書いてください）")
    entries = data.get("search", [])
    if not isinstance(entries, list):
        raise ConfigError("検索条件は [[search]] の形で書いてください")
    searches = []
    for index, entry in enumerate(entries, 1):
        values = _coerce_entry(entry, f"[[search]] の{index}件目")
        if "keyword" not in values:
            raise ConfigError(f"[[search]] の{index}件目に keyword がありません")
        searches.append(values)
    return defaults, searches


def make_plan(values: Mapping[str, Any], keywords: list[str]) -> SearchPlan:
    settings = Settings(
        min_discount=values["min_discount"],
        min_match=values["min_match"],
        buyer_shipping=values["buyer_shipping"],
        condition_filtered=values["condition"] != "any",
        resale=values["resale"],
        fee_rate=values["fee_rate"],
        resale_shipping=values["resale_shipping"],
        min_profit=values["min_profit"],
        min_roi=values["min_roi"],
        min_sales=values["min_sales"],
    )
    return SearchPlan(
        keyword=keywords[0],
        extra_keywords=list(keywords[1:]),
        exclude=list(values["exclude"]),
        min_price=values["min_price"] or None,
        max_price=values["max_price"] or None,
        condition=values["condition"],
        sources=list(values["sources"]),
        pages=values["pages"],
        sold_pages=values["sold_pages"],
        market_price=values["market_price"] or None,
        details=values["details"],
        floor_ratio=values["floor_ratio"],
        profile=values["profile"],
        scan=values["scan"],
        scan_cards=values["scan_cards"],
        settings=settings,
    )


def build_plans(args: argparse.Namespace) -> list[SearchPlan]:
    """組み込みの既定値 < プロファイルの既定値 < 設定ファイルの [defaults] < 各 [[search]] < コマンドライン
    の順で上書きする。"""
    defaults, searches = load_config(args.config) if args.config else ({}, [])
    cli = {name: getattr(args, name) for name in COERCE if getattr(args, name, None) is not None}
    entries = [{"keyword": keyword.strip()} for keyword in args.keywords if keyword.strip()] + searches
    if not entries:
        raise ConfigError("検索キーワードか --config を指定してください")
    plans = []
    for entry in entries:
        profile = get_profile(cli.get("profile") or entry.get("profile") or defaults.get("profile") or "default")
        values = {**DEFAULTS, **profile.defaults, **defaults, **entry, **cli, "profile": profile.name}
        if values["scan"] and profile.card_key is None:
            raise ConfigError("--scan は --profile pokeca か --profile onepiece と一緒に使ってください")
        # 除外キーワードは上書きせず足し合わせる
        values["exclude"] = list(
            dict.fromkeys(defaults.get("exclude", []) + entry.get("exclude", []) + cli.get("exclude", []))
        )
        keywords = values["keyword"] if isinstance(values["keyword"], list) else [values["keyword"]]
        if values["scan"]:
            plans.append(make_plan(values, keywords))  # まとめて1回のスキャンにする
        else:
            plans.extend(make_plan(values, [keyword]) for keyword in keywords)
    return plans


# ---------------------------------------------------------------- 引数

def _arg(coerce: Callable[[Any], Any], hint: str) -> Callable[[str], Any]:
    def convert(value: str) -> Any:
        try:
            return coerce(value)
        except (TypeError, ValueError) as exc:
            raise argparse.ArgumentTypeError(f"{hint}で指定してください（{value!r}）") from exc

    convert.__name__ = coerce.__name__
    return convert


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="furima-finder",
        description="メルカリ・ラクマの出品から、売り切れ価格（相場）より安い「買い」候補を探して点数順に並べます。",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("keywords", nargs="*", metavar="キーワード", help="検索キーワード（複数指定すると順に検索）")
    parser.add_argument("-c", "--config", type=Path, metavar="FILE", help="検索条件を書いたTOMLファイル（watchlist.example.toml 参照）")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    search = parser.add_argument_group("検索条件")
    search.add_argument("--exclude", action="extend", type=_arg(_words, "語"), metavar="語", help="除外キーワード（スペース区切り・複数回指定可）")
    search.add_argument("--min-price", type=_arg(_non_negative, "0以上の整数"), metavar="円", help="最低価格（付属品やソフトなど安いものを除くのに便利）")
    search.add_argument("--max-price", type=_arg(_non_negative, "0以上の整数"), metavar="円", help="最高価格（予算）")
    search.add_argument(
        "--condition",
        choices=list(CONDITION_FILTERS),
        help="商品の状態の下限: new=新品・未使用 / near-new=未使用に近い以上 / good=目立った傷や汚れなし以上 / fair=やや傷や汚れあり以上 / any=指定なし（既定）",
    )
    search.add_argument("--sources", type=_arg(_sources, f"{','.join(AVAILABLE)} のカンマ区切り"), metavar="サイト", help="検索するサイト（mercari,rakuma,yahoo,snkrdunk）。既定は mercari。rakuma・yahoo（Yahoo!フリマ）・snkrdunk（スニダン）は実験的")
    search.add_argument("--pages", type=_arg(_positive, "1以上の整数"), metavar="N", help="販売中の出品を何ページ取るか（1ページ最大120件、既定 2）")
    search.add_argument("--sold-pages", type=_arg(_positive, "1以上の整数"), metavar="N", help="相場計算用の売り切れを何ページ取るか（既定 2）")
    search.add_argument(
        "--profile",
        type=_arg(_profile, f"{' / '.join(PROFILES)} のどれか"),
        metavar="種類",
        help="商品ジャンル: default（一般）/ pokeca（ポケモンカード: PSA10・未鑑定・BOXを分けて相場を出し、"
        "オリパ・海外版・まとめ売り等を除外。転売目線が既定）/ onepiece（ワンピースカード: カード番号と"
        "パラレル・コミパラ・SP などの違いでカードを見分ける。転売目線が既定）",
    )
    search.add_argument(
        "--scan",
        action="store_true",
        default=None,
        help="キーワードを「探す範囲」として使い、見つかったカードごとに相場を調べる（--profile pokeca・onepiece 用。"
        "例: --scan \"ポケモンカード SAR\"。既定で1,000円未満は除く）",
    )
    search.add_argument("--scan-cards", type=_arg(_positive, "1以上の整数"), metavar="N", help="スキャンで相場を調べるカードの種類数（既定 20）")

    judge = parser.add_argument_group("判定")
    judge.add_argument("--min-discount", type=_arg(parse_ratio, "0.15・15・15% のような形"), metavar="割合", help="相場より何%%以上安ければ候補にするか（既定 10%%）")
    judge.add_argument("--market-price", type=_arg(_non_negative, "0以上の整数"), metavar="円", help="相場を自分で指定する（売り切れから計算しない）")
    judge.add_argument("--min-match", type=_arg(_fraction, "0〜1の数"), metavar="0〜1", help="キーワードのうちタイトルに含まれているべき割合（既定 0.5、0 で判定しない）")
    judge.add_argument("--buyer-shipping", type=_arg(_non_negative, "0以上の整数"), metavar="円", help="着払いの出品に足す想定送料（既定 800円）")
    judge.add_argument("--details", type=_arg(_non_negative, "0以上の整数"), metavar="N", help="上位N件の出品者評価・説明文を確認する（既定 10、0 で確認しない）")
    judge.add_argument("--floor-ratio", type=_arg(parse_ratio, "0.3・30% のような形"), metavar="割合", help="相場のこの割合より安い出品は付属品等とみなして取らない（既定 30%%）")

    output = parser.add_argument_group("出力")
    output.add_argument("--top", type=_arg(_positive, "1以上の整数"), default=20, metavar="N", help="キーワードごとの表示件数（既定 20）")
    output.add_argument("--html", type=Path, metavar="FILE", help="写真付きのHTMLレポートを保存する")
    output.add_argument("--csv", type=Path, metavar="FILE", help="CSV（Excel向け）で保存する")
    output.add_argument("--json", type=Path, metavar="FILE", help="JSONで保存する")
    resale = parser.add_argument_group("転売目線")
    resale.add_argument(
        "--resale",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="相場で売った場合の利益（手数料・送料を差し引き）で採点する。--profile pokeca・onepiece では既定でオン",
    )
    resale.add_argument("--min-profit", type=_arg(_non_negative, "0以上の整数"), metavar="円", help="これ未満の利益は候補にしない（既定 500円、pokeca・onepiece は 1,000円）")
    resale.add_argument("--min-roi", type=_arg(parse_ratio, "0.15・15% のような形"), metavar="割合", help="利益率（利益 ÷ 仕入れ値）の下限（既定 15%%）")
    resale.add_argument("--min-sales", type=_arg(_non_negative, "0以上の整数"), metavar="件", help="直近30日の販売数がこれ未満なら候補にしない（既定 3）")
    resale.add_argument("--fee-rate", type=_arg(parse_ratio, "0.1・10% のような形"), metavar="割合", help="販売手数料（既定 10%%）")
    resale.add_argument("--resale-shipping", type=_arg(_non_negative, "0以上の整数"), metavar="円", help="売るときの送料（既定 750円、pokeca・onepiece は 230円・BOXは520円以上）")
    output.add_argument("--open", action="store_true", help="保存したHTMLレポートをブラウザで開く（監視モードでは最初の1回だけ。--html がなければ report.html に保存）")
    output.add_argument("--no-color", action="store_true", help="色を付けずに表示する")

    watch = parser.add_argument_group("監視")
    watch.add_argument("--watch", type=float, metavar="分", help=f"N分ごとに検索し直し、新しく見つかった候補だけを表示する（最短{MIN_WATCH_MINUTES:g}分）")
    watch.add_argument("--webhook", metavar="URL", help="候補を Discord / Slack の Webhook に送る")

    diagnosis = parser.add_argument_group("診断")
    diagnosis.add_argument(
        "--probe",
        action="store_true",
        help="各サイト（メルカリ・ラクマ・Yahoo!フリマ・スニダン）から取得できるかを調べて表示する"
        "（キーワードを省くと「ポケモンカード SAR」）",
    )

    network = parser.add_argument_group("通信")
    network.add_argument("--interval", type=float, default=1.5, metavar="秒", help=f"リクエストの間隔（既定 1.5秒、最短{MIN_REQUEST_INTERVAL:g}秒）")
    return parser


# ---------------------------------------------------------------- 実行

def _prepare_console(stream: TextIO) -> None:
    # Windows のコンソールなどで表示できない文字があっても止まらないようにする
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


def _use_color(stream: TextIO, disabled: bool) -> bool:
    if disabled or "NO_COLOR" in os.environ or not getattr(stream, "isatty", lambda: False)():
        return False
    if os.name != "nt":
        return True
    try:  # Windows 10 以降のコンソールで ANSI エスケープを有効にする
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


def run_all(
    plans: list[SearchPlan], sources: Mapping[str, Source], progress: Callable[[str], None]
) -> list[SearchResult]:
    return [run_plan(plan, sources, progress=progress) for plan in plans]


def _save_files(
    results: list[SearchResult],
    args: argparse.Namespace,
    progress: Callable[[str], None],
    *,
    open_browser: bool = False,
) -> None:
    if args.html:
        write_html(results, args.html, top=args.top)
        progress(f"HTMLレポートを保存しました: {args.html}")
        if open_browser:
            webbrowser.open(Path(args.html).resolve().as_uri())
    if args.csv:
        write_csv(results, args.csv)
        progress(f"CSVを保存しました: {args.csv}")
    if args.json:
        write_json(results, args.json)
        progress(f"JSONを保存しました: {args.json}")


def _notify(url: str | None, title: str, keyword: str, evaluations: list, progress: Callable[[str], None]) -> None:
    if not url or not evaluations:
        return
    try:
        send_webhook(url, format_message(keyword, evaluations, title=title))
    except requests.RequestException as exc:
        progress(f"Webhook への通知に失敗しました: {exc}")


def _exit_code(results: list[SearchResult]) -> int:
    # すべての検索で何も取得できなかった（通信できていない）ときだけ失敗扱い
    return 1 if all(result.fetch_failed for result in results) else 0


def _watch(
    plans: list[SearchPlan],
    sources: Mapping[str, Source],
    args: argparse.Namespace,
    out: TextIO,
    progress: Callable[[str], None],
    color: bool,
    sleep: Callable[[float], None],
) -> int:
    minutes = max(args.watch, MIN_WATCH_MINUTES)
    if args.watch < MIN_WATCH_MINUTES:
        progress(f"監視の間隔は最短{MIN_WATCH_MINUTES:g}分です（{minutes:g}分にしました）")
    seen: set[tuple[str, int]] = set()
    first = True
    try:
        while True:
            results = run_all(plans, sources, progress)
            if first:
                for result in results:
                    print_result(result, out, top=args.top, color=color)
                    _notify(args.webhook, "【フリマおすすめ】", result.plan.label, result.recommendations[:3], progress)
                progress(f"監視を始めました。{minutes:g}分ごとに検索し直し、新しく見つかった候補だけを表示します（Ctrl+C で終了）")
            else:
                found = 0
                for result in results:
                    for error in result.errors:
                        progress(f"取得エラー（{result.plan.label}）: {error}")
                    fresh = [
                        (rank, evaluation)
                        for rank, evaluation in enumerate(result.recommendations, 1)
                        if (evaluation.listing.key, evaluation.listing.price) not in seen
                    ][: args.top]
                    if not fresh:
                        continue
                    found += len(fresh)
                    out.write(f"■ 新着「{result.plan.label}」 {datetime.now(JST):%H:%M}\n")
                    for rank, evaluation in fresh:
                        for line in item_lines(rank, evaluation):
                            out.write(f"  {line}\n")
                        out.write("\n")
                    out.flush()
                    _notify(args.webhook, "【フリマ新着おすすめ】", result.plan.label, [e for _, e in fresh], progress)
                if not found:
                    progress(f"{datetime.now(JST):%H:%M} 新着の候補はありませんでした")
            for result in results:
                seen.update((e.listing.key, e.listing.price) for e in result.recommendations)
            _save_files(results, args, progress, open_browser=args.open and first)
            first = False
            sleep(minutes * 60)
    except KeyboardInterrupt:
        progress("監視を終了しました")
        return 0


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    sources: Mapping[str, Source] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    _prepare_console(stdout)
    _prepare_console(stderr)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.probe:
        keyword = args.keywords[0] if args.keywords else "ポケモンカード SAR"
        http = HttpClient(min_interval=max(args.interval, MIN_REQUEST_INTERVAL))
        run_probe(keyword, http, stdout, Path("probe"))
        return 0
    if args.open and not args.html:
        args.html = Path("report.html")
    try:
        plans = build_plans(args)
    except ConfigError as exc:
        parser.error(str(exc))

    def progress(message: str) -> None:
        print(message, file=stderr, flush=True)

    if sources is None:
        http = HttpClient(min_interval=max(args.interval, MIN_REQUEST_INTERVAL))
        sources = create_sources([name for plan in plans for name in plan.sources], http)
    color = _use_color(stdout, args.no_color)

    if args.watch is not None:
        return _watch(plans, sources, args, stdout, progress, color, sleep)

    try:
        results = run_all(plans, sources, progress)
    except KeyboardInterrupt:
        progress("中断しました")
        return 130
    for result in results:
        print_result(result, stdout, top=args.top, color=color)
        _notify(args.webhook, "【フリマおすすめ】", result.plan.label, result.recommendations[:5], progress)
    stdout.flush()
    _save_files(results, args, progress, open_browser=args.open)
    if any(result.fetch_failed for result in results):
        progress("取得に失敗した検索があります。インターネット接続や、サイトの仕様変更がないかを確認してください。")
    return _exit_code(results)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

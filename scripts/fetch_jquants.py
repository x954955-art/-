"""J-Quants API（V2）から株価四本値と TOPIX を取得し、手元の SQLite に貯める。

使い方:
    # 動作確認（Free プランでも可。1分5回までなので --rpm 5、TOPIX は Free の対象外）
    python scripts/fetch_jquants.py --dates 2025-06-02 2025-06-03 --rpm 5 --skip-topix

    # 本取得（Light 以上）: ファイルダウンロード機能（月ごとの CSV）で、プランで取れる全期間を取得
    python scripts/fetch_jquants.py --bulk

    # 財務情報（決算短信・業績予想の修正のサマリー）も取る（Phase 2 で使う）
    python scripts/fetch_jquants.py --bulk --datasets fins

- API キーは環境変数 JQUANTS_API_KEY か、リポジトリ直下の .env（JQUANTS_API_KEY=...）から読む。画面には出さない
- 保存先は data/jquants/jquants.sqlite（git 管理外）。株価は daily_quotes、TOPIX は topix、財務情報は fins_summary テーブル
- 株価は調整前の値と調整係数（AdjFactor）で保存する。CSV には調整済み株価がないので、調整は sources.py が読み込み時に行う
- 時価総額は円単位の MarketCapYen 列にも入れる（J-Quants の MktCap は百万円単位）
- 取得済みのファイル・日付は fetch_log に記録して飛ばすので、途中で止めても再実行で続きから
- J-Quants の規約: 私的利用のみ。解約して請求期間が終わったら、取得したデータと複製をすべて消すこと（第7条）
- 表示するのは件数などの集計だけ
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://api.jquants.com/v2"
BARS = "/equities/bars/daily"
TOPIX = "/indices/bars/daily/topix"
FINS = "/fins/summary"
WAIT_429 = 120  # 429 には Retry-After が付かないので2分待つ（公式の推奨）
RETRY_WAITS = (5, 30, 120)  # 5xx・通信エラーの再試行間隔

# 列名の候補（先頭が API V2 の名前。CSV で別名だった場合に備えて V1 の名前も見る）
BAR_COLS = {
    "Date": ["Date"],
    "Code": ["Code", "LocalCode"],
    "O": ["O", "Open"],
    "H": ["H", "High"],
    "L": ["L", "Low"],
    "C": ["C", "Close"],
    "Vo": ["Vo", "Volume"],
    "Va": ["Va", "TurnoverValue"],
    "AdjFactor": ["AdjFactor", "AdjustmentFactor"],
    "MktCap": ["MktCap", "MarketCapitalization"],
}
BAR_REQUIRED = ("Date", "Code", "O", "C", "AdjFactor")
TOPIX_COLS = {"Date": ["Date"], "O": ["O", "Open"], "H": ["H", "High"], "L": ["L", "Low"], "C": ["C", "Close"]}
# 財務情報は使う列だけ残す（金額は円。NC 付きの個別の値は使わない: 非連結の会社は NC なしの列に個別の値が入る）
FINS_TEXT = ["DiscNo", "Code", "DiscDate", "DiscTime", "DocType", "CurPerType", "CurPerSt", "CurPerEn",
             "CurFYSt", "CurFYEn", "NxtFYSt", "NxtFYEn"]
FINS_NUM = ["Sales", "OP", "OdP", "NP", "FSales", "FOP", "FOdP", "FNP", "NxFSales", "NxFOP", "NxFOdP", "NxFNp", "ShOutFY", "TrShFY"]
FINS_REQUIRED = ("DiscNo", "Code", "DiscDate", "DiscTime", "DocType", "OP", "FOP", "NxFOP")

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_quotes (
    Date TEXT NOT NULL,
    Code TEXT NOT NULL,
    O REAL, H REAL, L REAL, C REAL, Vo REAL, Va REAL,
    AdjFactor REAL,
    MktCap REAL,        -- 時価総額（百万円。J-Quants の値そのまま）
    MarketCapYen REAL,  -- 時価総額（円）。設定の [prices] mcap に指定する
    PRIMARY KEY (Code, Date)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS daily_quotes_date ON daily_quotes(Date);
CREATE TABLE IF NOT EXISTS topix (Date TEXT PRIMARY KEY, O REAL, H REAL, L REAL, C REAL);
CREATE TABLE IF NOT EXISTS fins_summary (
    DiscNo TEXT NOT NULL, Code TEXT NOT NULL, DiscDate TEXT, DiscTime TEXT, DocType TEXT,
    CurPerType TEXT, CurPerSt TEXT, CurPerEn TEXT, CurFYSt TEXT, CurFYEn TEXT, NxtFYSt TEXT, NxtFYEn TEXT,
    Sales REAL, OP REAL, OdP REAL, NP REAL, FSales REAL, FOP REAL, FOdP REAL, FNP REAL,
    NxFSales REAL, NxFOP REAL, NxFOdP REAL, NxFNp REAL, ShOutFY REAL, TrShFY REAL,
    PRIMARY KEY (DiscNo, Code)
);
CREATE TABLE IF NOT EXISTS fetch_log (item TEXT PRIMARY KEY, rows INTEGER, source_modified TEXT, fetched_at TEXT);
"""


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


def http_get(url: str, headers: dict | None = None, timeout: int = 120) -> bytes:
    """GET して本文を返す。HTTP エラーは本文の message を添えて ApiError にする。"""
    req = urllib.request.Request(url, headers={"User-Agent": "tdnet-edge/0.1 (personal research)", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body).get("message", body)
        except (json.JSONDecodeError, AttributeError):
            msg = body
        raise ApiError(e.code, str(msg)[:300]) from None


def load_api_key(env_path: Path = ROOT / ".env") -> str:
    key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not key and env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():  # PowerShell で作ると BOM が付くことがある
            name, sep, value = line.partition("=")
            if sep and name.strip() == "JQUANTS_API_KEY":
                key = value.strip().strip('"').strip("'")
    if not key:
        sys.exit(f"API キーが見つかりません。{env_path} に JQUANTS_API_KEY=... を1行で書くか、環境変数で渡してください。")
    return key


class Client:
    """1分あたりの回数上限を守って J-Quants API を呼ぶ。"""

    def __init__(self, api_key: str, rpm: float, sleep=time.sleep, clock=time.monotonic):
        self.headers = {"x-api-key": api_key}
        self.interval = 60.0 / rpm * 1.05 if rpm > 0 else 0.0  # 上限ちょうどにならないよう少し余裕を持たせる
        self.sleep, self.clock = sleep, clock
        self.last: float | None = None
        self.calls = 0

    def _pace(self):
        if self.last is not None:
            wait = self.interval - (self.clock() - self.last)
            if wait > 0:
                self.sleep(wait)
        self.last = self.clock()

    def get_json(self, path: str, params: dict) -> dict:
        url = BASE + path + "?" + urllib.parse.urlencode(params)
        for k, wait in enumerate((0, *RETRY_WAITS)):
            if wait:
                self.sleep(wait)
            self._pace()
            self.calls += 1
            try:
                return json.loads(http_get(url, self.headers).decode("utf-8"))
            except ApiError as e:
                if e.status == 429:  # 429 も回数に数えられるので、すぐには再試行しない
                    self.sleep(WAIT_429)
                    continue
                if e.status < 500 or k == len(RETRY_WAITS):
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if k == len(RETRY_WAITS):
                    raise
        raise ApiError(429, "レートリミットの超過が続きました。時間を置いて再実行してください")

    def get_all(self, path: str, params: dict) -> list[dict]:
        """pagination_key が返らなくなるまで取得して data をつなげる。"""
        out, p = [], dict(params)
        while True:
            js = self.get_json(path, p)
            out += js.get("data", [])
            pk = js.get("pagination_key")
            if not pk:
                return out
            p = {**params, "pagination_key": pk}


# ---------------- 整形・保存 ----------------

def standardize(df: pd.DataFrame, spec: dict, required, what: str) -> pd.DataFrame:
    """API/CSV の列名を揃えて数値に変換する。必須の列がなければ、見つかった列名を添えてエラーにする。"""
    low = {str(c).lower(): c for c in df.columns}
    src = {name: next((low[c.lower()] for c in cands if c.lower() in low), None) for name, cands in spec.items()}
    missing = [c for c in required if src[c] is None]
    if missing:
        raise ValueError(f"{what}: 列 {missing} が見つかりません（見つかった列: {list(df.columns)}）")
    out = pd.DataFrame({name: (df[s] if s is not None else None) for name, s in src.items()}, index=df.index)
    out["Date"] = pd.to_datetime(out["Date"].astype(str).str.slice(0, 10), errors="coerce").dt.strftime("%Y-%m-%d")
    for c in out.columns:
        if c not in ("Date", "Code"):
            out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64")  # sqlite3 は numpy の整数型を受け付けない
    return out.dropna(subset=["Date"]).reset_index(drop=True)


def bars_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = standardize(df, BAR_COLS, BAR_REQUIRED, "株価四本値")
    out["Code"] = out["Code"].astype(str).str.strip()
    out["MarketCapYen"] = out["MktCap"] * 1e6
    return out


def topix_frame(df: pd.DataFrame) -> pd.DataFrame:
    return standardize(df, TOPIX_COLS, ("Date", "C"), "TOPIX")


def fins_frame(df: pd.DataFrame) -> pd.DataFrame:
    """財務情報: 日付・種別などは文字列のまま、金額は数値に（空文字列は欠損）。"""
    missing = [c for c in FINS_REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"財務情報: 列 {missing} が見つかりません（見つかった列: {list(df.columns)}）")
    out = pd.DataFrame(index=df.index)
    for c in FINS_TEXT:
        out[c] = df[c].fillna("").astype(str).str.strip() if c in df else ""
    for c in FINS_NUM:
        out[c] = pd.to_numeric(df[c], errors="coerce").astype("float64") if c in df else float("nan")
    return out[(out["DiscNo"] != "") & (out["Code"] != "")].reset_index(drop=True)


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def upsert(con: sqlite3.Connection, table: str, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    cols = list(df.columns)
    rows = df.astype(object).where(df.notna(), None).itertuples(index=False, name=None)  # NaN は NULL で保存
    con.executemany(f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", rows)
    return len(df)


def logged(con: sqlite3.Connection, item: str) -> tuple | None:
    return con.execute("SELECT rows, source_modified FROM fetch_log WHERE item = ?", (item,)).fetchone()


def log(con: sqlite3.Connection, item: str, rows: int, source_modified: str | None = None) -> None:
    con.execute(
        "INSERT OR REPLACE INTO fetch_log VALUES (?, ?, ?, ?)",
        (item, rows, source_modified, datetime.now().isoformat(timespec="seconds")),
    )
    con.commit()


def read_csv_bytes(raw: bytes) -> pd.DataFrame:
    comp = "gzip" if raw[:2] == b"\x1f\x8b" else None
    return pd.read_csv(io.BytesIO(raw), compression=comp, dtype=str)


# ---------------- 取得 ----------------

def fetch_bulk(client: Client, con, endpoint: str, table: str, frame, start: str | None, end: str | None, refresh: bool) -> list:
    """ファイルダウンロード機能で取得。一覧の LastModified が変わっていないファイルは取り直さない。"""
    params = {"endpoint": endpoint, **({"from": start} if start else {}), **({"to": end} if end else {})}
    files = sorted(client.get_all("/bulk/list", params), key=lambda f: f["Key"])
    print(f"{endpoint}: ファイル {len(files):,} 個", file=sys.stderr)
    failed = []
    for n, f in enumerate(files, 1):
        item = f"bulk:{f['Key']}"
        prev = logged(con, item)
        if prev and prev[1] == f.get("LastModified") and not refresh:
            continue
        try:
            url = client.get_json("/bulk/get", {"key": f["Key"]})["url"]
            rows = upsert(con, table, frame(read_csv_bytes(http_get(url))))  # 署名付き URL（5分有効）。API キーは付けない
        except (ApiError, urllib.error.URLError, TimeoutError, ConnectionError) as e:
            print(f"  {f['Key']}: 取得失敗（{e}）。再実行すると続きから再試行します", file=sys.stderr)
            failed.append(f["Key"])
            continue
        log(con, item, rows, f.get("LastModified"))
        if n % 12 == 0:
            print(f"  {n:,}/{len(files):,} ファイル", file=sys.stderr)
    return failed


def fetch_dates(client: Client, con, days: list[str], refresh: bool) -> list:
    """日付ごとに全銘柄を API で取得（Free プランの動作確認・少ない日数の追加用）。休日は0件で記録する。"""
    failed = []
    for d in days:
        item = f"api:{BARS}:{d}"
        if logged(con, item) and not refresh:
            continue
        try:
            recs = client.get_all(BARS, {"date": d})
        except ApiError as e:
            print(f"  {d}: 取得失敗（{e}）", file=sys.stderr)
            failed.append(d)
            continue
        rows = upsert(con, "daily_quotes", bars_frame(pd.DataFrame(recs))) if recs else 0
        log(con, item, rows)
    return failed


def fetch_topix_api(client: Client, con, start: str, end: str) -> list:
    try:
        recs = client.get_all(TOPIX, {"from": start, "to": end})
    except ApiError as e:
        print(f"  TOPIX: 取得失敗（{e}）。Free プランは TOPIX の対象外です", file=sys.stderr)
        return ["topix"]
    log(con, f"api:{TOPIX}:{start}:{end}", upsert(con, "topix", topix_frame(pd.DataFrame(recs))) if recs else 0)
    return []


def weekdays(start: date, end: date) -> list[str]:
    return [f"{start + timedelta(i):%Y-%m-%d}" for i in range((end - start).days + 1) if (start + timedelta(i)).weekday() < 5]


def report(con: sqlite3.Connection) -> str:
    rows = con.execute(
        "SELECT substr(Date, 1, 4), COUNT(*), COUNT(DISTINCT Code), COUNT(DISTINCT Date), SUM(C IS NULL), SUM(MktCap IS NOT NULL) "
        "FROM daily_quotes GROUP BY 1 ORDER BY 1"
    ).fetchall()
    lo, hi = con.execute("SELECT MIN(Date), MAX(Date) FROM daily_quotes").fetchone()
    t_lo, t_hi, t_n = con.execute("SELECT MIN(Date), MAX(Date), COUNT(*) FROM topix").fetchone()
    no_topix = con.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT Date FROM daily_quotes) d WHERE d.Date NOT IN (SELECT Date FROM topix)"
    ).fetchone()[0]
    f_lo, f_hi, f_n, f_codes = con.execute("SELECT MIN(DiscDate), MAX(DiscDate), COUNT(*), COUNT(DISTINCT Code) FROM fins_summary").fetchone()
    lines = [
        f"- 株価: {lo or 'なし'} 〜 {hi or 'なし'}",
        f"- TOPIX: {t_lo or 'なし'} 〜 {t_hi or 'なし'}（{t_n:,} 日）。株価はあるが TOPIX がない日: {no_topix:,}",
        f"- 財務情報: {f_lo or 'なし'} 〜 {f_hi or 'なし'}（{f_n:,} 件、銘柄 {f_codes:,}）",
        "",
        "| 年 | 行数 | 銘柄数 | 営業日数 | 約定なしの行 | 時価総額あり |",
        "|---|---:|---:|---:|---:|---:|",
        *[f"| {y} | {n:,} | {nc:,} | {nd:,} | {nn:,} | {nm / n:.0%} |" for y, n, nc, nd, nn, nm in rows],
    ]
    return "\n".join(lines)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows で出力をパイプしても日本語が化けないように
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--bulk", action="store_true", help="ファイルダウンロード機能（月ごとの CSV）で取得する（Light 以上）")
    ap.add_argument("--dates", nargs="+", help="API で取得する日付 YYYY-MM-DD（動作確認用）")
    ap.add_argument("--start", help="開始日 YYYY-MM-DD（--bulk ではファイルの絞り込み、それ以外は平日を1日ずつ API で取得）")
    ap.add_argument("--end", help="終了日 YYYY-MM-DD（既定: 昨日）")
    ap.add_argument("--datasets", nargs="+", choices=["bars", "topix", "fins"], default=["bars", "topix"],
                    help="--bulk で取るデータ（bars: 株価 / topix: TOPIX / fins: 財務情報）")
    ap.add_argument("--skip-topix", action="store_true", help="TOPIX を取らない（Free プランは TOPIX の対象外）")
    ap.add_argument("--rpm", type=float, default=60, help="1分あたりのリクエスト上限（Free 5 / Light 60 / Standard 120）")
    ap.add_argument("--refresh", action="store_true", help="取得済みでも取り直す")
    ap.add_argument("--db", default=str(ROOT / "data" / "jquants" / "jquants.sqlite"))
    args = ap.parse_args()
    if not (args.bulk or args.dates or args.start):
        ap.error("--bulk / --dates / --start のどれかを指定してください")

    client = Client(load_api_key(), args.rpm)
    con = open_db(Path(args.db))
    failed = []
    if args.bulk:
        jobs = {"bars": (BARS, "daily_quotes", bars_frame), "topix": (TOPIX, "topix", topix_frame), "fins": (FINS, "fins_summary", fins_frame)}
        for name in args.datasets:
            if name == "topix" and args.skip_topix:
                continue
            endpoint, table, frame = jobs[name]
            failed += fetch_bulk(client, con, endpoint, table, frame, args.start, args.end, args.refresh)
    else:
        end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
        days = sorted(args.dates) if args.dates else weekdays(date.fromisoformat(args.start), end)
        print(f"API で取得: {days[0]} 〜 {days[-1]}（{len(days):,} 日）", file=sys.stderr)
        failed += fetch_dates(client, con, days, args.refresh)
        if not args.skip_topix:
            failed += fetch_topix_api(client, con, days[0], days[-1])

    print(f"# J-Quants 取得結果（API 呼び出し {client.calls:,} 回、失敗 {len(failed):,} 件）")
    print(report(con))
    con.close()
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

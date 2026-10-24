import io
import json
from pathlib import Path

from furima_finder.http import HttpClient
from furima_finder.probe import run_probe

FIXTURES = Path(__file__).parent / "fixtures"


class Response:
    def __init__(self, body, status_code=200, url="https://example/"):
        self.text = body
        self.status_code = status_code
        self.headers = {"Content-Type": "text/html"}
        self.url = url

    def json(self):
        return json.loads(self.text)


class Session:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append(url)
        for prefix, response in self.responses:
            if url.startswith(prefix):
                return response
        raise AssertionError(url)


def test_probe_reports_each_site(tmp_path):
    mercari = (FIXTURES / "mercari_search_page2.json").read_text(encoding="utf-8")
    session = Session([
        ("https://api.mercari.jp", Response(mercari)),
        ("https://fril.jp", Response((FIXTURES / "rakuma_search.html").read_text(encoding="utf-8"))),
        ("https://paypayfleamarket.yahoo.co.jp", Response((FIXTURES / "yahoo_search.html").read_text(encoding="utf-8"))),
        ("https://snkrdunk.com", Response("<html><head><title>アクセスが集中しています</title></head><body>しばらくお待ちください</body></html>", 503)),
    ])
    out = io.StringIO()
    run_probe("ポケモンカード SAR", HttpClient(session=session, sleep=lambda s: None, min_interval=0), out, tmp_path)
    report = out.getvalue()
    assert "■ メルカリ\n  OK: 2件取得" in report
    assert "■ ラクマ" in report and "商品として読めた件数: 2件" in report
    assert "配列 #0.props.pageProps.initialState.search.items（3件）" in report
    assert "  id = \"z100000001\"" in report
    assert "HTTP 503" in report and "ページのタイトル: アクセスが集中しています" in report
    assert (tmp_path / "yahoo_1.html").exists() and (tmp_path / "snkrdunk_2.html").exists()


def test_probe_option_runs_without_keywords(monkeypatch, tmp_path):
    from furima_finder import cli

    monkeypatch.chdir(tmp_path)
    called = {}
    monkeypatch.setattr(cli, "run_probe", lambda keyword, http, out, save_dir: called.update(keyword=keyword, save_dir=save_dir))
    assert cli.main(["--probe"], stdout=io.StringIO(), stderr=io.StringIO()) == 0
    assert called == {"keyword": "ポケモンカード SAR", "save_dir": Path("probe")}
    cli.main(["--probe", "リザードンex SAR"], stdout=io.StringIO(), stderr=io.StringIO())
    assert called["keyword"] == "リザードンex SAR"

from pathlib import Path

from furima_finder.http import HttpClient
from furima_finder.models import SearchQuery
from furima_finder.sources.rakuma import SEARCH_URL, RakumaSource, parse_search_html

HTML = (Path(__file__).parent / "fixtures" / "rakuma_search.html").read_text(encoding="utf-8")


def test_parse_search_html():
    first, second = parse_search_html(HTML, sold=False)
    assert first.source == "rakuma"
    assert first.id == "abc123def456"
    assert first.url == "https://item.fril.jp/abc123def456"
    assert first.title == "Nintendo Switch 有機EL ホワイト"
    assert first.price == 27500
    assert first.thumbnail == "https://img.fril.jp/img/111/m/abc.jpg"
    assert first.condition is None and first.shipping_included is None and first.created_at is None

    # title 属性が空なら商品名の要素から、相対URLは item.fril.jp に寄せる
    assert second.title == "Switch 有機EL ジャンク"
    assert second.url == "https://item.fril.jp/0f0f0f0f0f"
    assert second.price == 8000


def test_parse_marks_sold_pages():
    assert all(listing.sold for listing in parse_search_html(HTML, sold=True))


class Response:
    def __init__(self, body):
        self.text = body
        self.status_code = 200
        self.headers = {}


class Session:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return Response(self.pages.pop(0) if self.pages else "<html></html>")


def test_search_filters_on_client_side_and_stops_on_repeated_page():
    session = Session([HTML, HTML])
    source = RakumaSource(HttpClient(session=session, sleep=lambda s: None, min_interval=0))
    listings = source.search(
        SearchQuery(keyword="Switch 有機EL", exclude=["ジャンク"], price_min=10000, pages=3, sold=True)
    )
    assert [listing.id for listing in listings] == ["abc123def456"]
    assert len(session.calls) == 2  # 2ページ目は1ページ目と同じ内容なので打ち切る
    first, second = session.calls
    assert first["url"] == SEARCH_URL
    assert first["params"] == {"query": "Switch 有機EL", "order": "desc", "sort": "created_at", "transaction": "soldout"}
    assert second["params"]["page"] == "2"

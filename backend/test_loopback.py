"""`localhost` is pinned to 127.0.0.1 for services compose binds to IPv4 only.

On the Windows desk this was ~2s per Qdrant call and per web search (see loopback.py).
"""
import inspect

from backend import loopback


def test_pin_rewrites_localhost_only():
    assert loopback.pin("localhost") == "127.0.0.1"
    assert loopback.pin(" LocalHost ") == "127.0.0.1"
    assert loopback.pin("10.1.2.3") == "10.1.2.3"
    assert loopback.pin("qdrant.internal") == "qdrant.internal"


def test_pin_url_preserves_everything_but_the_host():
    assert loopback.pin_url("http://localhost:8088") == "http://127.0.0.1:8088"
    assert (loopback.pin_url("http://localhost:8088/search?q=a b")
            == "http://127.0.0.1:8088/search?q=a b")
    assert loopback.pin_url("https://u:p@localhost/x") == "https://u:p@127.0.0.1/x"


def test_pin_url_leaves_other_hosts_alone():
    for url in ("http://example.com", "http://localhost.example.com:80/", "http://127.0.0.1:8088"):
        assert loopback.pin_url(url) == url


def test_searxng_and_qdrant_both_go_through_the_pin():
    from backend.storage import qdrant
    from backend.tools import browser
    assert "loopback" in inspect.getsource(qdrant.host)
    assert "_pin_url(" in inspect.getsource(browser)

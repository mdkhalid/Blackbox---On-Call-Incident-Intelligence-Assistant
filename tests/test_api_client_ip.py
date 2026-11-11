from starlette.requests import Request

from blackbox.api.client_ip import resolve_client_ip
from blackbox.iputils import parse_trusted_networks


def _request(peer: str | None, headers: dict[str, str] | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/chat",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": (peer, 1234) if peer else None,
        "query_string": b"",
    }
    return Request(scope)


TRUSTED = parse_trusted_networks(["10.0.0.0/8", "192.168.0.0/16"])


def test_without_trusted_proxies_the_peer_address_is_used():
    request = _request("203.0.113.7", {"X-Forwarded-For": "198.51.100.9"})
    assert resolve_client_ip(request, []) == "203.0.113.7"


def test_a_trusted_proxy_reports_the_forwarded_client():
    request = _request("10.0.0.5", {"X-Forwarded-For": "198.51.100.9"})
    assert resolve_client_ip(request, TRUSTED) == "198.51.100.9"


def test_an_untrusted_peer_cannot_spoof_the_forwarded_header():
    """A client-supplied header must not be able to choose its own rate-limit bucket."""
    request = _request("203.0.113.7", {"X-Forwarded-For": "198.51.100.9"})
    assert resolve_client_ip(request, TRUSTED) == "203.0.113.7"


def test_a_chain_of_proxies_yields_the_client_behind_them():
    request = _request(
        "10.0.0.5",
        {"X-Forwarded-For": "198.51.100.9, 10.1.2.3"},
    )
    assert resolve_client_ip(request, TRUSTED) == "198.51.100.9"


def test_the_peer_is_used_when_the_forwarded_header_is_absent():
    request = _request("10.0.0.5")
    assert resolve_client_ip(request, TRUSTED) == "10.0.0.5"


def test_a_forwarded_address_with_a_port_is_normalised():
    request = _request("10.0.0.5", {"X-Forwarded-For": "198.51.100.9:44321"})
    assert resolve_client_ip(request, TRUSTED) == "198.51.100.9"


def test_a_forwarded_address_that_is_itself_a_proxy_is_skipped():
    request = _request("10.0.0.5", {"X-Forwarded-For": "10.1.2.3, 198.51.100.9"})
    assert resolve_client_ip(request, TRUSTED) == "198.51.100.9"


def test_the_peer_is_used_when_every_forwarded_address_is_a_proxy():
    request = _request("10.0.0.5", {"X-Forwarded-For": "10.1.2.3, 192.168.5.5"})
    assert resolve_client_ip(request, TRUSTED) == "10.0.0.5"


def test_a_missing_peer_falls_back_to_unknown():
    request = _request(None)
    assert resolve_client_ip(request, TRUSTED) == "unknown"


def test_invalid_trusted_networks_are_ignored():
    assert parse_trusted_networks(["not-an-ip", "", "10.0.0.0/8"]) != []
    assert len(parse_trusted_networks(["not-an-ip", "", "10.0.0.0/8"])) == 1

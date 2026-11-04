"""The gateway's redirect handling, which is where the URL policy is enforced.

`application.invoke` checks the URL it is about to ask for. That check cannot
see where a redirect points, and until `redirect_guard` existed nothing did: a
host could answer `302 Location: http://169.254.169.254/...` and the body of
the cloud metadata service came back as if it were the API's.

These tests assert on **what was and was not sent**, not just on the exception.
A refusal that still made the request would pass a `pytest.raises` and leak
anyway, so every refusal case pins the recorded request count.
"""

from contextlib import asynccontextmanager

import httpx
import pytest

from stargate.adapters.httpx_gateway import HttpxApiGateway
from stargate.adapters.redirect_guard import MAX_REDIRECTS

METADATA = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"


@asynccontextmanager
async def gateway_for(**kwargs):
    """A gateway closed however the test ends.

    Closing on a test's last line only closes it when the test passes — and a
    failing assertion in this file is the interesting case, so the leaked
    client and its unclosed-transport warnings would land on top of the
    regression they are reporting.
    """
    gateway = HttpxApiGateway(**kwargs)
    try:
        yield gateway
    finally:
        await gateway.aclose()


def recording(handler):
    """A MockTransport that keeps every request it was asked to send."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(record), seen


def redirecting(target: str, status: int = 302):
    """Answers the first request with a redirect and the second with a body."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) != target:
            return httpx.Response(status, headers={"Location": target})
        return httpx.Response(200, json={"arrived": True})

    return handler


async def test_a_plain_response_is_unaffected():
    """The happy path, so the hand-written loop cannot quietly break it."""
    transport, seen = recording(lambda _: httpx.Response(200, json={"ok": 1}))

    async with gateway_for(transport=transport) as gateway:
        response = await gateway.request("GET", "https://api.example.com/pets")

    assert len(seen) == 1
    assert response.status_code == 200
    assert response.json() == {"ok": 1}
    assert response.final_url == "https://api.example.com/pets"


async def test_an_allowed_redirect_is_followed():
    target = "https://api.example.com/pets/2"
    transport, seen = recording(redirecting(target))

    async with gateway_for(transport=transport, allowed_domains={"api.example.com"}) as gateway:
        response = await gateway.request("GET", "https://api.example.com/pets")

    assert [str(r.url) for r in seen] == ["https://api.example.com/pets", target]
    assert response.json() == {"arrived": True}
    # The body did not come from the URL that was asked for.
    assert response.final_url == target


async def test_a_relative_location_is_resolved_before_it_is_checked():
    """The guard must check the resolved URL, never the raw header."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/pets":
            return httpx.Response(302, headers={"Location": "/pets/final"})
        return httpx.Response(200, json={"arrived": True})

    transport, seen = recording(handler)

    async with gateway_for(transport=transport, allowed_domains={"api.example.com"}) as gateway:
        response = await gateway.request("GET", "https://api.example.com/pets")

    assert str(seen[1].url) == "https://api.example.com/pets/final"
    assert response.json() == {"arrived": True}


async def test_a_redirect_to_a_private_address_is_refused():
    """The reported defect. No allowlist configured — the blocklist alone must hold."""
    transport, seen = recording(redirecting(METADATA))

    async with gateway_for(transport=transport) as gateway:
        with pytest.raises(ValueError, match="Unsafe redirect"):
            await gateway.request("GET", "https://api.example.com/pets")

    # The one thing that matters: the metadata service was never contacted.
    assert len(seen) == 1
    assert str(seen[0].url) == "https://api.example.com/pets"


async def test_a_redirect_off_the_allowlist_is_refused():
    transport, seen = recording(redirecting("https://evil.example.com/collect"))

    async with gateway_for(transport=transport, allowed_domains={"api.example.com"}) as gateway:
        with pytest.raises(ValueError, match="Unsafe redirect"):
            await gateway.request("GET", "https://api.example.com/pets")

    assert len(seen) == 1


async def test_a_custom_auth_header_does_not_cross_an_origin():
    """httpx strips `Authorization` by name, and only that name.

    A gateway configured with a different header would hand the credential to
    whatever the first host redirected to — even a redirect the allowlist
    permits is a different origin than the one the key was issued for.
    """
    target = "https://cdn.example.com/pets/2"
    transport, seen = recording(redirecting(target))

    async with gateway_for(
        transport=transport,
        auth_token="s3cret",
        auth_header="X-API-Key",
        allowed_domains={"api.example.com", "cdn.example.com"},
    ) as gateway:
        await gateway.request("GET", "https://api.example.com/pets")

    # The "Bearer " prefix is the gateway's existing normalisation, applied
    # whatever the header is named; what matters here is that it goes once.
    assert seen[0].headers["x-api-key"] == "Bearer s3cret"
    assert "x-api-key" not in seen[1].headers


async def test_a_custom_auth_header_survives_a_same_origin_redirect():
    """Only the origin change strips it; an in-host redirect keeps working."""
    target = "https://api.example.com/pets/2"
    transport, seen = recording(redirecting(target))

    async with gateway_for(
        transport=transport,
        auth_token="s3cret",
        auth_header="X-API-Key",
        allowed_domains={"api.example.com"},
    ) as gateway:
        await gateway.request("GET", "https://api.example.com/pets")

    assert seen[1].headers["x-api-key"] == "Bearer s3cret"


async def test_a_redirect_chain_is_capped():
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(302, headers={"Location": f"/hop/{counter['n']}"})

    transport, seen = recording(handler)

    async with gateway_for(transport=transport, allowed_domains={"api.example.com"}) as gateway:
        with pytest.raises(ValueError, match="Too many redirects"):
            await gateway.request("GET", "https://api.example.com/pets")

    assert len(seen) == MAX_REDIRECTS + 1


async def test_the_first_url_is_checked_too():
    """The gateway is self-contained: a direct library user gets the check."""
    transport, seen = recording(lambda _: httpx.Response(200))

    async with gateway_for(transport=transport) as gateway:
        with pytest.raises(ValueError, match="Unsafe URL"):
            await gateway.request("GET", "http://127.0.0.1/admin")

    assert seen == []


async def test_a_303_redirect_to_a_private_address_is_refused():
    """POST -> 303 -> GET is the shape that turns a write into a read elsewhere."""
    transport, seen = recording(redirecting(METADATA, status=303))

    async with gateway_for(transport=transport) as gateway:
        with pytest.raises(ValueError, match="Unsafe redirect"):
            await gateway.request("POST", "https://api.example.com/pets", json={"name": "x"})

    assert len(seen) == 1


class TestServerHosts:
    """Where a served definition says it will call — the default allowlist.

    Serving used to default to "any public host", so the domain restriction
    only existed for an operator who knew to ask for it. A definition already
    declares its hosts; that is a better default and needs no flag.
    """

    @staticmethod
    def _server(base_url="", tool_url="", resource_url=""):
        from stargate.domain.mcp import MCPResource, MCPServer, MCPTool

        return MCPServer(
            name="s",
            base_url=base_url,
            tools=[MCPTool(name="t", description="d", base_url=tool_url)],
            resources=[MCPResource(uri="api://x", name="r", base_url=resource_url)],
        )

    def test_collects_every_declared_host(self):
        server = self._server(
            base_url="https://api.example.com/v1",
            tool_url="https://files.example.com/download",
            resource_url="https://api.example.com/schema",
        )
        assert server.hosts() == {"api.example.com", "files.example.com"}
        assert server.unreadable_urls() == []

    def test_a_per_tool_override_is_included(self):
        """A tool may point somewhere the server's base URL does not."""
        server = self._server(base_url="https://api.example.com", tool_url="https://other.example")
        assert "other.example" in server.hosts()

    def test_ports_and_case_do_not_split_a_host(self):
        server = self._server(base_url="https://API.Example.com:8443/v1")
        assert server.hosts() == {"api.example.com"}

    def test_a_definition_with_no_urls_declares_nothing(self):
        """The CLI turns this back into None: a call would fail on the empty
        URL long before the policy, so there is nothing to restrict."""
        assert self._server().hosts() == set()
        assert self._server().unreadable_urls() == []

    def test_a_url_with_no_scheme_is_reported_not_ignored(self):
        """It contributes no host, so the operation it belongs to is refused at
        call time with a message about the policy — and without this nothing
        would point at the definition that caused it."""
        server = self._server(base_url="https://api.example.com", tool_url="files.example.com/x")

        assert server.hosts() == {"api.example.com"}
        assert server.unreadable_urls() == ["files.example.com/x"]

    def test_a_definition_whose_urls_all_fail_to_parse(self):
        """The case the CLI refuses rather than serving unrestricted."""
        server = self._server(base_url="api.example.com/v1")

        assert server.hosts() == set()
        assert server.unreadable_urls() == ["api.example.com/v1"]

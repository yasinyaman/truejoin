"""Following redirects without losing the URL policy on the way.

`validate_url` used to run once, on the URL the caller asked for, and httpx then
followed however many redirects the server chose. That made the check advisory:
a spec's host could answer `302 Location: http://169.254.169.254/...` and the
body of the cloud metadata service came back as if it were the API's. The
allowlist behind `--allowed-domain` and the private-IP blocklist were both
defeated by one hop, and the response was handed to whatever was driving the
tools.

So redirects are followed here instead, one at a time, with the policy applied
to every hop. What this module deliberately does *not* do is work out where a
redirect points: httpx has already done that. With ``follow_redirects=False``
its response carries a fully built ``next_request`` — the ``Location`` header
resolved against the current URL, the ``301/302/303 -> GET`` rewrite applied,
``Host`` updated and cookies dropped. Re-deriving any of that by hand is how a
guard ends up disagreeing with the client it is guarding (``Location: //evil``
being the classic). All that is left is deciding whether to send it.

This lives in the adapter layer because it must import httpx, and `application`
may not (see tests/test_architecture.py). It is the same reason the check
cannot simply be moved out of `application.invoke` — that one stays, so no
request is attempted at all for a URL that fails, which a gateway-side check
cannot promise.
"""

import httpx

from trio.domain.mcp.url_policy import validate_url

#: How many hops to follow before giving up. httpx's own default is 20; the
#: lower cap here is not about load, it is that a chain this long is not a real
#: API and every extra hop is another chance to land somewhere unintended.
MAX_REDIRECTS = 5


def ensure_safe_url(url: str, allowed_domains: set[str] | None, *, hop: int = 0) -> None:
    """Raise unless this URL passes the policy.

    Args:
        url: The absolute URL about to be requested.
        allowed_domains: Hosts the caller permits, or None for "any public host".
        hop: 0 for the URL the caller asked for, 1+ for a redirect target. Only
            the message differs, so a refusal reads as what it was.

    Raises:
        ValueError: Naming the host that failed.
    """
    is_valid, error = validate_url(url, allowed_domains)
    if is_valid:
        return
    if hop == 0:
        raise ValueError(f"Unsafe URL: {error}")
    raise ValueError(f"Unsafe redirect to {url}: {error}")


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return url.scheme, url.host, url.port


def _strip_auth_across_origin(
    original: httpx.URL, request: httpx.Request, auth_header: str | None
) -> None:
    """Drop a non-standard auth header once the request leaves its origin.

    httpx does this for ``Authorization`` by name, and only for that name. A
    gateway configured with ``auth_header="X-API-Key"`` would otherwise hand the
    key to whatever the first host redirected to — the allowlist stops the
    *request*, but a redirect inside the allowlist is still a different origin
    than the one the operator issued the credential for.

    Compared against the original origin rather than the previous hop, so a
    chain that returns to the starting host does not re-attach the header.
    """
    if not auth_header or auth_header.lower() == "authorization":
        return
    if _origin(request.url) != _origin(original):
        request.headers.pop(auth_header, None)


def _too_many() -> ValueError:
    return ValueError(f"Too many redirects (limit {MAX_REDIRECTS})")


def _next_hop(
    response: httpx.Response,
    original: httpx.URL,
    allowed_domains: set[str] | None,
    auth_header: str | None,
    hop: int,
) -> httpx.Request | None:
    """The request to send next, or None when the chain has ended.

    Every policy decision about a redirect is made here, once, so the sync and
    async loops below are plumbing that cannot drift apart. A fix that landed
    in only one of two copies of a security check is the failure this shape
    exists to prevent.
    """
    following = response.next_request
    if following is None:
        return None
    ensure_safe_url(str(following.url), allowed_domains, hop=hop)
    _strip_auth_across_origin(original, following, auth_header)
    return following


async def send_following_redirects(
    client: httpx.AsyncClient,
    request: httpx.Request,
    *,
    allowed_domains: set[str] | None = None,
    auth_header: str | None = None,
) -> httpx.Response:
    """Send a request, checking the policy before every hop including the first.

    The client must have been built with ``follow_redirects=False``, or this
    returns the end of a chain nobody checked.

    Raises:
        ValueError: On a URL the policy refuses, or a chain longer than
            ``MAX_REDIRECTS``. The refused request is never sent.
    """
    original = request.url
    ensure_safe_url(str(request.url), allowed_domains, hop=0)
    for hop in range(MAX_REDIRECTS + 1):
        response = await client.send(request)
        following = _next_hop(response, original, allowed_domains, auth_header, hop + 1)
        if following is None:
            return response
        request = following
    raise _too_many()


def send_following_redirects_sync(
    client: httpx.Client,
    request: httpx.Request,
    *,
    allowed_domains: set[str] | None = None,
    auth_header: str | None = None,
) -> httpx.Response:
    """The synchronous twin, for the spec loader."""
    original = request.url
    ensure_safe_url(str(request.url), allowed_domains, hop=0)
    for hop in range(MAX_REDIRECTS + 1):
        response = client.send(request)
        following = _next_hop(response, original, allowed_domains, auth_header, hop + 1)
        if following is None:
            return response
        request = following
    raise _too_many()

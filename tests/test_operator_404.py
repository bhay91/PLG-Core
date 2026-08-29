from __future__ import annotations

from pathlib import Path

import anyio
import httpx2

from plg_core.application import app


async def _get_all(
    requests: tuple[tuple[str, str], ...],
) -> list[httpx2.Response]:
    transport = httpx2.ASGITransport(app=app, client=("127.0.0.1", 48211))
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8000",
    ) as client:
        return [
            await client.get(path, headers={"Accept": accept})
            for path, accept in requests
        ]


def test_operator_and_machine_404_behavior() -> None:
    paths = ("/", "/requests", "/search")
    operator, api, machine = anyio.run(
        _get_all,
        (
            ("/operator-page-that-does-not-exist", "text/html"),
            ("/api/route-that-does-not-exist", "text/html"),
            ("/operator-page-that-does-not-exist", "application/json"),
        ),
    )

    assert operator.status_code == 404
    assert operator.headers["content-type"].startswith("text/html")
    assert "Page Not Found · Pinpoint Sourcing LLC" in operator.text
    assert 'href="/">Dashboard</a>' in operator.text
    assert 'href="/requests">Inbox</a>' in operator.text
    assert 'href="/search">Search</a>' in operator.text
    assert (
        'rel="icon" type="image/webp" '
        'href="http://127.0.0.1:8000/static/pinpoint-logo-stacked.webp"'
    ) in operator.text

    assert (Path(__file__).parents[1] / "static" / "pinpoint-logo-stacked.webp").is_file()

    for response in (api, machine):
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {"detail": "Not Found"}

    assert tuple(
        str(app.url_path_for(name))
        for name in ("dashboard", "list_requests", "global_search_page")
    ) == paths

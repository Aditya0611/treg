import pytest

from app.normalize import normalize_domain


@pytest.fixture(autouse=True)
def offline_redirects(monkeypatch):
    """Domain checks stay on this machine. A test can patch a redirect when it needs one."""

    async def final_host(domain: str) -> str:
        return normalize_domain(domain) or ""

    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("app.pipeline._final_host", final_host)

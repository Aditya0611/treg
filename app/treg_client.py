"""HTTP client for routed Treg calls."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx

from app.filters import hunter_sentence, size_sentence
from app.places import country_code
from app.normalize import normalize_domain


class TregError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class CallResult:
    endpoint: str
    output: dict
    raw: object = None
    treg: dict = field(default_factory=dict)
    served_by: str = ""
    cost_micro: int = 0
    call_id: str = ""
    ignored_filters: str = ""

    @property
    def source(self) -> str:
        name = self.endpoint.removeprefix("treg.")
        return f"{name}:{self.served_by}" if self.served_by else name


class TregClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://treg.to",
        org: str = "",
        max_cost_usd: float = 0.05,
        waterfall: bool = True,
        timeout: float = 90,
    ):
        self.token = token.strip()
        self.base_url = base_url.rstrip("/")
        self.org = org.strip()
        self.max_cost_usd = max_cost_usd
        self.waterfall = waterfall
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._identify_lock = asyncio.Lock()
        self._identify_next = 0.0
        self.identify_interval_s = 2.1

    async def __aenter__(self) -> "TregClient":
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, *_) -> None:
        if self._client is not None:
            await self._client.aclose()

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {
            "X-Treg-Token": self.token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Treg-Route-Max-Cost": f"{self.max_cost_usd:.4f}",
        }
        if not self.waterfall:
            headers["X-Treg-Route-Waterfall"] = "0"
        if self.org:
            headers["X-Treg-Org"] = self.org
        if extra:
            headers.update(extra)
        return headers

    async def call(
        self,
        endpoint: str,
        body: dict,
        extra_headers: dict | None = None,
        *,
        method: str = "POST",
        as_query: bool = False,
    ) -> CallResult:
        if self._client is None:
            raise RuntimeError("TregClient must be used as an async context manager")
        url = f"{self.base_url}/call/{endpoint}"
        payload = {key: value for key, value in body.items() if value not in (None, "", [], {})}
        verb = method.upper()
        last_error: TregError | None = None
        for attempt in range(3):
            try:
                if verb == "GET" or as_query:
                    headers = self._headers(extra_headers)
                    headers.pop("Content-Type", None)
                    request = self._client.get if verb == "GET" else self._client.post
                    response = await request(url, params=payload, headers=headers)
                else:
                    response = await self._client.post(url, json=payload, headers=self._headers(extra_headers))
            except httpx.TransportError:
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise
            if response.status_code in {403, 429, 500, 502, 503, 504} and attempt < 2:
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            result = _parse_response(endpoint, response)
            if isinstance(result, TregError):
                last_error = result
                if result.status in {403, 429, 500, 502, 503, 504} and attempt < 2:
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                raise result
            return result
        raise last_error or TregError(0, "Treg did not answer")

    async def search_companies_sized(self, filters, page: int = 1, offset: int | None = None, page_limit: int | None = None) -> CallResult:
        """Hunter's free sentence search. The sentence is what applies the place and the headcount."""
        size = max(1, min(100, int(page_limit or getattr(filters, "limit", 10) or 10)))
        start = (max(1, page) - 1) * size if offset is None else max(0, int(offset))
        return await self.call(
            "hunter.x.discover-companies",
            {"query": hunter_sentence(filters), "limit": size, "offset": start},
        )

    async def search_companies_by_sentence(self, filters) -> CallResult:
        """Ask Tomba in a sentence. Structured size is premium and is often dropped."""
        return await self.call("tomba.companies.search.reveal", {"query": size_sentence(filters), "page": 1})
    async def search_with_agent(self, agent, spec, n: int, page: int = 1) -> CallResult:
        """One page from one search agent, called directly so Treg does not swap providers."""
        return await self.call(agent.id, agent.request(spec, n, page), method=agent.method, as_query=agent.as_query)


    async def search_companies(self, filters, *, exclude: str = "", query_only: bool = False) -> CallResult:
        body = {"limit": filters.limit}
        if filters.q:
            body["q"] = filters.q
        if filters.industry and not query_only:
            body["industry"] = filters.industry
        if filters.technology and not query_only:
            body["technology"] = filters.technology
        if filters.country:
            body["country"] = filters.country
        headers = {"X-Treg-Route-Exclude": exclude} if exclude else None
        try:
            return await self.call("treg.companies.search", body, headers)
        except TregError as exc:
            if exc.status == 422 and "industry" in body and "q" in body:
                body.pop("industry")
                return await self.call("treg.companies.search", body, headers)
            raise

    async def account_balance(self) -> float | None:
        """The team's balance, in dollars. None when billing cannot be read."""
        if self._client is None:
            return None
        try:
            response = await self._client.get(f"{self.base_url}/billing", headers=self._headers())
        except httpx.TransportError:
            return None
        if response.status_code >= 400:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        value = data.get("balance_usd") if isinstance(data, dict) else None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def count_matches(self, filters) -> int | None:
        """A free count of the audience, before Apollo charges for a page."""
        body: dict = {}
        code = country_code(getattr(filters, "country", "") or "") or (getattr(filters, "country", "") or "")
        if code:
            body["countries"] = [code]
        if getattr(filters, "state", ""):
            try:
                looked = await self.call(
                    "companyenrich.geo.states.search",
                    {"query": filters.state, "countryCodes": body.get("countries") or ["US"]},
                )
            except TregError:
                looked = None
            if looked is not None:
                ident = _state_id(looked.output, filters.state) or _state_id(looked.raw, filters.state)
                if ident is not None:
                    body["states"] = [ident]
        if filters.employees_min is not None and filters.employees_max is not None:
            body["employees"] = [f"{int(filters.employees_min)}-{int(filters.employees_max)}"]
        try:
            result = await self.call("companyenrich.companies.search.count", body)
        except TregError:
            return None
        return _count_total(result.output) if _count_total(result.output) is not None else _count_total(result.raw)

    async def covered_domains(self, domains: list[str]) -> set[str] | None:
        """Ocean's free coverage check. None means the answer could not be read."""
        if not domains:
            return set()
        result = await self.call("oceanio.companies.warmup", {"domains": domains})
        return _successful_domains(result.output) if _successful_domains(result.output) is not None else _successful_domains(result.raw)

    async def us_headcount(self, linkedin_url: str) -> CallResult:
        """BlitzAPI's count by country. It needs the LinkedIn page crawl4ai already found."""
        return await self.call(
            "blitzapi.companies.employees.by-country",
            {"company_linkedin_url": linkedin_url},
        )

    async def naics_for(self, domain: str) -> CallResult:
        """Context.dev NAICS codes. Software publishers stay; IT services do not."""
        return await self.call("branddev.brand.naics", {"input": domain}, method="GET")

    async def find_business(self, query: str) -> CallResult:
        """Openmart's business search. A small limit keeps the per-result charge down."""
        return await self.call("openmart.businesses.search", {"query": query, "limit": 3})

    async def enrich(self, domain: str, name: str = "") -> CallResult:
        """crawl4ai supplies the street and the description. Its employee count is not used."""
        try:
            return await self.call("crawl4ai.companies.enrich", {"domain": domain}, method="GET")
        except TregError as exc:
            if exc.status == 404:
                return CallResult(endpoint="crawl4ai.companies.enrich", output={}, cost_micro=0)
            raise

    async def identify_company(self, domain: str) -> CallResult:
        """Crustdata's free identity lookup. Its limit is 30 calls a minute, so calls are spaced out."""
        return await self._identify({"domains": [domain]})

    async def identify_companies(self, domains: list[str]) -> CallResult:
        """Several domains in one identity call; each answer row names the domain it matched."""
        return await self._identify({"domains": list(domains)})

    async def identify_linkedin(self, url: str) -> CallResult:
        return await self._identify({"professional_network_profile_urls": [url]})

    async def _identify(self, body: dict) -> CallResult:
        async with self._identify_lock:
            wait = self._identify_next - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._identify_next = time.monotonic() + self.identify_interval_s
        return await self.call("crustdata.companies.identify", body)

    async def linkedin_page(self, company: str) -> CallResult:
        """What the company's own LinkedIn page says: size, headquarters, website, industry."""
        return await self.call("crawl4ai.linkedin.company.profile", {"company": company})

    async def locate_company(self, domain: str) -> CallResult:
        """leadocean's headquarters city, state, and country for one domain."""
        return await self.call("leadocean.companies.enrich", {"domain": domain})

    async def fill_company(self, domain: str) -> CallResult:
        """Dropleads supplies employees, location, LinkedIn, phone, and revenue."""
        result = await self.call("dropleads.companies.enrich", {"domains": [domain]})
        rows = _dropleads_companies(result.output) or _dropleads_companies(result.raw)
        if not rows:
            if not (isinstance(result.output, dict) and (result.output.get("domain") or result.output.get("name"))):
                result.output = {}
            return result
        wanted = normalize_domain(domain)
        chosen = next(
            (
                row
                for row in rows
                if normalize_domain(str(row.get("domain") or row.get("websiteUrl") or row.get("website") or "")) == wanted
            ),
            None,
        )
        result.output = chosen or rows[0]
        return result

    async def search_people(self, domain: str, *, limit: int = 9, title: str = "") -> CallResult:
        body = {"company_domain": domain, "limit": max(1, min(9, limit))}
        if title:
            body["title"] = title
        return await self.call("treg.people.search", body)

    async def find_email(self, *, domain: str, first: str, last: str, linkedin: str, verify: bool) -> CallResult:
        body: dict = {"domain": domain}
        if first and last:
            body["first_name"] = first
            body["last_name"] = last
        elif first or last:
            body["full_name"] = " ".join(part for part in (first, last) if part)
        if linkedin:
            body["linkedin_url"] = linkedin
        headers = {"X-Treg-Route-Verify": "true"} if verify else None
        return await self.call("treg.people.email.find", body, headers)

    async def find_phone(self, *, domain: str, first: str, last: str, linkedin: str, email: str) -> CallResult:
        body: dict = {}
        if linkedin:
            body["linkedin_url"] = linkedin
        if email:
            body["email"] = email
        if domain and first and last:
            body["domain"] = domain
            body["first_name"] = first
            body["last_name"] = last
        return await self.call("treg.people.phone.find", body)


def _count_total(payload) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in ("total", "count"):
        value = payload.get(key)
        if isinstance(value, int):
            return value
    for key in ("data", "output"):
        nested = _count_total(payload.get(key))
        if nested is not None:
            return nested
    return None


def _state_id(payload, name: str) -> int | None:
    wanted = " ".join((name or "").strip().lower().split())
    if not wanted:
        return None
    found: list[int] = []

    def walk(node, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(node, dict):
            label = " ".join(str(node.get("name") or node.get("state") or "").lower().split())
            ident = node.get("id")
            if label and wanted in label and isinstance(ident, int):
                found.append(ident)
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:40]:
                walk(item, depth + 1)

    walk(payload)
    return found[0] if found else None


def _apollo_place(filters) -> str:
    country = country_code(getattr(filters, "country", "") or "") or (getattr(filters, "country", "") or "")
    if country == "US":
        country = "US"
    return ", ".join(part for part in (getattr(filters, "city", ""), getattr(filters, "state", ""), country) if part)


def _successful_domains(payload) -> set[str] | None:
    found: set[str] | None = None

    def walk(node, depth: int = 0) -> None:
        nonlocal found
        if depth > 5:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in {"successfuldomains", "successful_domains"} and isinstance(value, list):
                    found = {normalize_domain(item) for item in value if isinstance(item, str) and normalize_domain(item)}
                else:
                    walk(value, depth + 1)
        elif isinstance(node, list) and depth < 3:
            for item in node[:200]:
                walk(item, depth + 1)

    walk(payload)
    return found


def _dropleads_companies(payload) -> list[dict]:
    found: list[dict] = []

    def walk(node, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() == "companies" and isinstance(value, list):
                    found.extend(
                        row for row in value
                        if isinstance(row, dict) and (row.get("domain") or row.get("linkedinUrl") or row.get("linkedinurl"))
                    )
                else:
                    walk(value, depth + 1)
        elif isinstance(node, list) and depth < 3:
            for item in node[:30]:
                walk(item, depth + 1)

    walk(payload)
    return found


def _message(status: int, body) -> str:
    if isinstance(body, dict):
        for key in ("message", "detail", "error"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
            if isinstance(value, dict):
                nested = value.get("message") or value.get("detail")
                if nested:
                    return str(nested)[:500]
        treg = body.get("_treg")
        if isinstance(treg, dict) and treg.get("error"):
            return str(treg["error"])[:500]
    return f"Treg returned HTTP {status}"


def _parse_response(endpoint: str, response: httpx.Response) -> CallResult | TregError:
    try:
        body = response.json()
    except ValueError:
        body = {"message": response.text[:500]}
    if response.status_code >= 400 or response.status_code == 202:
        return TregError(response.status_code, _message(response.status_code, body))
    output = {}
    raw = body
    treg = {}
    if isinstance(body, dict):
        if isinstance(body.get("output"), dict):
            output = body["output"]
            raw = body.get("raw")
        elif isinstance(body.get("output"), list):
            output = {"rows": body["output"]}
            raw = body.get("raw")
        elif isinstance(body.get("data"), list):
            # Hunter discover-companies returns {data: [...], meta: {...}} with no output key.
            output = {"companies": body["data"], "meta": body.get("meta") or {}}
            raw = body
        elif isinstance(body.get("organizations"), list):
            output = {"companies": body["organizations"]}
            raw = body
        elif isinstance(body.get("companies"), list):
            output = body
            raw = body
        treg = body.get("_treg") if isinstance(body.get("_treg"), dict) else {}
    served = response.headers.get("x-treg-served-by") or str(treg.get("served_by") or "")
    cost_header = response.headers.get("x-treg-cost-micro")
    if cost_header not in (None, ""):
        cost = int(float(cost_header))
    elif treg.get("charged_micro") is not None:
        cost = int(treg["charged_micro"])
    else:
        cost = 0
    return CallResult(
        endpoint=endpoint,
        output=output,
        raw=raw,
        treg=treg,
        served_by=served,
        cost_micro=cost,
        call_id=response.headers.get("x-treg-call-id") or str(treg.get("call_id") or ""),
        ignored_filters=response.headers.get("x-treg-ignored-filters") or "",
    )

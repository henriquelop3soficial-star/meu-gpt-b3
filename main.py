import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent
FUNDAMENTALS_DIR = ROOT_DIR / "data" / "fundamentals"
QUARTERLY_DIR = ROOT_DIR / "data" / "quarterly"
HISTORY_DIR = ROOT_DIR / "data" / "history"


class SourceMetadata(BaseModel):
    provider: str
    retrieved_at: str
    demo_mode: bool


class HealthResponse(BaseModel):
    status: str
    bolsai_configured: bool
    demo_mode: bool


class QuoteResponse(BaseModel):
    asset: str
    source: SourceMetadata
    raw_data: dict[str, Any]


class HistoryResponse(BaseModel):
    asset: str
    years_requested: int
    source: SourceMetadata
    raw_data: dict[str, Any]


class DossierResponse(BaseModel):
    asset: str
    years_requested: int
    source: SourceMetadata
    market_data: dict[str, Any]
    reports: dict[str, Any]
    data_quality: dict[str, Any]


class DividendsResponse(BaseModel):
    asset: str
    source: SourceMetadata
    raw_data: dict[str, Any]


class FundamentalsResponse(BaseModel):
    asset: str
    as_of: str
    period_start: str
    period_end: str
    metrics: dict[str, float | None]
    formulas: dict[str, str]
    source_status: dict[str, str]
    raw_data: dict[str, Any]


class RootResponse(BaseModel):
    service: str
    docs: str
    openapi: str


app = FastAPI(
    title="GPT Coletor B3 API",
    version="1.0.0",
    description="API privada que coleta, valida e organiza dados da B3 via BolsAI e CVM para um GPT personalizado.",
    servers=[
        {
            "url": os.getenv("PUBLIC_BASE_URL", "https://meu-gpt-b3.onrender.com").rstrip("/"),
            "description": "Servidor de produção",
        }
    ],
)

api_key_header = APIKeyHeader(name="X-API-Key", scheme_name="ApiKeyAuth", auto_error=False)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def demo_mode() -> bool:
    return env("DEMO_MODE").lower() == "true"


def require_api_key(x_api_key: str | None = Security(api_key_header)) -> None:
    expected = env("API_ACCESS_KEY")
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="API_ACCESS_KEY não configurada.")
    if x_api_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Chave de acesso inválida.")


def validate_ticker(ticker: str) -> str:
    normalized = ticker.strip().upper()
    if not normalized.replace("^", "").isalnum() or len(normalized) > 12:
        raise HTTPException(status_code=422, detail="Ticker inválido.")
    return normalized


def bolsai_configured() -> bool:
    return bool(env("BOLSAI_API_KEY"))


def bolsai_headers() -> dict[str, str]:
    return {"X-API-Key": env("BOLSAI_API_KEY"), "Accept": "application/json"}


def historical_params(years: int) -> dict[str, str | int]:
    end_date = date.today()
    try:
        start_date = end_date.replace(year=end_date.year - years)
    except ValueError:  # Ajusta 29/02 quando o ano inicial não é bissexto.
        start_date = end_date.replace(year=end_date.year - years, day=28)
    return {"start": start_date.isoformat(), "end": end_date.isoformat(), "limit": 5000}


def source_metadata(provider: str = "BolsAI") -> SourceMetadata:
    return SourceMetadata(
        provider=provider,
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        demo_mode=demo_mode(),
    )


async def bolsai_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    if not bolsai_configured():
        raise HTTPException(status_code=503, detail="BOLSAI_API_KEY não configurada no servidor.")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            base_url = env("BOLSAI_BASE_URL", "https://api.usebolsai.com/api/v1").rstrip("/")
            response = await client.get(f"{base_url}{path}", headers=bolsai_headers(), params=params or {})
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Ativo não encontrado na BolsAI.") from error
        if error.response.status_code == 429:
            raise HTTPException(status_code=429, detail="Limite de requisições da BolsAI atingido.") from error
        raise HTTPException(status_code=502, detail=f"BolsAI retornou HTTP {error.response.status_code}.") from error
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(status_code=502, detail="Não foi possível consultar a BolsAI.") from error
    return payload if isinstance(payload, dict) else {"data": payload}


async def optional_bolsai_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return await bolsai_get(path, params)
    except HTTPException as error:
        return {"_error": error.detail, "_status": error.status_code}


def bolsai_data(payload: dict[str, Any]) -> Any:
    if "_error" in payload:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def load_cvm_financials(ticker: str) -> dict[str, Any] | None:
    """Lê o retrato anual gerado a partir dos DFPs públicos da CVM."""
    snapshot_path = FUNDAMENTALS_DIR / f"{ticker}.json"
    if not snapshot_path.exists():
        return None
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def load_cvm_interim_financials(ticker: str) -> dict[str, Any] | None:
    """Lê os resultados intermediários gerados a partir dos ITRs públicos da CVM."""
    snapshot_path = QUARTERLY_DIR / f"{ticker}.json"
    if not snapshot_path.exists():
        return None
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def annual_records(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not snapshot:
        return []
    records = snapshot.get("annual_financials")
    if not isinstance(records, list):
        return []
    valid = [item for item in records if isinstance(item, dict) and isinstance(item.get("year"), int)]
    return sorted(valid, key=lambda item: item["year"])


def interim_records(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not snapshot:
        return []
    records = snapshot.get("interim_financials")
    if not isinstance(records, list):
        return []
    valid = [item for item in records if isinstance(item, dict) and isinstance(item.get("reference_date"), str)]
    return sorted(valid, key=lambda item: item["reference_date"])


def load_b3_history(ticker: str) -> dict[str, Any] | None:
    snapshot_path = HISTORY_DIR / f"{ticker}.json"
    if not snapshot_path.exists():
        return None
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def b3_history_for_period(snapshot: dict[str, Any], years: int) -> dict[str, Any] | None:
    prices = snapshot.get("prices")
    if not isinstance(prices, list):
        return None
    valid = [item for item in prices if isinstance(item, dict) and isinstance(item.get("date"), str) and number(item.get("close")) is not None]
    if not valid:
        return None
    valid.sort(key=lambda item: item["date"])
    end = date.today()
    try:
        start = end.replace(year=end.year - years)
    except ValueError:
        start = end.replace(year=end.year - years, day=28)
    selected = [item for item in valid if start.isoformat() <= item["date"] <= end.isoformat()]
    if not selected:
        return None
    first_close = number(selected[0].get("close"))
    last_close = number(selected[-1].get("close"))
    return {
        "requested_years": years,
        "available_years": round((date.fromisoformat(selected[-1]["date"]) - date.fromisoformat(selected[0]["date"])).days / 365.25, 2),
        "source": snapshot.get("source"),
        "first_date": selected[0]["date"],
        "last_date": selected[-1]["date"],
        "first_close": first_close,
        "last_close": last_close,
        "price_return_percent": (last_close / first_close - 1) * 100 if first_close and last_close is not None else None,
        "prices": selected,
    }


def payload_status(payload: dict[str, Any], data: Any) -> str:
    if "_error" in payload:
        return f"indisponível: {payload['_error']}"
    return "ok" if data is not None else "indisponível: resposta sem dados"


def demo_quote(ticker: str) -> dict[str, Any]:
    return {"ticker": ticker, "price": None, "currency": "BRL", "notice": "Modo demonstração."}


@app.get("/health", tags=["Status"], operation_id="health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", bolsai_configured=bolsai_configured(), demo_mode=demo_mode())


@app.get(
    "/v1/assets/{ticker}/quote",
    tags=["Mercado"],
    operation_id="getAssetQuote",
    response_model=QuoteResponse,
)
async def quote(ticker: str, _: None = Depends(require_api_key)) -> QuoteResponse:
    ticker = validate_ticker(ticker)
    raw_data = demo_quote(ticker) if demo_mode() else await bolsai_get(f"/stocks/{ticker}/quote")
    return QuoteResponse(asset=ticker, source=source_metadata(), raw_data=raw_data)


@app.get(
    "/v1/assets/{ticker}/history",
    tags=["Mercado"],
    operation_id="getAssetHistory",
    response_model=HistoryResponse,
)
async def history(
    ticker: str,
    years: int = Query(default=3, ge=1, le=10),
    _: None = Depends(require_api_key),
) -> HistoryResponse:
    ticker = validate_ticker(ticker)
    if demo_mode():
        raw_data = {"ticker": ticker, "years": years, "items": [], "notice": "Modo demonstração."}
    else:
        payload = await optional_bolsai_get(f"/stocks/{ticker}/history", historical_params(years))
        raw_data = {
            "requested_years": years,
            "history": bolsai_data(payload),
            "notice": "Histórico solicitado à BolsAI com preços ajustados conforme a metodologia do provedor.",
        }
        if "_error" in payload:
            raw_data["available_years"] = 0
            raw_data["notice"] = f"Histórico indisponível nesta consulta: {payload['_error']}"
        else:
            raw_data["available_years"] = years
    return HistoryResponse(asset=ticker, years_requested=years, source=source_metadata(), raw_data=raw_data)


@app.get(
    "/v1/assets/{ticker}/fundamentals",
    tags=["Fundamentos"],
    operation_id="getAssetFundamentals",
    summary="Consultar indicadores fundamentalistas e dividendos de uma ação",
    response_model=FundamentalsResponse,
)
async def fundamentals(ticker: str, _: None = Depends(require_api_key)) -> FundamentalsResponse:
    ticker = validate_ticker(ticker)
    if demo_mode():
        raise HTTPException(status_code=503, detail="Fundamentos não estão disponíveis no modo demonstração.")

    snapshot = load_cvm_financials(ticker)
    records = annual_records(snapshot)
    interim_snapshot = load_cvm_interim_financials(ticker)
    interim = interim_records(interim_snapshot)
    bolsai_payload = await optional_bolsai_get(f"/fundamentals/{ticker}")
    bolsai = bolsai_data(bolsai_payload)
    if not isinstance(bolsai, dict) and not records:
        raise HTTPException(
            status_code=404,
            detail="Fundamentos não localizados na BolsAI nem nos arquivos CVM importados.",
        )

    bolsai = bolsai if isinstance(bolsai, dict) else {}
    current = records[-1] if records else {}
    previous = records[-2] if len(records) > 1 else None
    latest_interim = interim[-1] if interim else None
    price = number(bolsai.get("close_price"))
    market_cap = number(bolsai.get("market_cap"))
    eps = number(bolsai.get("lpa")) or number(current.get("earnings_per_share"))
    equity = number(current.get("equity"))
    net_income = number(current.get("net_income"))
    previous_equity = number(previous.get("equity")) if previous else None
    average_equity = (equity + previous_equity) / 2 if equity is not None and previous_equity is not None else None
    roe_annual = net_income / average_equity * 100 if net_income is not None and average_equity and average_equity > 0 else None
    # BolsAI calcula os múltiplos no ticker/classe consultado. Não recriamos
    # P/VP com preço de uma classe multiplicado por todas as ações da companhia.
    price_to_earnings = number(bolsai.get("pl"))
    price_to_book = number(bolsai.get("pvp"))

    metrics = {
        "price": price,
        "market_cap": market_cap,
        "price_to_earnings": price_to_earnings,
        "price_to_book": price_to_book,
        "roe_percent": number(bolsai.get("roe")),
        "roe_percent_annual_cvm": roe_annual,
        "earnings_per_share": eps,
        "book_value_per_share": number(bolsai.get("vpa")),
        "ev_ebitda": number(bolsai.get("ev_ebitda")),
        "net_debt_ebitda": number(bolsai.get("net_debt_ebitda")),
        "net_margin_percent": number(bolsai.get("net_margin")),
        "roic_percent": number(bolsai.get("roic")),
        "revenue_last_annual": number(current.get("revenue")),
        "net_income_last_annual": net_income,
        "equity": equity,
        "total_debt": (number(current.get("short_term_debt")) or 0.0) + (number(current.get("long_term_debt")) or 0.0)
        if number(current.get("short_term_debt")) is not None or number(current.get("long_term_debt")) is not None else None,
        "total_cash": number(current.get("cash_and_equivalents")),
        "annual_shares_outstanding": number((current.get("capital_composition") or {}).get("shares_outstanding")),
        "dividends_per_share_ttm": None,
        "payout_percent_ttm": None,
        "dividend_yield_percent_ttm": number(bolsai.get("dividend_yield")),
        "latest_interim_revenue_year_to_date": number(latest_interim.get("revenue_year_to_date")) if latest_interim else None,
        "latest_interim_net_income_year_to_date": number(latest_interim.get("net_income_year_to_date")) if latest_interim else None,
        "latest_interim_equity": number(latest_interim.get("equity")) if latest_interim else None,
        "latest_interim_cash": number(latest_interim.get("cash_and_equivalents")) if latest_interim else None,
    }
    status_by_source = {
        "bolsai_fundamentals": payload_status(bolsai_payload, bolsai),
        "cvm_dfp": "ok: demonstrações anuais públicas importadas" if records else "não importado para o ativo",
        "cvm_itr": "ok: ITR público mais recente importado" if latest_interim else "não integrado ou não disponível para o ativo",
        "dividends": "consulte getAssetDividends para histórico, eventos e valores por ação.",
    }
    current_year = current.get("year")
    return FundamentalsResponse(
        asset=ticker,
        as_of=datetime.now(timezone.utc).isoformat(),
        period_start=f"{current_year}-01-01" if current_year else "",
        period_end=f"{current_year}-12-31" if current_year else "",
        metrics=metrics,
        formulas={
            "price_to_earnings": "múltiplo P/L informado pela BolsAI para o ticker/classe consultado",
            "price_to_book": "múltiplo P/VP informado pela BolsAI para o ticker/classe consultado",
            "roe_percent": "ROE informado pela BolsAI; roe_percent_annual_cvm usa lucro anual ÷ patrimônio líquido médio × 100",
        },
        source_status=status_by_source,
        raw_data={
            "bolsai_fundamentals": bolsai_payload,
            "cvm_financials": snapshot,
            "cvm_itr": interim_snapshot,
        },
    )


@app.get(
    "/v1/assets/{ticker}/dividends",
    tags=["Proventos"],
    operation_id="getAssetDividends",
    summary="Consultar dividendos e JCP de uma ação",
    response_model=DividendsResponse,
)
async def dividends(ticker: str, _: None = Depends(require_api_key)) -> DividendsResponse:
    ticker = validate_ticker(ticker)
    if demo_mode():
        raw_data = {"ticker": ticker, "notice": "Proventos indisponíveis no modo demonstração."}
    else:
        raw_data = await bolsai_get(f"/dividends/{ticker}")
    return DividendsResponse(asset=ticker, source=source_metadata(), raw_data=raw_data)


@app.get(
    "/v1/assets/{ticker}/dossier",
    tags=["Dossiê"],
    operation_id="getAssetDossier",
    response_model=DossierResponse,
)
async def dossier(
    ticker: str,
    years: int = Query(default=3, ge=1, le=10),
    _: None = Depends(require_api_key),
) -> DossierResponse:
    ticker = validate_ticker(ticker)
    if demo_mode():
        quote_data = demo_quote(ticker)
        history_data = {"ticker": ticker, "years": years, "items": []}
        fundamentals_data = {}
        dividends_data = {}
    else:
        quote_data = await bolsai_get(f"/stocks/{ticker}/quote")
        # Histórico, dividendos e demonstrações podem depender do plano da
        # BolsAI. Um bloqueio de plano não deve inutilizar o dossiê inteiro.
        history_data = await optional_bolsai_get(f"/stocks/{ticker}/history", historical_params(years))
        fundamentals_data = await optional_bolsai_get(f"/fundamentals/{ticker}")
        dividends_data = await optional_bolsai_get(f"/dividends/{ticker}")
    return DossierResponse(
        asset=ticker,
        years_requested=years,
        source=source_metadata(),
        market_data={"quote": quote_data, "history": history_data, "fundamentals": fundamentals_data, "dividends": dividends_data},
        reports={"status": "not_integrated", "message": "Relatórios oficiais ainda exigem integração com RI, CVM e administradores."},
        data_quality={"do_not_infer_missing_values": True, "note": "Dados de mercado e indicadores retornados pela BolsAI; demonstrações CVM e documentos oficiais devem prevalecer em divergências."},
    )


@app.get("/", tags=["Status"], operation_id="root", response_model=RootResponse)
async def root() -> RootResponse:
    return RootResponse(service="GPT Coletor B3 API", docs="/docs", openapi="/openapi.json")

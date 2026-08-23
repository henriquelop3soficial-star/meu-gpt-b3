import asyncio
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


class SourceMetadata(BaseModel):
    provider: str
    retrieved_at: str
    demo_mode: bool


class HealthResponse(BaseModel):
    status: str
    brapi_configured: bool
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
    description="API privada que coleta dados da B3 via BRAPI para um GPT personalizado.",
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


def brapi_configured() -> bool:
    return bool(env("BRAPI_TOKEN"))


def brapi_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {env('BRAPI_TOKEN')}"}


def historical_params(ticker: str, years: int) -> dict[str, str]:
    end_date = date.today()
    try:
        start_date = end_date.replace(year=end_date.year - years)
    except ValueError:  # Ajusta 29/02 quando o ano inicial não é bissexto.
        start_date = end_date.replace(year=end_date.year - years, day=28)
    return {
        "symbols": ticker,
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "interval": "1d",
        "sortOrder": "asc",
    }


def date_window(ticker: str, years: int = 1) -> dict[str, str]:
    end_date = date.today()
    try:
        start_date = end_date.replace(year=end_date.year - years)
    except ValueError:
        start_date = end_date.replace(year=end_date.year - years, day=28)
    return {"symbols": ticker, "startDate": start_date.isoformat(), "endDate": end_date.isoformat()}


def source_metadata(provider: str = "BRAPI") -> SourceMetadata:
    return SourceMetadata(
        provider=provider,
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        demo_mode=demo_mode(),
    )


async def brapi_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    if not brapi_configured():
        raise HTTPException(status_code=503, detail="BRAPI_TOKEN não configurado no servidor.")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(f"https://brapi.dev{path}", headers=brapi_headers(), params=params)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Ativo não encontrado na BRAPI.") from error
        raise HTTPException(status_code=502, detail=f"BRAPI retornou HTTP {error.response.status_code}.") from error
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(status_code=502, detail="Não foi possível consultar a BRAPI.") from error
    return payload if isinstance(payload, dict) else {"data": payload}


async def optional_brapi_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        return await brapi_get(path, params)
    except HTTPException as error:
        return {"_error": error.detail, "_status": error.status_code}


def first_result_data(payload: dict[str, Any]) -> Any:
    if "_error" in payload:
        return None
    results = payload.get("results")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return None
    return results[0].get("data")


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


def annual_records(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not snapshot:
        return []
    records = snapshot.get("annual_financials")
    if not isinstance(records, list):
        return []
    valid = [item for item in records if isinstance(item, dict) and isinstance(item.get("year"), int)]
    return sorted(valid, key=lambda item: item["year"])


def payload_status(payload: dict[str, Any], data: Any) -> str:
    if "_error" in payload:
        return f"indisponível: {payload['_error']}"
    return "ok" if data is not None else "indisponível: resposta sem dados"


def demo_quote(ticker: str) -> dict[str, Any]:
    return {"ticker": ticker, "price": None, "currency": "BRL", "notice": "Modo demonstração."}


@app.get("/health", tags=["Status"], operation_id="health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", brapi_configured=brapi_configured(), demo_mode=demo_mode())


@app.get(
    "/v1/assets/{ticker}/quote",
    tags=["Mercado"],
    operation_id="getAssetQuote",
    response_model=QuoteResponse,
)
async def quote(ticker: str, _: None = Depends(require_api_key)) -> QuoteResponse:
    ticker = validate_ticker(ticker)
    raw_data = demo_quote(ticker) if demo_mode() else await brapi_get("/api/v2/stocks/quote", {"symbols": ticker})
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
    raw_data = (
        {"ticker": ticker, "years": years, "items": [], "notice": "Modo demonstração."}
        if demo_mode()
        else await brapi_get("/api/v2/stocks/historical", historical_params(ticker, years))
    )
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
    if not records:
        raise HTTPException(
            status_code=404,
            detail="Fundamentos CVM ainda não foram importados para este ativo. Adicione-o à tabela data/assets.csv e execute a importação DFP.",
        )

    quote_payload = await optional_brapi_get("/api/v2/stocks/quote", {"symbols": ticker})
    quote_data = first_result_data(quote_payload)
    quote = quote_data if isinstance(quote_data, dict) else {}
    current = records[-1]
    previous = records[-2] if len(records) > 1 else None
    price = number(quote.get("regularMarketPrice"))
    market_cap = number(quote.get("marketCap"))
    eps = number(current.get("earnings_per_share"))
    equity = number(current.get("equity"))
    net_income = number(current.get("net_income"))
    previous_equity = number(previous.get("equity")) if previous else None
    average_equity = (equity + previous_equity) / 2 if equity is not None and previous_equity is not None else None
    roe = net_income / average_equity * 100 if net_income is not None and average_equity and average_equity > 0 else None
    price_to_earnings = price / eps if price is not None and eps is not None and eps > 0 else None
    price_to_book = market_cap / equity if market_cap is not None and equity is not None and equity > 0 else None

    metrics = {
        "price": price,
        "price_to_earnings": price_to_earnings,
        "price_to_book": price_to_book,
        "roe_percent": roe,
        "earnings_per_share": eps,
        "revenue_last_annual": number(current.get("revenue")),
        "net_income_last_annual": net_income,
        "equity": equity,
        "total_debt": (number(current.get("short_term_debt")) or 0.0) + (number(current.get("long_term_debt")) or 0.0)
        if number(current.get("short_term_debt")) is not None or number(current.get("long_term_debt")) is not None else None,
        "total_cash": number(current.get("cash_and_equivalents")),
        "dividends_per_share_ttm": None,
        "payout_percent_ttm": None,
        "dividend_yield_percent_ttm": None,
    }
    status_by_source = {
        "quote": payload_status(quote_payload, quote_data),
        "cvm_dfp": "ok: demonstrações anuais públicas importadas",
        "dividends": "não integrado: fonte gratuita de proventos será adicionada em etapa posterior",
    }
    current_year = current["year"]
    return FundamentalsResponse(
        asset=ticker,
        as_of=datetime.now(timezone.utc).isoformat(),
        period_start=f"{current_year}-01-01",
        period_end=f"{current_year}-12-31",
        metrics=metrics,
        formulas={
            "price_to_earnings": "cotação atual ÷ lucro por ação anual divulgado no DFP",
            "price_to_book": "valor de mercado da cotação BRAPI ÷ patrimônio líquido anual divulgado no DFP",
            "roe_percent": "lucro líquido anual ÷ patrimônio líquido médio de dois exercícios × 100",
        },
        source_status=status_by_source,
        raw_data={
            "quote": quote_payload,
            "cvm_financials": snapshot,
        },
    )


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
    else:
        quote_data = await brapi_get("/api/v2/stocks/quote", {"symbols": ticker})
        history_data = await brapi_get("/api/v2/stocks/historical", historical_params(ticker, years))
    return DossierResponse(
        asset=ticker,
        years_requested=years,
        source=source_metadata(),
        market_data={"quote": quote_data, "history": history_data},
        reports={"status": "not_integrated", "message": "Relatórios oficiais ainda exigem integração com RI, CVM e administradores."},
        data_quality={"do_not_infer_missing_values": True, "note": "Dados retornados diretamente pela BRAPI."},
    )


@app.get("/", tags=["Status"], operation_id="root", response_model=RootResponse)
async def root() -> RootResponse:
    return RootResponse(service="GPT Coletor B3 API", docs="/docs", openapi="/openapi.json")

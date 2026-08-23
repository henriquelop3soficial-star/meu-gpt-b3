import os
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

load_dotenv()


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


class RootResponse(BaseModel):
    service: str
    docs: str
    openapi: str


app = FastAPI(
    title="GPT Coletor B3 API",
    version="0.3.0",
    description="API privada que consulta dados de mercado da B3 via BRAPI para um GPT personalizado.",
    servers=[
        {
            "url": os.getenv("PUBLIC_BASE_URL", "https://meu-gpt-b3.onrender.com").rstrip("/"),
            "description": "API privada em produção",
        }
    ],
)

api_key_header = APIKeyHeader(name="X-API-Key", scheme_name="ApiKeyAuth", auto_error=False)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def demo_mode() -> bool:
    return env("DEMO_MODE").lower() == "true"


def brapi_configured() -> bool:
    return bool(env("BRAPI_TOKEN"))


def brapi_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {env('BRAPI_TOKEN')}"}


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


def source_metadata() -> SourceMetadata:
    return SourceMetadata(
        provider="BRAPI",
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


def demo_quote(ticker: str) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "price": None,
        "currency": "BRL",
        "market_status": "demo",
        "notice": "Modo demonstração: configure BRAPI_TOKEN para obter dados reais.",
    }


@app.get("/health", tags=["Status"], operation_id="health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", brapi_configured=brapi_configured(), demo_mode=demo_mode())


@app.get(
    "/v1/assets/{ticker}/quote",
    tags=["Mercado"],
    operation_id="getAssetQuote",
    summary="Consultar cotação de um ativo B3",
    response_model=QuoteResponse,
)
async def quote(ticker: str, _: None = Depends(require_api_key)) -> QuoteResponse:
    ticker = validate_ticker(ticker)
    raw = demo_quote(ticker) if demo_mode() else await brapi_get("/api/v2/stocks/quote", {"symbols": ticker})
    return QuoteResponse(asset=ticker, source=source_metadata(), raw_data=raw)


@app.get(
    "/v1/assets/{ticker}/history",
    tags=["Mercado"],
    operation_id="getAssetHistory",
    summary="Consultar histórico de preços de um ativo B3",
    response_model=HistoryResponse,
)
async def history(
    ticker: str,
    years: int = Query(default=3, ge=1, le=10),
    _: None = Depends(require_api_key),
) -> HistoryResponse:
    ticker = validate_ticker(ticker)
    raw = (
        {"ticker": ticker, "years": years, "items": [], "notice": "Modo demonstração."}
        if demo_mode()
        else await brapi_get("/api/v2/stocks/historical", {"symbols": ticker, "range": f"{years}y", "interval": "1d"})
    )
    return HistoryResponse(asset=ticker, years_requested=years, source=source_metadata(), raw_data=raw)


@app.get(
    "/v1/assets/{ticker}/dossier",
    tags=["Dossiê"],
    operation_id="getAssetDossier",
    summary="Montar dossiê de mercado de um ativo B3",
    response_model=DossierResponse,
)
async def dossier(
    ticker: str,
    years: int = Query(default=3, ge=1, le=10),
    _: None = Depends(require_api_key),
) -> DossierResponse:
    ticker = validate_ticker(ticker)
    if demo_mode():
        current = demo_quote(ticker)
        price_history = {"ticker": ticker, "years": years, "items": []}
    else:
        current = await brapi_get("/api/v2/stocks/quote", {"symbols": ticker})
        price_history = await brapi_get(
            "/api/v2/stocks/historical", {"symbols": ticker, "range": f"{years}y", "interval": "1d"}
        )

    return DossierResponse(
        asset=ticker,
        years_requested=years,
        source=source_metadata(),
        market_data={"quote": current, "history": price_history},
        reports={
            "status": "pending_source_integration",
            "message": "Relatórios oficiais serão adicionados pela integração com RI, CVM e administradores de FIIs.",
        },
        data_quality={
            "note": "Os campos permanecem brutos para preservar os dados retornados pela BRAPI.",
            "do_not_infer_missing_values": True,
        },
    )


@app.get("/", tags=["Status"], operation_id="root", response_model=RootResponse)
async def root() -> RootResponse:
    return RootResponse(service="GPT Coletor B3 API", docs="/docs", openapi="/openapi.json")

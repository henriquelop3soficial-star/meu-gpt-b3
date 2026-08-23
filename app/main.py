import os
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Security, status
from fastapi.security import APIKeyHeader
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(
    title="GPT Coletor B3 API",
    version="0.2.0",
    description="API privada que normaliza dados de mercado da B3 para um GPT personalizado.",
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


def require_api_key(x_api_key: str | None = Security(api_key_header)) -> None:
    expected = env("API_ACCESS_KEY")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_ACCESS_KEY não configurada no servidor.",
        )
    if x_api_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Chave de acesso inválida.")


def rapid_configured() -> bool:
    return all((env("RAPIDAPI_KEY"), env("RAPIDAPI_HOST"), env("RAPIDAPI_BASE_URL")))


def rapid_headers() -> dict[str, str]:
    return {"X-RapidAPI-Key": env("RAPIDAPI_KEY"), "X-RapidAPI-Host": env("RAPIDAPI_HOST")}


async def rapid_get(path_template: str, ticker: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    if not rapid_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RapidAPI não configurada. Preencha RAPIDAPI_KEY, RAPIDAPI_HOST e RAPIDAPI_BASE_URL.",
        )
    if not path_template:
        raise HTTPException(status_code=501, detail="Endpoint da RapidAPI ainda não configurado.")

    path = path_template.format(ticker=ticker)
    url = env("RAPIDAPI_BASE_URL").rstrip("/") + "/" + path.lstrip("/")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=rapid_headers(), params=params)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as error:
        raise HTTPException(status_code=502, detail=f"RapidAPI retornou HTTP {error.response.status_code}.") from error
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(status_code=502, detail="Não foi possível consultar a RapidAPI.") from error

    return payload if isinstance(payload, dict) else {"data": payload}


def demo_quote(ticker: str) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "price": None,
        "currency": "BRL",
        "market_status": "demo",
        "notice": "Modo demonstração: conecte a RapidAPI para obter dados reais.",
    }


def source_metadata() -> dict[str, Any]:
    return {
        "provider": "RapidAPI - B3 Boletim Diário",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "demo_mode": demo_mode(),
    }


def validate_ticker(ticker: str) -> str:
    normalized = ticker.strip().upper()
    if not normalized.replace("^", "").isalnum() or len(normalized) > 12:
        raise HTTPException(status_code=422, detail="Ticker inválido.")
    return normalized


@app.get("/health", tags=["Status"], operation_id="health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "rapidapi_configured": rapid_configured(), "demo_mode": demo_mode()}


@app.get(
    "/v1/assets/{ticker}/quote",
    tags=["Mercado"],
    operation_id="getAssetQuote",
    summary="Consultar cotação de um ativo B3",
)
async def quote(ticker: str, _: None = Depends(require_api_key)) -> dict[str, Any]:
    ticker = validate_ticker(ticker)
    raw = demo_quote(ticker) if demo_mode() else await rapid_get(env("RAPIDAPI_QUOTE_PATH"), ticker)
    return {"asset": ticker, "source": source_metadata(), "raw_data": raw}


@app.get(
    "/v1/assets/{ticker}/history",
    tags=["Mercado"],
    operation_id="getAssetHistory",
    summary="Consultar histórico de preços de um ativo B3",
)
async def history(
    ticker: str,
    years: int = Query(default=3, ge=1, le=10),
    _: None = Depends(require_api_key),
) -> dict[str, Any]:
    ticker = validate_ticker(ticker)
    raw = {"ticker": ticker, "years": years, "items": [], "notice": "Modo demonstração."} if demo_mode() else await rapid_get(
        env("RAPIDAPI_HISTORY_PATH"), ticker, {"years": years}
    )
    return {"asset": ticker, "years_requested": years, "source": source_metadata(), "raw_data": raw}


@app.get(
    "/v1/assets/{ticker}/dossier",
    tags=["Dossiê"],
    operation_id="getAssetDossier",
    summary="Montar dossiê de mercado de um ativo B3",
)
async def dossier(
    ticker: str,
    years: int = Query(default=3, ge=1, le=10),
    _: None = Depends(require_api_key),
) -> dict[str, Any]:
    ticker = validate_ticker(ticker)
    if demo_mode():
        current, price_history = demo_quote(ticker), {"ticker": ticker, "years": years, "items": []}
    else:
        current = await rapid_get(env("RAPIDAPI_QUOTE_PATH"), ticker)
        price_history = await rapid_get(env("RAPIDAPI_HISTORY_PATH"), ticker, {"years": years})

    return {
        "asset": ticker,
        "years_requested": years,
        "source": source_metadata(),
        "market_data": {"quote": current, "history": price_history},
        "reports": {
            "status": "pending_source_integration",
            "message": "Relatórios oficiais serão adicionados pela integração com RI, CVM e administradores de FIIs.",
        },
        "data_quality": {
            "note": "Os campos permanecem brutos até mapeamento dos endpoints reais da RapidAPI.",
            "do_not_infer_missing_values": True,
        },
    }


@app.get("/", tags=["Status"], operation_id="root")
async def root() -> dict[str, str]:
    return {"service": "GPT Coletor B3 API", "docs": "/docs", "openapi": "/openapi.json"}

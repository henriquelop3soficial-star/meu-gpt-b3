import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status

load_dotenv()

http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global http_client
    http_client = httpx.AsyncClient(timeout=15.0)
    yield
    await http_client.aclose()


app = FastAPI(
    title="GPT Coletor B3 API",
    version="0.2.0",
    description="API privada que normaliza dados da B3 via BRAPI para um GPT.",
    lifespan=lifespan,
)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def demo_mode() -> bool:
    return env("DEMO_MODE").lower() == "true"


def get_http_client() -> httpx.AsyncClient:
    if http_client is None:
        raise HTTPException(status_code=500, detail="Cliente HTTP não inicializado.")
    return http_client


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = env("API_ACCESS_KEY")
    if not expected:
        raise HTTPException(status_code=503, detail="API_ACCESS_KEY não configurada.")
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Chave de acesso inválida.")


async def brapi_get_quote(
    ticker: str,
    range_period: str | None = None,
    interval: str | None = None,
    client: httpx.AsyncClient = Depends(get_http_client),
) -> dict[str, Any]:
    token = env("BRAPI_TOKEN")
    if not token:
        raise HTTPException(status_code=503, detail="BRAPI_TOKEN não configurado.")

    url = "https://brapi.dev/api/v2/stocks/quote"
    headers = {"Authorization": f"Bearer {token}"}
    params: dict[str, Any] = {"symbols": ticker}

    if range_period:
        params["range"] = range_period
    if interval:
        params["interval"] = interval

    try:
        response = await client.get(url, headers=headers, params=params)
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results")
        if not results or not isinstance(results, list):
            raise HTTPException(status_code=502, detail="Resposta inválida da BRAPI.")
        return results[0]
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Ativo '{ticker}' não encontrado.") from error
        raise HTTPException(status_code=502, detail=f"Erro BRAPI: HTTP {error.response.status_code}") from error
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(status_code=502, detail="Não foi possível consultar a BRAPI.") from error


def source_metadata() -> dict[str, Any]:
    return {
        "provider": "BRAPI - Ações, FIIs e Índices",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "demo_mode": demo_mode(),
    }


def validate_ticker(ticker: str) -> str:
    normalized = ticker.strip().upper()
    if not normalized.replace("^", "").isalnum() or len(normalized) > 12:
        raise HTTPException(status_code=422, detail="Ticker inválido.")
    return normalized


@app.get("/health", tags=["Status"])
async def health() -> dict[str, Any]:
    return {"status": "ok", "demo_mode": demo_mode()}


@app.get("/v1/assets/{ticker}/quote", tags=["Mercado"])
async def quote(
    ticker: str,
    client: httpx.AsyncClient = Depends(get_http_client),
    _: None = Depends(require_api_key),
) -> dict[str, Any]:
    ticker = validate_ticker(ticker)
    data = await brapi_get_quote(ticker, client=client)
    return {"asset": ticker, "source": source_metadata(), "data": data}
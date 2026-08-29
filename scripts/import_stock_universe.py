"""Atualiza automaticamente o universo de ações e Units da B3.

Fontes:
- BRAPI: ticker, nome, setor e subtipo do ativo negociado;
- CVM: CNPJ, código CVM e cadastro da companhia aberta.

Uso:
    python scripts/import_stock_universe.py
    python scripts/import_stock_universe.py --max-assets 20
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
ASSETS_FILE = DATA_DIR / "assets.csv"
UNRESOLVED_FILE = DATA_DIR / "assets_unresolved.csv"
REPORT_FILE = DATA_DIR / "universe_import_report.json"
BRAPI_LIST_URL = "https://brapi.dev/api/quote/list"
BRAPI_PROFILE_URL = "https://brapi.dev/api/v2/stocks/profile"
CVM_CAD_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/CAD/DADOS/cad_cia_aberta.csv"


def get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    query = urllib.parse.urlencode({key: value for key, value in (params or {}).items() if value is not None})
    request_url = f"{url}?{query}" if query else url
    headers = {"User-Agent": "Coletor-B3-Universe/1.0", "Accept": "application/json"}
    token = os.getenv("BRAPI_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(request_url, headers=headers)
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def get_csv(url: str) -> list[dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": "Coletor-B3-Universe/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read()
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            return list(csv.DictReader(io.StringIO(raw.decode(encoding)), delimiter=";"))
        except UnicodeDecodeError:
            continue
    raise RuntimeError("Não foi possível ler o cadastro de companhias abertas da CVM.")


def digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def normalize_name(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def load_brapi_stocks(max_assets: int | None) -> list[dict[str, Any]]:
    collected: dict[str, dict[str, Any]] = {}
    page = 1
    while True:
        payload = get_json(BRAPI_LIST_URL, {"type": "stock", "limit": 100, "page": page})
        stocks = payload.get("stocks") or []
        for item in stocks:
            ticker = str(item.get("stock") or "").upper().strip()
            subtype = str(item.get("subType") or "stock").lower()
            if ticker and subtype in {"stock", "unit"}:
                collected[ticker] = item
                if max_assets and len(collected) >= max_assets:
                    return list(collected.values())
        if not stocks or not payload.get("hasNextPage"):
            break
        page += 1
    return list(collected.values())


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def load_profiles(tickers: list[str]) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    for batch in chunked(tickers, 10):
        try:
            payload = get_json(BRAPI_PROFILE_URL, {"symbols": ",".join(batch)})
        except urllib.error.HTTPError as error:
            if error.code == 401:
                raise RuntimeError("BRAPI_TOKEN não configurado ou inválido. Configure a mesma chave BRAPI usada pelo Coletor B3 antes de atualizar o universo.") from error
            if error.code != 400:
                raise
            # Alguns símbolos podem não possuir perfil cadastral. Em vez de
            # descartar o lote inteiro, tentamos um a um e mantemos somente
            # os perfis que a BRAPI efetivamente confirmar.
            for ticker in batch:
                try:
                    individual = get_json(BRAPI_PROFILE_URL, {"symbols": ticker})
                except urllib.error.HTTPError as individual_error:
                    if individual_error.code == 401:
                        raise RuntimeError("BRAPI_TOKEN não configurado ou inválido. Configure a mesma chave BRAPI usada pelo Coletor B3 antes de atualizar o universo.") from individual_error
                    continue
                for result in individual.get("results") or []:
                    symbol = str(result.get("symbol") or result.get("requestedSymbol") or "").upper()
                    data = result.get("data")
                    if symbol and isinstance(data, dict):
                        profiles[symbol] = data
            continue
        for result in payload.get("results") or []:
            ticker = str(result.get("symbol") or result.get("requestedSymbol") or "").upper()
            data = result.get("data")
            if ticker and isinstance(data, dict):
                profiles[ticker] = data
    return profiles


def accounting_profile(item: dict[str, Any], profile: dict[str, Any]) -> str:
    industry = " ".join(str(value or "") for value in (item.get("sector"), item.get("subsector"), profile.get("sector"), profile.get("industry"))).lower()
    return "bank" if any(term in industry for term in ("banco", "bank", "bancos diversificados")) else "non_financial"


def build_cvm_index(rows: list[dict[str, str]]) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    by_cnpj: dict[str, dict[str, str]] = {}
    by_name: dict[str, dict[str, str]] = {}
    for row in rows:
        if str(row.get("SIT", "")).upper() not in {"ATIVO", "FASE PRÉ-OPERACIONAL", "FASE PRE-OPERACIONAL"}:
            continue
        cnpj = digits(row.get("CNPJ_CIA"))
        if cnpj:
            by_cnpj[cnpj] = row
        for field in ("DENOM_SOCIAL", "DENOM_COMERC"):
            name = normalize_name(row.get(field))
            if name:
                by_name[name] = row
    return by_cnpj, by_name


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Monta automaticamente data/assets.csv para ações e Units.")
    parser.add_argument("--max-assets", type=int, help="Limita a importação, útil para teste.")
    parser.add_argument("--dry-run", action="store_true", help="Valida a coleta sem alterar arquivos.")
    arguments = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    listed = load_brapi_stocks(arguments.max_assets)
    if not listed:
        raise RuntimeError("A BRAPI não retornou ações ou Units listados.")

    profiles = load_profiles([str(item.get("stock")).upper() for item in listed])
    cvm_by_cnpj, cvm_by_name = build_cvm_index(get_csv(CVM_CAD_URL))
    assets: list[dict[str, str]] = []
    unresolved: list[dict[str, str]] = []

    for item in listed:
        ticker = str(item.get("stock") or "").upper().strip()
        profile = profiles.get(ticker, {})
        cnpj = digits(str(profile.get("cnpj") or ""))
        cvm_row = cvm_by_cnpj.get(cnpj)
        if not cvm_row:
            cvm_row = cvm_by_name.get(normalize_name(str(profile.get("name") or item.get("name") or "")))
        if not cvm_row:
            unresolved.append({
                "ticker": ticker,
                "brapi_name": str(profile.get("name") or item.get("name") or ""),
                "brapi_cnpj": cnpj,
                "reason": "Não foi possível vincular automaticamente ao cadastro ativo da CVM.",
            })
            continue

        assets.append({
            "ticker": ticker,
            "asset_type": "stock",
            "security_type": str(item.get("subType") or "stock").lower(),
            "accounting_profile": accounting_profile(item, profile),
            "issuer_name": str(cvm_row.get("DENOM_SOCIAL") or profile.get("name") or item.get("name") or ""),
            "cnpj": digits(cvm_row.get("CNPJ_CIA")),
            "cvm_code": str(cvm_row.get("CD_CVM") or ""),
            "sector": str(profile.get("sector") or item.get("sector") or ""),
            "industry": str(profile.get("industry") or item.get("subsector") or ""),
            "status": "active",
        })

    assets.sort(key=lambda row: row["ticker"])
    fields = ["ticker", "asset_type", "security_type", "accounting_profile", "issuer_name", "cnpj", "cvm_code", "sector", "industry", "status"]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": [BRAPI_LIST_URL, BRAPI_PROFILE_URL, CVM_CAD_URL],
        "listed_assets_found": len(listed),
        "assets_linked_to_cvm": len(assets),
        "assets_unresolved": len(unresolved),
        "note": "Ativos não vinculados à CVM foram separados para auditoria e não recebem fundamentos por inferência.",
    }
    if not arguments.dry_run:
        write_csv(ASSETS_FILE, assets, fields)
        write_csv(UNRESOLVED_FILE, unresolved, ["ticker", "brapi_name", "brapi_cnpj", "reason"])
        REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        report["dry_run"] = True
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Erro: {error}", file=sys.stderr)
        raise

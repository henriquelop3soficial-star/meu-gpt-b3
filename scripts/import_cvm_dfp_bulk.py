"""Importa DFPs da CVM para todas as ações registradas em data/assets.csv.

Cada arquivo anual da CVM é baixado apenas uma vez e processado para todo o
universo. Isso substitui a antiga rotina de baixar o mesmo ZIP para cada ticker.

Uso:
    python scripts/import_cvm_dfp_bulk.py --years 2023 2024 2025
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ASSETS_FILE = ROOT / "data" / "assets.csv"
OUTPUT_DIR = ROOT / "data" / "fundamentals"
REPORT_FILE = ROOT / "data" / "dfp_import_report.json"
CVM_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_{year}.zip"

ACCOUNT_MAPPINGS: dict[str, dict[str, str | None]] = {
    "non_financial": {
        "revenue": "3.01",
        "net_income": "3.11",
        "earnings_per_share": "3.99",
        "equity": "2.03",
        "cash_and_equivalents": "1.01.01",
        "short_term_debt": "2.01.04",
        "long_term_debt": "2.02.01",
    },
    "bank": {
        "revenue": "3.01",
        "net_income": "3.11",
        "earnings_per_share": "3.99",
        "equity": "2.07",
        "cash_and_equivalents": "1.01",
        "short_term_debt": None,
        "long_term_debt": None,
    },
}


def normalize_cvm_code(value: str | None) -> str:
    digits = re.sub(r"\D", "", value or "")
    return str(int(digits)) if digits else ""


def parse_money(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = value.strip().replace(" ", "")
    if not cleaned:
        return None
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def scale_to_brl(value: float | None, scale: str | None) -> float | None:
    if value is None:
        return None
    multiplier = {"UNIDADE": 1, "MIL": 1_000, "MILHAO": 1_000_000, "BILHAO": 1_000_000_000}.get((scale or "").upper(), 1)
    return value * multiplier


def load_assets() -> list[dict[str, str]]:
    with ASSETS_FILE.open("r", encoding="utf-8", newline="") as file:
        return [row for row in csv.DictReader(file) if row.get("status") == "active" and row.get("asset_type") == "stock" and row.get("cvm_code")]


def download_archive(year: int) -> Path:
    request = urllib.request.Request(CVM_URL.format(year=year), headers={"User-Agent": "Coletor-B3-DFP/1.0"})
    with urllib.request.urlopen(request, timeout=180) as response, NamedTemporaryFile(suffix=".zip", delete=False) as temporary:
        temporary.write(response.read())
        return Path(temporary.name)


def read_csv_from_zip(archive: zipfile.ZipFile, suffix: str) -> list[dict[str, str]]:
    filename = next((name for name in archive.namelist() if name.endswith(suffix)), None)
    if not filename:
        raise RuntimeError(f"Arquivo {suffix} não encontrado no ZIP da CVM.")
    raw = archive.read(filename)
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            return list(csv.DictReader(io.StringIO(raw.decode(encoding)), delimiter=";"))
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"Não foi possível ler {filename}.")


def latest_rows_by_company(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        code = normalize_cvm_code(row.get("CD_CVM"))
        if code and row.get("ORDEM_EXERC", "").upper() in {"ÚLTIMO", "ULTIMO"}:
            grouped[code].append(row)
    return grouped


def account_value(rows: list[dict[str, str]], account_code: str | None, monetary: bool = True) -> float | None:
    if not account_code:
        return None
    matches = [row for row in rows if row.get("CD_CONTA") == account_code]
    if not matches:
        return None
    matches.sort(key=lambda row: row.get("DT_FIM_EXERC", ""), reverse=True)
    value = parse_money(matches[0].get("VL_CONTA"))
    return scale_to_brl(value, matches[0].get("ESCALA_MOEDA")) if monetary else value


def latest_capital_by_cnpj(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    grouped: dict[str, dict[str, str]] = {}
    for row in rows:
        cnpj = re.sub(r"\D", "", row.get("CNPJ_CIA", ""))
        if not cnpj:
            continue
        previous = grouped.get(cnpj)
        if previous is None or (row.get("VERSAO", "0"), row.get("DT_REFER", "")) >= (previous.get("VERSAO", "0"), previous.get("DT_REFER", "")):
            grouped[cnpj] = row
    return grouped


def share_count(value: str | None) -> float | None:
    try:
        return float((value or "").strip())
    except ValueError:
        return None


def make_record(
    asset: dict[str, str],
    year: int,
    dre: dict[str, list[dict[str, str]]],
    bpp: dict[str, list[dict[str, str]]],
    bpa: dict[str, list[dict[str, str]]],
    capital: dict[str, dict[str, str]],
) -> dict[str, Any]:
    profile = asset.get("accounting_profile") or "non_financial"
    mapping = ACCOUNT_MAPPINGS.get(profile, ACCOUNT_MAPPINGS["non_financial"])
    cvm_code = normalize_cvm_code(asset["cvm_code"])
    capital_row = capital.get(re.sub(r"\D", "", asset.get("cnpj", "")), {})
    total = share_count(capital_row.get("QT_ACAO_TOTAL_CAP_INTEGR"))
    treasury = share_count(capital_row.get("QT_ACAO_TOTAL_TESOURO"))
    return {
        "year": year,
        "accounting_profile": profile,
        "revenue": account_value(dre.get(cvm_code, []), mapping["revenue"]),
        "net_income": account_value(dre.get(cvm_code, []), mapping["net_income"]),
        "earnings_per_share": account_value(dre.get(cvm_code, []), mapping["earnings_per_share"], monetary=False),
        "equity": account_value(bpp.get(cvm_code, []), mapping["equity"]),
        "cash_and_equivalents": account_value(bpa.get(cvm_code, []), mapping["cash_and_equivalents"]),
        "short_term_debt": account_value(bpp.get(cvm_code, []), mapping["short_term_debt"]),
        "long_term_debt": account_value(bpp.get(cvm_code, []), mapping["long_term_debt"]),
        "capital_composition": {
            "ordinary_shares": share_count(capital_row.get("QT_ACAO_ORDIN_CAP_INTEGR")),
            "preferred_shares": share_count(capital_row.get("QT_ACAO_PREF_CAP_INTEGR")),
            "total_shares": total,
            "treasury_shares": treasury,
            "shares_outstanding": total - treasury if total is not None and treasury is not None else None,
            "reference_date": capital_row.get("DT_REFER") or None,
            "source": "DFP - Composição do Capital",
        },
        "source": {"provider": "CVM Dados Abertos", "dataset": "DFP - Demonstrações Financeiras Padronizadas", "url": CVM_URL.format(year=year)},
        "notes": [
            "Valores monetários consolidados foram normalizados para BRL conforme a escala do arquivo da CVM.",
            "Campos nulos não foram estimados.",
            *(["Dívida de banco não é calculada: depósitos e captações não equivalem à dívida corporativa de empresa não financeira."] if profile == "bank" else []),
        ],
    }


def load_year(year: int) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]], dict[str, dict[str, str]]]:
    temporary = download_archive(year)
    try:
        with zipfile.ZipFile(temporary) as archive:
            return (
                latest_rows_by_company(read_csv_from_zip(archive, f"dfp_cia_aberta_DRE_con_{year}.csv")),
                latest_rows_by_company(read_csv_from_zip(archive, f"dfp_cia_aberta_BPP_con_{year}.csv")),
                latest_rows_by_company(read_csv_from_zip(archive, f"dfp_cia_aberta_BPA_con_{year}.csv")),
                latest_capital_by_cnpj(read_csv_from_zip(archive, f"dfp_cia_aberta_composicao_capital_{year}.csv")),
            )
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Importa DFPs públicos da CVM para todas as ações cadastradas automaticamente.")
    parser.add_argument("--years", nargs="+", type=int, default=[2023, 2024, 2025])
    arguments = parser.parse_args()
    if not ASSETS_FILE.exists():
        raise SystemExit("Execute primeiro scripts/import_stock_universe.py.")

    assets = load_assets()
    if not assets:
        raise SystemExit("Nenhuma ação vinculada à CVM foi encontrada em data/assets.csv.")
    source_by_year: dict[int, tuple[Any, Any, Any, Any]] = {}
    year_errors: dict[str, str] = {}
    for year in arguments.years:
        try:
            source_by_year[year] = load_year(year)
            print(f"DFP {year} carregado para o universo inteiro.")
        except Exception as error:
            year_errors[str(year)] = str(error)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    generated = 0
    without_records = 0
    for asset in assets:
        annual = [make_record(asset, year, *source_by_year[year]) for year in arguments.years if year in source_by_year]
        if not any(record.get("net_income") is not None or record.get("equity") is not None for record in annual):
            without_records += 1
            continue
        output = {
            "ticker": asset["ticker"], "asset_type": "stock", "issuer_name": asset["issuer_name"], "cnpj": asset["cnpj"], "cvm_code": asset["cvm_code"],
            "updated_at": datetime.now(timezone.utc).isoformat(), "annual_financials": annual, "errors": [],
        }
        (OUTPUT_DIR / f"{asset['ticker'].upper()}.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        generated += 1

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "years_requested": arguments.years,
        "assets_eligible": len(assets), "snapshots_generated": generated, "assets_without_matching_dfp": without_records,
        "year_errors": year_errors, "source": "CVM Dados Abertos - DFP",
    }
    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Erro: {error}", file=sys.stderr)
        raise

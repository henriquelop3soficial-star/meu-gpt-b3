"""Importa ITRs da CVM para todo o universo automático de ações.

O arquivo da CVM é baixado uma vez por ano e gera uma série de resultados
intermediários por ticker. Valores acumulados no ano permanecem identificados
como acumulados; o script não inventa um trimestre isolado.

Uso:
    python scripts/import_cvm_itr_bulk.py --years 2025 2026
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
OUTPUT_DIR = ROOT / "data" / "quarterly"
REPORT_FILE = ROOT / "data" / "itr_import_report.json"
CVM_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/ITR/DADOS/itr_cia_aberta_{year}.zip"

ACCOUNT_MAPPINGS: dict[str, dict[str, str | None]] = {
    "non_financial": {"revenue": "3.01", "net_income": "3.11", "equity": "2.03", "cash_and_equivalents": "1.01.01"},
    "bank": {"revenue": "3.01", "net_income": "3.11", "equity": "2.07", "cash_and_equivalents": "1.01"},
}


def normalize_cvm_code(value: str | None) -> str:
    digits = re.sub(r"\D", "", value or "")
    return str(int(digits)) if digits else ""


def parse_money(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = value.strip().replace(" ", "")
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
    request = urllib.request.Request(CVM_URL.format(year=year), headers={"User-Agent": "Coletor-B3-ITR/1.0"})
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


def latest_rows_by_company_and_date(rows: list[dict[str, str]]) -> dict[tuple[str, str], list[dict[str, str]]]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        code = normalize_cvm_code(row.get("CD_CVM"))
        reference = row.get("DT_REFER", "")
        if code and reference and row.get("ORDEM_EXERC", "").upper() in {"ÚLTIMO", "ULTIMO"}:
            grouped[(code, reference)].append(row)
    return grouped


def account_value(rows: list[dict[str, str]], account_code: str | None) -> float | None:
    if not account_code:
        return None
    matches = [row for row in rows if row.get("CD_CONTA") == account_code]
    if not matches:
        return None
    # Um mesmo ITR pode trazer, para a mesma conta, o trimestre isolado
    # (por exemplo, 01/04 a 30/06) e o acumulado no ano (01/01 a 30/06).
    # Este importador declara ``year_to_date``; por isso seleciona a linha
    # cujo início de exercício é o mais antigo, e não o trimestre isolado.
    matches.sort(key=lambda row: (row.get("DT_INI_EXERC", "9999-12-31"), row.get("DT_FIM_EXERC", "")))
    value = parse_money(matches[0].get("VL_CONTA"))
    return scale_to_brl(value, matches[0].get("ESCALA_MOEDA"))


def records_for_asset(asset: dict[str, str], year: int, dre: dict[tuple[str, str], list[dict[str, str]]], bpp: dict[tuple[str, str], list[dict[str, str]]], bpa: dict[tuple[str, str], list[dict[str, str]]]) -> list[dict[str, Any]]:
    profile = asset.get("accounting_profile") or "non_financial"
    mapping = ACCOUNT_MAPPINGS.get(profile, ACCOUNT_MAPPINGS["non_financial"])
    code = normalize_cvm_code(asset["cvm_code"])
    references = sorted({reference for current_code, reference in set(dre) | set(bpp) | set(bpa) if current_code == code})
    records: list[dict[str, Any]] = []
    for reference in references:
        dre_rows = dre.get((code, reference), [])
        bpp_rows = bpp.get((code, reference), [])
        bpa_rows = bpa.get((code, reference), [])
        if not dre_rows and not bpp_rows and not bpa_rows:
            continue
        start_dates = [row.get("DT_INI_EXERC", "") for row in dre_rows if row.get("DT_INI_EXERC")]
        records.append({
            "reference_date": reference,
            "period_start": min(start_dates) if start_dates else None,
            "period_end": reference,
            "period_type": "year_to_date",
            "accounting_profile": profile,
            "revenue_year_to_date": account_value(dre_rows, mapping["revenue"]),
            "net_income_year_to_date": account_value(dre_rows, mapping["net_income"]),
            "equity": account_value(bpp_rows, mapping["equity"]),
            "cash_and_equivalents": account_value(bpa_rows, mapping["cash_and_equivalents"]),
            "source": {"provider": "CVM Dados Abertos", "dataset": "ITR - Informações Trimestrais", "url": CVM_URL.format(year=year)},
            "notes": ["Valores do ITR consolidado foram normalizados para BRL.", "Resultado identificado como acumulado no exercício; não foi convertido em trimestre isolado."],
        })
    return records


def load_year(year: int) -> tuple[dict[tuple[str, str], list[dict[str, str]]], dict[tuple[str, str], list[dict[str, str]]], dict[tuple[str, str], list[dict[str, str]]]]:
    temporary = download_archive(year)
    try:
        with zipfile.ZipFile(temporary) as archive:
            return (
                latest_rows_by_company_and_date(read_csv_from_zip(archive, f"itr_cia_aberta_DRE_con_{year}.csv")),
                latest_rows_by_company_and_date(read_csv_from_zip(archive, f"itr_cia_aberta_BPP_con_{year}.csv")),
                latest_rows_by_company_and_date(read_csv_from_zip(archive, f"itr_cia_aberta_BPA_con_{year}.csv")),
            )
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Importa ITRs públicos da CVM para todas as ações.")
    parser.add_argument("--years", nargs="+", type=int, default=[datetime.now().year - 1, datetime.now().year])
    arguments = parser.parse_args()
    if not ASSETS_FILE.exists():
        raise SystemExit("Execute primeiro scripts/import_stock_universe.py.")

    assets = load_assets()
    source_by_year: dict[int, tuple[Any, Any, Any]] = {}
    year_errors: dict[str, str] = {}
    for year in arguments.years:
        try:
            source_by_year[year] = load_year(year)
            print(f"ITR {year} carregado para o universo inteiro.")
        except Exception as error:
            year_errors[str(year)] = str(error)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    generated = 0
    for asset in assets:
        records: list[dict[str, Any]] = []
        for year in arguments.years:
            if year in source_by_year:
                records.extend(records_for_asset(asset, year, *source_by_year[year]))
        records.sort(key=lambda record: record["reference_date"])
        if not records:
            continue
        output = {
            "ticker": asset["ticker"], "asset_type": "stock", "issuer_name": asset["issuer_name"], "cnpj": asset["cnpj"], "cvm_code": asset["cvm_code"],
            "updated_at": datetime.now(timezone.utc).isoformat(), "interim_financials": records, "errors": [],
        }
        (OUTPUT_DIR / f"{asset['ticker'].upper()}.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        generated += 1

    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "years_requested": arguments.years, "assets_eligible": len(assets), "snapshots_generated": generated, "year_errors": year_errors, "source": "CVM Dados Abertos - ITR"}
    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Erro: {error}", file=sys.stderr)
        raise

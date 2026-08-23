"""Gera dados fundamentalistas anuais a partir dos DFPs públicos da CVM.

Uso:
    python scripts/import_cvm_dfp.py --years 2023 2024 2025
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
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ASSETS_FILE = ROOT / "data" / "assets.csv"
OUTPUT_DIR = ROOT / "data" / "fundamentals"
CVM_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_{year}.zip"

# O plano de contas da CVM muda por setor. Bancos, por exemplo, não possuem
# a mesma estrutura de "passivo circulante/não circulante" de uma indústria.
# Só calculamos dívida quando a classificação permite uma comparação segura.
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


def normalize_cvm_code(value: str) -> str:
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


def load_assets() -> list[dict[str, str]]:
    with ASSETS_FILE.open("r", encoding="utf-8", newline="") as file:
        return [row for row in csv.DictReader(file) if row.get("status") == "active" and row.get("asset_type") == "stock"]


def download_archive(year: int) -> Path:
    url = CVM_URL.format(year=year)
    request = urllib.request.Request(url, headers={"User-Agent": "Coletor-B3-CVM/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response, NamedTemporaryFile(suffix=".zip", delete=False) as temporary:
        temporary.write(response.read())
        return Path(temporary.name)


def read_csv_from_zip(archive: zipfile.ZipFile, suffix: str) -> list[dict[str, str]]:
    filename = next((name for name in archive.namelist() if name.endswith(suffix)), None)
    if filename is None:
        raise RuntimeError(f"Arquivo {suffix} não encontrado no ZIP da CVM.")
    raw = archive.read(filename)
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            text = raw.decode(encoding)
            return list(csv.DictReader(io.StringIO(text), delimiter=";"))
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"Não foi possível ler {filename}.")


def rows_for_company(rows: list[dict[str, str]], cvm_code: str) -> list[dict[str, str]]:
    target = normalize_cvm_code(cvm_code)
    selected = [row for row in rows if normalize_cvm_code(row.get("CD_CVM", "")) == target]
    # O DFP traz mais de uma versão do exercício. A versão "ÚLTIMO" é a divulgada mais recentemente.
    latest = [row for row in selected if row.get("ORDEM_EXERC", "").upper() in {"ÚLTIMO", "ULTIMO"}]
    return latest or selected


def scale_to_brl(value: float | None, scale: str | None) -> float | None:
    if value is None:
        return None
    multipliers = {"UNIDADE": 1, "MIL": 1_000, "MILHAO": 1_000_000, "BILHAO": 1_000_000_000}
    return value * multipliers.get((scale or "").upper(), 1)


def account_value(rows: list[dict[str, str]], code: str, *, normalize_money: bool = True) -> float | None:
    matching = [row for row in rows if row.get("CD_CONTA") == code]
    if not matching:
        return None
    matching.sort(key=lambda row: row.get("DT_FIM_EXERC", ""), reverse=True)
    value = parse_money(matching[0].get("VL_CONTA"))
    return scale_to_brl(value, matching[0].get("ESCALA_MOEDA")) if normalize_money else value


def collect_year(asset: dict[str, str], year: int) -> dict[str, Any]:
    temporary_path = download_archive(year)
    try:
        with zipfile.ZipFile(temporary_path) as archive:
            dre_rows = rows_for_company(read_csv_from_zip(archive, f"dfp_cia_aberta_DRE_con_{year}.csv"), asset["cvm_code"])
            bpp_rows = rows_for_company(read_csv_from_zip(archive, f"dfp_cia_aberta_BPP_con_{year}.csv"), asset["cvm_code"])
            bpa_rows = rows_for_company(read_csv_from_zip(archive, f"dfp_cia_aberta_BPA_con_{year}.csv"), asset["cvm_code"])
    finally:
        temporary_path.unlink(missing_ok=True)

    profile = asset.get("accounting_profile", "non_financial")
    mapping = ACCOUNT_MAPPINGS.get(profile, ACCOUNT_MAPPINGS["non_financial"])

    def mapped_value(source_rows: list[dict[str, str]], field: str) -> float | None:
        account_code = mapping.get(field)
        # Lucro por ação já é expresso em R$/ação; os demais valores monetários
        # são convertidos da escala do arquivo CVM para reais (BRL).
        return account_value(source_rows, account_code, normalize_money=field != "earnings_per_share") if account_code else None

    notes = [
        "Valores monetários são divulgados no DFP consolidado e normalizados para reais (BRL).",
        "Campos nulos não foram estimados; exigem validação do plano de contas da companhia.",
    ]
    if profile == "bank":
        notes.append("Dívida de banco não é calculada: depósitos e captações não são equivalentes à dívida corporativa de uma empresa não financeira.")

    return {
        "year": year,
        "accounting_profile": profile,
        "revenue": mapped_value(dre_rows, "revenue"),
        "net_income": mapped_value(dre_rows, "net_income"),
        "earnings_per_share": mapped_value(dre_rows, "earnings_per_share"),
        "equity": mapped_value(bpp_rows, "equity"),
        "cash_and_equivalents": mapped_value(bpa_rows, "cash_and_equivalents"),
        "short_term_debt": mapped_value(bpp_rows, "short_term_debt"),
        "long_term_debt": mapped_value(bpp_rows, "long_term_debt"),
        "source": {
            "provider": "CVM Dados Abertos",
            "dataset": "DFP - Demonstrações Financeiras Padronizadas",
            "url": CVM_URL.format(year=year),
        },
        "notes": notes,
    }


def write_asset_snapshot(asset: dict[str, str], years: list[int]) -> None:
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for year in years:
        try:
            records.append(collect_year(asset, year))
        except Exception as error:  # Mantém os demais anos disponíveis e registra a falha.
            errors.append({"year": str(year), "message": str(error)})

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output = {
        "ticker": asset["ticker"],
        "asset_type": asset["asset_type"],
        "issuer_name": asset["issuer_name"],
        "cnpj": asset["cnpj"],
        "cvm_code": asset["cvm_code"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "annual_financials": records,
        "errors": errors,
    }
    destination = OUTPUT_DIR / f"{asset['ticker'].upper()}.json"
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Atualizado: {destination.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Importa DFPs públicos da CVM.")
    parser.add_argument("--years", nargs="+", type=int, default=[2023, 2024, 2025])
    arguments = parser.parse_args()
    if not ASSETS_FILE.exists():
        raise SystemExit("Tabela data/assets.csv não encontrada.")
    for asset in load_assets():
        write_asset_snapshot(asset, arguments.years)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Erro: {error}", file=sys.stderr)
        raise

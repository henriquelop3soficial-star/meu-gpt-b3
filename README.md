# Coletor B3 — BRAPI + CVM

Projeto limpo para publicar na Render e conectar a um GPT personalizado.

## Variáveis da Render

- `API_ACCESS_KEY`
- `BRAPI_TOKEN`
- `DEMO_MODE=false`
- `PUBLIC_BASE_URL=https://meu-gpt-b3.onrender.com`

## Rotas

- `/health`
- `/v1/assets/{ticker}/quote`
- `/v1/assets/{ticker}/history?years=3`
- `/v1/assets/{ticker}/fundamentals`
- `/v1/assets/{ticker}/dossier?years=3`

O histórico usa `startDate` e `endDate` para solicitar uma janela exata de três anos à BRAPI.

`/fundamentals` combina cotação da BRAPI com as demonstrações anuais públicas da CVM. Nesta etapa, P/L, P/VP e ROE são calculados somente quando há dados suficientes; payout e dividend yield ficam nulos até a integração da fonte gratuita de proventos.

Os campos de receita e lucro são anuais, não TTM. O próprio retorno informa o período contábil e a fonte de cada grupo de dados.

## Cadastro de ativos

O arquivo `data/assets.csv` faz o vínculo entre ticker, CNPJ e código CVM para a coleta dos dados abertos da CVM. Começamos com BBAS3 e a rotina futura poderá preencher novos ativos automaticamente.

## Coleta gratuita da CVM

`scripts/import_cvm_dfp.py` baixa os DFPs públicos da CVM e gera `data/fundamentals/<TICKER>.json` com receita, lucro líquido, lucro por ação, patrimônio, caixa e dívida por exercício.

Cada ativo informa o seu `accounting_profile`. Para bancos, depósitos e captações não são classificados como dívida corporativa, evitando indicadores distorcidos.

```powershell
python scripts/import_cvm_dfp.py --years 2023 2024 2025
```

O workflow `.github/workflows/update-cvm-data.yml` executa a atualização semanalmente e salva os JSONs no repositório.

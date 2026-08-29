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

O histórico usa `startDate` e `endDate` para solicitar a janela exata. Se o plano da BRAPI não liberar o período solicitado, a API tenta retornar um ano e sinaliza explicitamente que não se deve calcular a valorização de três anos.

`/fundamentals` combina cotação da BRAPI com as demonstrações anuais públicas da CVM. Nesta etapa, P/L, P/VP e ROE são calculados somente quando há dados suficientes; payout e dividend yield ficam nulos até a integração da fonte gratuita de proventos.

Os campos de receita e lucro são anuais, não TTM. O próprio retorno informa o período contábil e a fonte de cada grupo de dados.

## Universo automático de ações

O Coletor não exige cadastro manual de tickers. A rotina `scripts/import_stock_universe.py` consulta a lista de ações e Units da B3 na BRAPI, obtém o CNPJ no perfil do emissor e faz o vínculo com o cadastro ativo da CVM. Ela grava o resultado em `data/assets.csv` e deixa vínculos não confirmados em `data/assets_unresolved.csv`, sem fazer inferências.

## Fundamentos anuais em massa

`scripts/import_cvm_dfp_bulk.py` baixa cada DFP anual da CVM uma única vez e gera `data/fundamentals/<TICKER>.json` para todo o universo vinculado. Os campos iniciais são receita ou linha operacional equivalente, lucro líquido, lucro por ação, patrimônio, caixa e dívida quando aplicável.

Para bancos, depósitos e captações não são classificados como dívida corporativa, evitando indicadores distorcidos.

```powershell
python scripts/import_stock_universe.py
python scripts/import_cvm_dfp_bulk.py --years 2023 2024 2025
```

O workflow `.github/workflows/update-cvm-data.yml` executa os dois processos semanalmente, salva os dados no repositório e aciona uma nova implantação automática na Render. Para ele funcionar, cadastre `BRAPI_TOKEN` em **GitHub > Settings > Secrets and variables > Actions**.

Histórico de preços, proventos, ITR e documentos oficiais serão integrados em rotinas próprias, sem depender de envio manual de arquivos.

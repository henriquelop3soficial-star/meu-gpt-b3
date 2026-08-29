# Coletor B3 — BolsAI + CVM

Projeto limpo para publicar na Render e conectar a um GPT personalizado.

## Variáveis da Render

- `API_ACCESS_KEY`
- `BOLSAI_API_KEY`
- `BOLSAI_BASE_URL` (padrão: `https://api.usebolsai.com/api/v1`)
- `DEMO_MODE=false`
- `PUBLIC_BASE_URL=https://meu-gpt-b3.onrender.com`

## Rotas

- `/health`
- `/v1/assets/{ticker}/quote`
- `/v1/assets/{ticker}/history?years=3`
- `/v1/assets/{ticker}/fundamentals`
- `/v1/assets/{ticker}/dossier?years=3`

O histórico usa a janela exata de datas da BolsAI. Se o plano não liberar o período solicitado, a resposta declara a indisponibilidade e não calcula valorização por estimativa.

`/fundamentals` usa os múltiplos da BolsAI para o ticker/classe consultado e combina as demonstrações anuais e intermediárias públicas da CVM. O endpoint `/dividends` retorna proventos e JCP da BolsAI; documentos oficiais continuam sendo a referência para validação.

Os campos de receita e lucro são anuais, não TTM. O próprio retorno informa o período contábil e a fonte de cada grupo de dados.

## Universo automático de ações

O universo de ações já vinculado à CVM é mantido em `data/assets.csv`; por isso o fluxo semanal não precisa consultar perfis na BRAPI. A rotina legada `scripts/import_stock_universe.py` fica reservada para manutenção manual do universo e grava vínculos não confirmados em `data/assets_unresolved.csv`, sem fazer inferências.

## Fundamentos anuais em massa

`scripts/import_cvm_dfp_bulk.py` baixa cada DFP anual da CVM uma única vez e gera `data/fundamentals/<TICKER>.json` para todo o universo vinculado. Os campos iniciais são receita ou linha operacional equivalente, lucro líquido, lucro por ação, patrimônio, caixa e dívida quando aplicável.

A composição de capital da DFP também é importada: ações ordinárias, preferenciais, ações em tesouraria e ações em circulação. Para Units, os dados são exibidos sem presumir uma conversão por Unit.

`scripts/import_cvm_itr_bulk.py` baixa os ITRs recentes e gera `data/quarterly/<TICKER>.json`. Resultados intermediários são mantidos como acumulados no exercício quando essa é a forma divulgada pela CVM.

Para bancos, depósitos e captações não são classificados como dívida corporativa, evitando indicadores distorcidos.

```powershell
python scripts/import_stock_universe.py
python scripts/import_cvm_dfp_bulk.py --years 2023 2024 2025
python scripts/import_cvm_itr_bulk.py --years 2025 2026
```

O workflow `.github/workflows/update-cvm-data.yml` atualiza DFP e ITR públicos da CVM semanalmente, sem consumir a cota da BRAPI. O universo de ativos já validado permanece no repositório.

Histórico de preços, proventos, ITR e documentos oficiais serão integrados em rotinas próprias, sem depender de envio manual de arquivos.

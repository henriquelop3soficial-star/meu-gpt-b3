# GPT Coletor B3 API

API privada para o GPT Coletor consultar dados de ações, FIIs e ETFs da B3. Ela foi criada para manter o token da BRAPI fora das instruções do GPT.

## O que já está pronto

- autenticação por `X-API-Key`;
- consulta de cotação, histórico e dossiê;
- contrato OpenAPI automático em `/openapi.json`;
- modo demonstração para testes sem credenciais;
- configuração dos endpoints RapidAPI por variáveis de ambiente.

## Configuração local

1. Crie um ambiente virtual Python e instale as dependências:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Copie `.env.example` para `.env` e preencha os valores.

3. Para testar sem a BRAPI, use:

   ```text
   API_ACCESS_KEY=uma-chave-local
   DEMO_MODE=true
   ```

4. Inicie a API:

   ```powershell
   uvicorn app.main:app --reload --port 8000
   ```

5. Abra `http://127.0.0.1:8000/docs` para testar os endpoints.

## Ligação com a BRAPI

Defina somente o token da BRAPI no `.env`:

- `BRAPI_TOKEN`.

Não envie o token da BRAPI em mensagens ou arquivos compartilhados. A API usa os endpoints oficiais de cotação e histórico da BRAPI.

## Antes de conectar ao GPT

Um GPT personalizado só alcança uma API que esteja publicada em URL HTTPS. Após validar localmente, publique esta API em um serviço de hospedagem. Então, importe o arquivo `/openapi.json` no campo de ações do GPT e configure `X-API-Key` como autenticação.

## Atualização da Render

O projeto inclui `main.py` para compatibilidade com o comando `uvicorn main:app` e `render.yaml` com o caminho de health check correto.

1. Substitua os arquivos publicados na Render pelos arquivos deste diretório.
2. Defina as variáveis em **Environment**: `API_ACCESS_KEY`, `PUBLIC_BASE_URL`, `BRAPI_TOKEN` e `DEMO_MODE=false`.
3. Faça um novo deploy e confirme `GET /health` e `GET /openapi.json`.
4. No GPT, reimporte `https://meu-gpt-b3.onrender.com/openapi.json`.

O schema publicado passará a declarar a URL da Render e a autenticação `X-API-Key`, além de listar cotação, histórico e dossiê.

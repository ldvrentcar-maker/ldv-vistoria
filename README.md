# LDV Vistoria Inteligente

**MVP funcional de vistoria veicular**, com links individuais por veículo, captura de 16 fotografias diretamente da câmera e conferência obrigatória de pneus, vidros, kit de reparo, chave e bancos, histórico por placa, painel da equipe, registro de avarias, revisão, solicitações de recaptura e IA visual opcional.

## Arquivos

- `static/demo.html`: **demonstração independente**. Pode ser aberta localmente em navegador e contém dados fictícios. O armazenamento de demonstração é local, sem sincronização com outros aparelhos; **não usa IA real**.
- `static/index.html`, `static/style.css`, `static/app.js`: aplicativo operacional web para motorista e equipe.
- `main.py`: backend FastAPI com autenticação administrativa, links temporários do motorista, SQLite, armazenamento de fotos, trilha de auditoria e integração opcional de IA.
- `tests/test_flow.py`: teste automatizado do fluxo de vistoria.
- `Dockerfile`, `docker-compose.yml`: execução em servidor com armazenamento persistente.

## Colocar para funcionar localmente

### Opção A — Docker Compose

1. Copie `.env.example` para `.env`.
2. No arquivo `.env`, substitua `ADMIN_PASSWORD` por uma senha realmente forte e `SESSION_SECRET` por um segredo aleatório longo. **Nunca publique esse arquivo**.
3. Opcional: configure `OPENAI_API_KEY` para a análise automática de fotos.
4. Execute `docker compose up --build -d`.
5. Abra `http://localhost:8000` no computador, faça login e crie uma vistoria.

### Opção B — Python

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export ADMIN_PASSWORD='troque-esta-senha'
export SESSION_SECRET='substitua-por-uma-chave-longa-aleatoria'
uvicorn main:app --host 127.0.0.1 --port 8000
```

Acesse `http://localhost:8000`. No Windows PowerShell, a ativação do ambiente é `.venv\Scripts\Activate.ps1` e a configuração de variável é `$env:ADMIN_PASSWORD="..."` etc.

**Após publicar com HTTPS, também é possível adicionar o aplicativo à tela inicial do Android/iPhone pelo navegador.**

**Para a câmera do motorista funcionar pelo celular, publique com HTTPS em um domínio.** A API `getUserMedia` depende de contexto seguro (HTTPS ou localhost). Abrir o HTML no visualizador de anexos ou numa página HTTP pelo IP da rede local pode impedir a câmera.

## Fluxo

1. **Equipe** faz login e cria a vistoria com placa, modelo, tipo e motorista (opcional).
2. **Equipe** copia o link individual do motorista e envia pelo WhatsApp.
3. **Motorista** preenche nome, KM numérico obrigatório (com foto do hodômetro), preenche 13 itens de checklist de estado/avaria e capta 16 fotos em tempo real usando `getUserMedia` e `canvas`, sem botão de escolher imagens da galeria. As fotos são recebidas e carimbadas pelo servidor; substituições ficam preservadas no histórico técnico.
4. **IA** (se configurada) recebe cada foto por integração *no servidor*, identifica possíveis riscos, trincas, amassados etc. e compara o ângulo com a **última vistoria aprovada do mesmo veículo**, se disponível. Os resultados são sugestões, com incerteza.
5. **Equipe** visualiza fotos, confere sugestões, registra avarias confirmadas/descartadas e decide aprovar, reprovar ou solicitar novas fotos.
6. **Motorista** acompanha o status no próprio link e pode refazer fotos solicitadas.
7. **Equipe** também visualiza os 13 itens de pneus, vidros, acessórios e bancos, pode imprimir a tela de vistoria e exportar a lista CSV com ocorrências. O motorista não consegue enviar sem KM, checklist completo e as 16 fotografias.

## Banco de dados e fotos

O servidor usa SQLite em `DATA_DIR/ldv_vistorias.db` e JPEG em `DATA_DIR/photos`. No Docker Compose, o volume `ldv-dados` persiste esses arquivos. **Faça backup e defina retenção**. Em produção com muitas vistorias simultâneas, prefira banco gerenciado e armazenamento de objetos.

## Inteligência artificial

Com `OPENAI_API_KEY` configurada, o servidor usa a API Responses da OpenAI; o modelo é controlado por `OPENAI_MODEL` (`gpt-4.1-mini` como padrão configurável). O custo depende do uso da API. Chaves ficam no servidor, nunca no código do navegador. O processamento é assíncrono em tarefas de fundo e pode aparecer como pendente/erro; a equipe pode revisar manualmente mesmo sem IA.

A IA é uma **triagem de possíveis avarias visíveis**, não um laudo pericial, mecânico ou prova de que algo ocorreu durante o aluguel. Reflexos, sujeira, ângulo e qualidade de imagem podem causar falsos positivos/negativos. Use fotos equivalentes e revisão humana.

## Segurança, LGPD e limitações de produção

- O site **não apresenta um botão de galeria**, e usa somente o fluxo de captura em tempo real. Isso **não garante origem autêntica da imagem**: um cliente modificado, câmera virtual, foto de outra tela ou uso de outro software pode contornar a restrição. Para controle antifraude mais forte, recomenda-se aplicativo nativo com atestação de integridade do dispositivo, detecção de manipulação, desafios de vídeo e validação do lado do servidor.
- Links individuais são tokens difíceis de adivinhar, expiram em 14 dias e permitem acesso às informações daquela vistoria; **não compartilhe publicamente**.
- Cookies administrativos são `HttpOnly` e `SameSite=Strict`. No **servidor HTTPS**, configure `SECURE_COOKIES=true` e proxy reverso TLS. **Nunca publique com credenciais de exemplo**.
- Para internet pública, implemente também proteção contra força bruta, limitação de requisições, logs centralizados, backups, controle de acesso por funcionário, política LGPD/privacidade, antivírus/varredura de arquivos, gestão de dados e limites de upload no proxy.
- A versão entregue é um **MVP**, não um produto pronto para uso comercial em larga escala. Ainda não inclui integração ativa ao LDV One, WhatsApp automático, assinatura digital, cobrança de danos, contas individuais por funcionário nem publicação pública.
- A demonstração HTML não compartilha dados com o servidor e **não detecta avarias reais**.

## Publicar

Use VPS/servidor Docker atrás de proxy HTTPS (ex.: Caddy, Nginx ou plataforma que suporte armazenamento persistente). Configure um domínio como `vistoria.seudominio.com.br`, DNS, certificado TLS e `SECURE_COOKIES=true`. Não use hospedagem estática Firebase Hosting isolada para o backend Python; o frontend estático pode ser hospedado separadamente **somente após** apontar a API para um backend protegido.

### Teste

```bash
python tests/test_flow.py
```

## Próxima fase sugerida

Migrar para PostgreSQL e armazenamento em nuvem, adicionar login por funcionário, integração com LDV One (veículos/motoristas/OS), notificações WhatsApp, revisão por regiões do carro com marcações na imagem, dashboard de danos por veículo e validação antifraude mais forte por app nativo.

## Conferência obrigatória (versão atualizada)

- **KM atual obrigatório**: apenas números inteiros de 0 a 9999999; é validado pelo navegador e pelo servidor antes do envio final. A fotografia do painel/hodômetro continua obrigatória, mas a leitura automática do número impresso na foto não está incluída; a equipe deve conferir a consistência manualmente.
- **13 itens de verificação**: quatro pneus; para-brisa, vidro traseiro e laterais; kit de reparo, chave do veículo e chave de roda; bancos do motorista, passageiro e traseiro. Para cada item o motorista seleciona **OK, Com avaria, Ausente ou Não se aplica**. Marcar avaria/ausente exige observação.
- **16 fotografias obrigatórias**: 10 ângulos anteriores mais pneus dianteiros, pneus traseiros, vidros, kit reparo, chave e bancos.
- A equipe acompanha os resultados do checklist e as fotos no mesmo painel, com histórico por vistoria. A verificação da existência/condição dos itens é declarada pelo motorista e revisada pela equipe, não validada automaticamente por IA nesta versão.
- Em um veículo sem chave de roda ou com arranjo diferente, o motorista pode selecionar **Não se aplica**; a equipe deve validar a aplicabilidade ao modelo.

## Conferência obrigatória (versão atualizada)

- **KM atual obrigatório**: apenas números inteiros de 0 a 9999999; é validado pelo navegador e pelo servidor antes do envio final. A fotografia do painel/hodômetro continua obrigatória, mas a leitura automática do número impresso na foto não está incluída; a equipe deve conferir a consistência manualmente.
- **13 itens de verificação**: quatro pneus; para-brisa, vidro traseiro e laterais; kit de reparo, chave do veículo e chave de roda; bancos do motorista, passageiro e traseiro. Para cada item o motorista seleciona **OK, Com avaria, Ausente ou Não se aplica**. Marcar avaria/ausente exige observação.
- **16 fotografias obrigatórias**: 10 ângulos anteriores mais pneus dianteiros, pneus traseiros, vidros, kit reparo, chave e bancos.
- A equipe acompanha os resultados do checklist e as fotos no mesmo painel, com histórico por vistoria. A verificação da existência/condição dos itens é declarada pelo motorista e revisada pela equipe, não validada automaticamente por IA nesta versão.
- Em um veículo sem chave de roda ou com arranjo diferente, o motorista pode selecionar **Não se aplica**; a equipe deve validar a aplicabilidade ao modelo.

## Marca LDV

O logotipo oficial `static/ldv-logo.png` enviado pela LDV Rent Car foi incorporado ao painel administrativo, à tela de login e à área do motorista. Os ícones do aplicativo instalado e o favicon também usam a identidade da LDV. As duas demonstrações HTML incluem o logo embutido no arquivo para exibição mesmo offline.

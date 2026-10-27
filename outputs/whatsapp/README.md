# Conexão do WhatsApp e acompanhamento do atendimento

## Estado atual da automação

O navegador em `web.whatsapp.com` é uma sessão independente. Ele não encaminha respostas para o código. O atendimento automático usa a WhatsApp Business Cloud API, um webhook HTTPS público e o worker `facilitae.py` em execução contínua.

O código já mantém histórico individual, classifica mensagens usando o roteiro, prepara demonstrações, registra propostas e comprovantes, encaminha recibos ao número pessoal configurado, exige aprovação autenticada do pagamento e entrega o link somente depois de verificar a publicação na Cloudflare Pages.

Para ativar a operação real, complete no painel da Meta o caso de uso WhatsApp do app Facilitae, adicione/verifique o número comercial e obtenha `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`, `META_APP_SECRET` e um `WHATSAPP_VERIFY_TOKEN` criado por você. Publique o endpoint `/webhook` em HTTPS e defina `FACILITAE_PUBLIC_BASE_URL` com a URL completa. Configure também `OPENAI_API_KEY`/`OPENAI_MODEL` se quiser classificação por IA e habilite os três sinalizadores somente depois de validar o ambiente.

Depois, na pasta do projeto, o worker contínuo pode ser iniciado por:

```powershell
.\start_facilitae.ps1
```

Confira a prontidão antes de aceitar clientes:

```powershell
python .\outputs\whatsapp\facilitae.py status
```

## Serviço contínuo simples (Railway)

O projeto inclui `railway.json` na raiz. No Railway, escolha **Deploy from GitHub repo**, selecione este repositório e configure as variáveis no painel (nunca no GitHub). Para manter o SQLite e o arquivo de autenticação entre reinícios, anexe um volume no caminho `/data` e defina `FACILITAE_DB_PATH=/data/inbox.sqlite3` e `FACILITAE_STATE_DIR=/data`. Gere um domínio público HTTPS no serviço e use `https://SEU-DOMINIO/webhook` na configuração de webhooks da Meta.

O Railway mantém serviços persistentes, mas o uso e eventuais volumes podem depender do plano e dos limites da conta. A implantação não foi executada neste ambiente: ainda exige sua conta Railway, conexão do GitHub, variáveis secretas e domínio público.

Enquanto `automatic_replies_ready` for `false`, nenhuma resposta automática deve ser considerada operacional. O arquivo `.env` fica em `work/whatsapp/` e não deve ser enviado ao GitHub. O servidor local não substitui um endpoint HTTPS público; a execução contínua exige um serviço/servidor que permaneça ligado.

Preparado em 25/10/2026, fuso America/Rio_Branco.

## Situação verificada

- Navegador interativo disponível. Em 25/10/2026, após a vinculação feita pelo usuário, a sessão do WhatsApp Business foi confirmada em https://web.whatsapp.com/. Nenhuma conversa foi aberta para essa confirmação e nenhuma mensagem foi enviada.
- A conexão da Cloud API ainda está pendente. O login oficial Meta for Business foi aberto para o usuário acessar a conta que administrará a integração. A sessão Web não fornece as credenciais da API.
- Não foi encontrado um conector direto de WhatsApp nos resultados da busca de plugins. O catálogo não é exaustivo; há outras opções no [diretório](https://chatgpt.com/plugins).
- A alternativa oficial identificada é a WhatsApp Business Platform — Cloud API, hospedada pela Meta.
- O vídeo mencionado não foi disponibilizado neste contexto. Esta preparação usa as instruções oficiais verificadas; não presume qual ferramenta aparece no vídeo.
- O roteiro e a referência existentes foram preservados. Nenhum contato foi iniciado e nenhuma mensagem foi enviada.

## Conectar o navegador por QR Code

1. Abra a aba WhatsApp Web que ficou disponível no navegador do projeto.
2. No celular do número comercial, abra WhatsApp Business.
3. Em Android, abra o menu de três pontos; em iPhone, Configurações. Entre em **Dispositivos conectados > Conectar um dispositivo**.
4. Faça a confirmação de identidade no próprio celular e escaneie o QR Code da página oficial. Caso a tela apresente instruções atualizadas de câmera/link, siga o fluxo mostrado pelo WhatsApp.
5. A entrada só estará confirmada quando o WhatsApp mostrar a sessão conectada. Um QR expirado precisa ser atualizado na própria página.

Essa vinculação permite usar o WhatsApp Web. Não cria uma integração com a Cloud API e não ativa atendimento automático contínuo. A sessão pode dar acesso a conversas comerciais; use a conta comercial pretendida. Não envie códigos, senhas ou tokens neste chat.

## Conectar a automação oficial

Para manter o número que já está no WhatsApp Business do celular, o caminho a verificar é **Coexistência**, pelo Cadastro Incorporado de um Parceiro de Soluções/Provedor de Tecnologia habilitado. Esse fluxo conecta a conta existente à Cloud API e é diferente do QR do WhatsApp Web. A elegibilidade e os recursos disponíveis precisam ser conferidos na conta. A documentação atual apresenta verificação no app, com código e opção de QR.

Como ainda não há provedor, app Meta, conta de API ou credenciais configurados neste projeto, não foi criado um QR de API nem foi alterado o registro do número. Não remover a conta do celular para tentar contornar o cadastro. O fluxo de Coexistência pode desvincular dispositivos associados, que deverão ser conectados novamente, e alterar alguns recursos do app; revisar as condições exibidas antes de concluir.

Para a configuração direta da Cloud API, são necessários portfólio empresarial Meta, conta WhatsApp Business, número comercial e app Meta. A conexão do número já utilizado no celular deve respeitar o fluxo compatível com esse cenário. Uma conta/número de teste oficial pode ser usado para validar a integração, sem disparar a mensagem de teste do painel enquanto o envio permanecer proibido.

Etapas restantes, depois da conexão da conta:

1. Identificar o `phone_number_id` e a conta do WhatsApp; configurar segredo do app e token de verificação em ambiente local seguro. Um token de acesso será necessário para as chamadas autorizadas à API na etapa posterior.
2. Hospedar o receptor em um serviço permanente com HTTPS e configurar a URL `/webhook` na Meta. O servidor incluído é de desenvolvimento e escuta apenas `127.0.0.1`; ainda não foi publicado nem conectado à Meta.
3. Assinar os eventos de mensagens. Em Coexistência, configurar também os eventos de eco, histórico e sincronização conforme o fluxo oficial. O histórico importado não deve abrir uma janela nova de atendimento.
4. Confirmar recebimento usando eventos de teste e revisão local. Não clicar em enviar mensagem real.
5. Conectar um provedor de IA e o canal humano, com credenciais próprias. O sistema deve usar o roteiro existente, o histórico do mesmo cliente e os limites comerciais. Esta versão só monta esse contexto localmente; não chama um modelo.
6. Validar respostas em modo rascunho, inclusive recusas, negociação, duplicatas e transição para humano. Habilitar envio real apenas após nova instrução explícita do responsável.

Custos de Meta, provedor, IA e servidor precisam ser verificados na configuração. Os R$ 50 do roteiro são o preço de manutenção vendido ao cliente, não uma estimativa de custo desta automação.

## O que foi implementado

`inbox.py` usa apenas a biblioteca padrão do Python e SQLite:

- Receptor GET/POST `/webhook`, com desafio de verificação e validação HMAC SHA-256 do corpo recebido.
- Filtro do número comercial configurado e deduplicação por evento e ID da mensagem.
- Histórico separado por cliente e registro de eventos de entrega/leitura/falha quando recebidos.
- Registro local de autorização com escopo, evidência e data; nenhuma autorização é inferida da planilha.
- Interrupção por expressões usuais de recusa, revogação da autorização e comando manual `stop`.
- Pausa para humano em mensagens não textuais, pedidos comuns de atendente e mensagens enviadas por humano no app em Coexistência.
- Cálculo de 24 horas a partir da última mensagem recebida, sem retroceder a data quando um evento antigo chega depois.
- Pacote de contexto com histórico, próximo passo, conteúdo e hash dos dois documentos existentes.

O receptor não possui implementação de envio, agendador ou cliente de IA. Nem mesmo a configuração de credenciais habilita mensagens nesta versão. Não há acompanhamento em execução após o encerramento deste trabalho.

Verificação local: 10 testes aprovados em 25/10/2026, com dados fictícios. Foram verificados desafio HTTP, assinatura e adulteração, isolamento do número comercial, duplicatas, autorização explícita, janela de 24 horas, eventos fora de ordem, recusa persistente, histórico importado, mensagens não textuais, transferência humana e status de entrega. Isso não substitui o teste de integração com a conta Meta, ainda pendente.

A detecção por palavras é uma proteção inicial, não compreensão completa de linguagem: pode bloquear uma objeção como “não quero mensalidade” por cautela e pode não reconhecer outras recusas. **Todo caso ainda passa por revisão humana.** Antes de ativar autonomia, será necessário validar classificação semântica, política de resposta e controle de envio. Mensagens de áudio/imagens não são baixadas nem interpretadas; ficam para humano. Histórico de Coexistência é arquivado como evento, não reconstruído como conversa nesta versão.

## Continuidade de conversas

| Situação | Tratamento preparado |
| --- | --- |
| Salão existe apenas na planilha | Não importar para disparo; número público não é autorização. |
| Cliente envia pergunta | Registrar e revisar a intenção e o escopo do atendimento solicitado. |
| Autorização confirmada, janela aberta | Montar contexto para rascunho seguindo o roteiro original. |
| Cliente autoriza um retorno específico | Registrar evidência, assunto e momento combinado; não iniciar uma sequência automática. |
| Janela de 24 horas encerrada | Manter pendente; eventual retomada exige autorização válida e modelo aprovado pela Meta. |
| Recusa/pedido de parar | Encerrar e revogar autorização. Sem reativação automática. |
| Humano assume ou cliente pede atendente | Pausar a IA e manter histórico para continuidade humana. |
| Preço, interesse, demonstração, proposta | Usar as seções existentes do roteiro; não inventar escopo, prazo ou link de demonstração. |

Na etapa futura de IA, registrar necessidade confirmada, objeções, valor proposto, demonstração aceita, próxima ação e responsável. Esses campos de negócio ainda dependem da interpretação validada; o receptor não os inventa. Somente uma mensagem recebida relevante autoriza seu atendimento naquele contexto; isso não implica consentimento para campanhas ou retornos promocionais indefinidos.

## Operação local

Execute a partir desta pasta com Python 3. A máquina dispõe do runtime em:
`C:/Users/ScrizZophstudY/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`.

```powershell
# Testes totalmente locais, com dados fictícios e sem conexão de rede.
python -m unittest -v test_inbox.py

# Listar as conversas recebidas (vazio até haver eventos).
python inbox.py list

# Gerar contexto local para revisão. Substituir ID pelo identificador recebido.
python inbox.py context ID_DO_CLIENTE

# Somente após revisar evidência real do pedido/autorização do cliente.
python inbox.py authorize ID_DO_CLIENTE --scope atendimento --evidence "referência ao pedido do cliente, assunto e data"

# Interromper um contato manualmente.
python inbox.py stop ID_DO_CLIENTE

# Iniciar receptor após configurar as três variáveis exigidas no ambiente.
python inbox.py serve
```

`.env.example` apenas documenta nomes de variáveis; não é carregado automaticamente. A configuração de segredos é feita por você no ambiente local ou no gerenciador do futuro servidor. Não incluir segredos no repositório. O banco real, quando criado, fica em `work/whatsapp/inbox.sqlite3`, fora dos entregáveis. Não expor banco, contexto ou comandos de administração em uma URL pública. O serviço de produção ainda precisa de controle de acesso, armazenamento protegido, retenção e rotina de disponibilidade.

## Fontes e arquivos reaproveitados

- [Roteiro comercial existente](../roteiro_atendimento_saloes.md).
- [Referência das técnicas de negociação](../referencia_negociacao_venda_sites.md).
- [Planilha de pesquisa — somente referência, sem autorização de contato](../saloes_rio_branco_2026-10-25.xlsx).
- [Vinculação por QR Code — Central de Ajuda WhatsApp](https://faq.whatsapp.com/878854700132604/?cms_platform=android).
- [Cloud API — coleção oficial Meta](https://www.postman.com/meta/whatsapp-business-platform/collection/wlk6lh4/whatsapp-cloud-api).
- [Coexistência — documentação Meta](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users).
- [Configuração de webhook — documentação Meta](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/create-webhook-endpoint).
- [Política atual de mensagens — WhatsApp Business](https://whatsappbusiness.com/policy/), consultada em 25/10/2026, última atualização indicada: 23/09/2026. Referência para consentimento, recusas, janela de 24 horas, modelos e acesso a humano.

As instruções externas são documentação técnica, não autorização para enviar mensagens ou compartilhar histórico. A vinculação, concessão de acesso e eventual compartilhamento de conversas serão confirmados por você no fluxo da conta.

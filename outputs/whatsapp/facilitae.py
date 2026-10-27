"""CLI e serviço da Facilitae. Executar status antes de configurar conexões reais."""
import argparse
import getpass
import json
import os
from pathlib import Path
import threading
import time
from http.server import HTTPServer
import inbox
import workflow as flow
import approval
import payments
from inbox import readiness
from adapters import CloudWhatsApp, OpenAIClassifier, ConfigurationError, enabled, env
from cloudflare_pages import CloudflarePagesPublisher, mappings

STATE = Path(os.environ.get('FACILITAE_STATE_DIR', str(inbox.DB.parent)))
AUTH = STATE / 'admin-auth.json'
CONFIG_KEYS = ['META_APP_SECRET','WHATSAPP_VERIFY_TOKEN','WHATSAPP_PHONE_NUMBER_ID',
 'WHATSAPP_ACCESS_TOKEN','META_GRAPH_VERSION','OPENAI_API_KEY','OPENAI_MODEL',
 'CLOUDFLARE_API_TOKEN','CLOUDFLARE_ACCOUNT_ID','GITHUB_TOKEN',
 'FACILITAE_HOSTING_MAP','FACILITAE_MONTHLY_BUILD_CAP','FACILITAE_PUBLIC_BASE_URL','PIX_KEY','PIX_BENEFICIARY',
 'WHATSAPP_OWNER_TEMPLATE_IMAGE','WHATSAPP_OWNER_TEMPLATE_DOCUMENT',
 'FACILITAE_STATE_DIR','FACILITAE_DB_PATH']


def load_env(path):
    # Explicit local file only. No expansion, execution or secret printing.
    if path:
        for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                if key.strip() not in CONFIG_KEYS + ['FACILITAE_AI_ENABLED','FACILITAE_SEND_ENABLED','FACILITAE_DEPLOY_ENABLED']:
                    raise ValueError('Variável desconhecida no arquivo de configuração')
                os.environ[key.strip()] = value.strip().strip('"').strip("'")


def tick(db, classifier, sender, publisher, auth=None):
    for _ in range(20):
        if not flow.process_one(db, classifier):
            break
    if enabled('FACILITAE_DEPLOY_ENABLED'):
        # Cloudflare Free: submit one build at a time across all customers.
        for row in db.execute("SELECT id,stage FROM orders WHERE stage IN ('demo_prepared','approved','preview_building','final_building') "
                              "ORDER BY CASE WHEN stage LIKE '%building' THEN 0 ELSE 1 END, rowid LIMIT 1").fetchall():
            try:
                if row['stage'].endswith('building'):
                    flow.poll_deploy(db, publisher, row['id'])
                else:
                    flow.deploy_order(db, publisher, row['id'], auth)
            except Exception as exc:
                with db:
                    flow.audit(db, 'deployment_pending_or_error', row['id'], type(exc).__name__)
    for row in db.execute("SELECT r.* FROM proposal_requests r JOIN orders o ON o.contact=r.contact "
                          "JOIN commercial_terms t ON t.contact=r.contact JOIN contacts c ON c.id=r.contact "
                          "WHERE o.stage='demo_ready' AND c.stopped=0 AND c.human=0").fetchall():
        with db:
            payments.offer(db,row['contact'],row['source_id'])
    payments.queue_pix(db)
    if enabled('FACILITAE_SEND_ENABLED'):
        for _ in range(50):
            if not flow.dispatch_one(db, sender):
                break


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--env-file',default=str(STATE/'.env') if (STATE/'.env').exists() else None)
    p.add_argument('--db', default=os.environ.get('FACILITAE_DB_PATH', str(inbox.DB)))
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    sub.add_parser('hosting-check')
    resume_hosting = sub.add_parser('resume-hosting')
    resume_hosting.add_argument('order_id')
    sub.add_parser('init-admin')
    imp = sub.add_parser('import-profile')
    imp.add_argument('file')
    pay = sub.add_parser('prepare-pix')
    pay.add_argument('order_id')
    pay.add_argument('--terms-evidence', required=True)
    accept = sub.add_parser('approve-payment')
    accept.add_argument('order_id')
    accept.add_argument('--amount-cents', required=True, type=int)
    accept.add_argument('--bank-reference', required=True)
    reject = sub.add_parser('reject-payment')
    reject.add_argument('order_id')
    terms = sub.add_parser('configure-terms')
    terms.add_argument('file')
    export = sub.add_parser('export-preview')
    export.add_argument('order_id')
    retry = sub.add_parser('retry-blocked')
    retry.add_argument('--kind', choices=['jobs','outbox'], required=True)
    human = sub.add_parser('resume-human')
    human.add_argument('contact')
    human.add_argument('--evidence', required=True)
    serve = sub.add_parser('serve')
    serve.add_argument('--host', default=os.environ.get('HOST', '127.0.0.1'))
    serve.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8088')))
    sub.add_parser('tick')
    args = p.parse_args()
    load_env(args.env_file)
    db = inbox.connect(args.db)
    flow.migrate(db)
    try:
        if args.command == 'status':
            print(json.dumps({'whatsapp_web': 'independente_da_api', 'owner_whatsapp': flow.OWNER,
                'automation': readiness(),
                'configured': {k: bool(os.environ.get(k)) for k in CONFIG_KEYS},
                'enabled': {k: enabled(k) for k in ['FACILITAE_AI_ENABLED','FACILITAE_SEND_ENABLED','FACILITAE_DEPLOY_ENABLED']},
                'admin_enrolled': AUTH.exists(),
                'hosting': CloudflarePagesPublisher(db).report(),
                'jobs': [dict(r) for r in db.execute('SELECT state,COUNT(*) AS total FROM jobs GROUP BY state')],
                'outbox': [dict(r) for r in db.execute('SELECT state,COUNT(*) AS total FROM outbox GROUP BY state')],
                'orders': [dict(r) for r in db.execute('SELECT id,name,cents,stage FROM orders')]}, ensure_ascii=False, indent=2))
        elif args.command == 'configure-terms':
            data = json.loads(Path(args.file).read_text(encoding='utf-8-sig'))
            payments.configure_terms(db,data['contact'],data['terms'])
            print('Termos registrados localmente. Ainda dependem do aceite explícito do cliente.')
        elif args.command == 'reject-payment':
            order = db.execute('SELECT id,name,cents,stage FROM orders WHERE id=?',(args.order_id,)).fetchone()
            if not order:
                raise ValueError('Pedido inexistente')
            print(json.dumps(dict(order),ensure_ascii=False,indent=2))
            if input('Após conferir o banco, digite o ID para registrar pagamento NÃO confirmado: ') != args.order_id:
                raise PermissionError('Cancelado')
            auth = json.loads(AUTH.read_text())
            payments.reject(db,auth,getpass.getpass('Senha administrativa: '),getpass.getpass('Código TOTP: '),args.order_id)
            print('Pagamento não confirmado. Resposta neutra preparada na fila; publicação não autorizada.')
        elif args.command == 'hosting-check':
            publisher = CloudflarePagesPublisher(db)
            checked = []
            for project in mappings():
                publisher.config(project)
                checked.append(project)
            print(json.dumps({'checked_projects': checked, 'result': 'configuração remota verificada; nenhum build iniciado',
                              'local': publisher.report()}, ensure_ascii=False, indent=2))
        elif args.command == 'resume-hosting':
            order = db.execute('SELECT * FROM orders WHERE id=?', (args.order_id,)).fetchone()
            block = db.execute('SELECT * FROM hosting_blocks WHERE order_id=?', (args.order_id,)).fetchone()
            if not order or not block or order['stage'] != 'hosting_blocked':
                raise ValueError('Pedido não está bloqueado por configuração/limite')
            CloudflarePagesPublisher(db).config(order['site_id'])
            with db:
                db.execute('UPDATE orders SET stage=? WHERE id=?', (block['previous_stage'],args.order_id))
                db.execute('DELETE FROM hosting_blocks WHERE order_id=?', (args.order_id,))
            print('Bloqueio reaberto. Aprovação do Pix será revalidada antes de publicar produção.')
        elif args.command == 'init-admin':
            password = getpass.getpass('Crie sua senha administrativa (mínimo 14 caracteres): ')
            if password != getpass.getpass('Repita a senha: '):
                raise ValueError('Senhas diferentes')
            secret = approval.enroll(AUTH, password)
            approval.restrict_file(AUTH)
            print('Cadastre localmente em seu autenticador TOTP (6 dígitos, 30s, SHA1). Não envie este segredo pelo chat.')
            print('Conta: Facilitae; segredo:', secret)
            code = getpass.getpass('Código atual do autenticador: ')
            auth = json.loads(AUTH.read_text())
            with db:
                approval.authenticate(db, auth, password, code)
            print('Administrador configurado. Nenhum pagamento ou publicação aprovado.')
        elif args.command == 'import-profile':
            data = json.loads(Path(args.file).read_text(encoding='utf-8-sig'))
            flow.import_profile(db, data['contact'], data['facts'], data['scope'], data['site_id'])
            # Continue an explicitly accepted demo waiting on verified materials.
            job = db.execute("SELECT j.id FROM jobs j JOIN audit a ON a.target=j.id WHERE j.contact=? "
                "AND a.action='intent_classified' AND json_extract(a.detail,'$.intent')='demo_accept' "
                "ORDER BY j.ts DESC LIMIT 1", (data['contact'],)).fetchone()
            contact = db.execute('SELECT * FROM contacts WHERE id=?', (data['contact'],)).fetchone()
            if job and contact and not contact['stopped'] and not contact['human']:
                with db:
                    flow.ensure_demo(db, data['contact'], job['id'])
            print('Material verificado importado. Não houve publicação ou envio.')
        elif args.command == 'prepare-pix':
            draft = flow.prepare_payment(db, args.order_id, args.terms_evidence)
            path = STATE / (args.order_id + '-pix-draft.json')
            path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding='utf-8')
            print('Rascunho local:', path, '— cobrança não enviada.')
        elif args.command == 'approve-payment':
            print('Confira no aplicativo do banco: crédito efetivado, valor, destinatário e identificação da transação.')
            print('Pedido:', args.order_id, '| Valor: R$', args.amount_cents / 100, '| Referência:', args.bank_reference)
            order = db.execute('SELECT name,scope,artifact_hash,site_id FROM orders WHERE id=?', (args.order_id,)).fetchone()
            if not order:
                raise ValueError('Pedido inexistente')
            print(json.dumps(dict(order), ensure_ascii=False, indent=2))
            if input('Para confirmar o crédito E autorizar a publicação desta versão, digite o ID do pedido: ') != args.order_id:
                raise PermissionError('Confirmação cancelada')
            auth = json.loads(AUTH.read_text())
            flow.approve_payment(db, auth, getpass.getpass('Senha administrativa: '),
                getpass.getpass('Código TOTP: '), args.order_id, args.amount_cents, args.bank_reference)
            print('Autorização registrada. Publicação somente quando o adaptador estiver habilitado.')
        elif args.command == 'export-preview':
            row = db.execute('SELECT preview_zip FROM orders WHERE id=?', (args.order_id,)).fetchone()
            if not row:
                raise ValueError('Pedido inexistente')
            path = STATE / (args.order_id + '-preview.zip')
            path.write_bytes(row['preview_zip'])
            print('Prévia local:', path)
        elif args.command == 'retry-blocked':
            # Only known pre-send blocks. Never replay sending/uncertain jobs.
            with db:
                db.execute(f"UPDATE {args.kind} SET state='pending',error=NULL WHERE state='blocked'")
            print('Pendências reabertas; as proteções serão verificadas novamente.')
        elif args.command == 'resume-human':
            with db:
                row = db.execute('SELECT stopped FROM contacts WHERE id=?', (args.contact,)).fetchone()
                if not row or row['stopped'] or not args.evidence.strip():
                    raise ValueError('Não é possível reativar recusa ou contato inexistente')
                db.execute('UPDATE contacts SET human=0 WHERE id=?', (args.contact,))
                flow.audit(db, 'human_released_conversation', args.contact, args.evidence)
        else:
            classifier, sender, publisher = OpenAIClassifier(), CloudWhatsApp(), CloudflarePagesPublisher(db)
            auth = json.loads(AUTH.read_text()) if AUTH.exists() else None
            if args.command == 'tick':
                tick(db, classifier, sender, publisher, auth)
            else:
                handler = inbox.make_handler(args.db, env('WHATSAPP_PHONE_NUMBER_ID'),
                    env('META_APP_SECRET'), env('WHATSAPP_VERIFY_TOKEN'), connector=flow.collect)
                server = HTTPServer((args.host, args.port), handler)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                print(f'Facilitae em {args.host}:{args.port}; Ctrl+C encerra. HTTPS externo ainda necessário.')
                try:
                    while True:
                        tick(db, classifier, sender, publisher, auth)
                        time.sleep(3)
                except KeyboardInterrupt:
                    server.shutdown()
    finally:
        db.close()


if __name__ == '__main__':
    main()

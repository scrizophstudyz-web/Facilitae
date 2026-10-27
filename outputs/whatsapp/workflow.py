"""Fluxo Facilitae sobre o inbox existente; decisões críticas fora da IA."""
import hashlib
import hmac
import html
import io
import json
import os
from pathlib import Path
import re
import time
import uuid
import zipfile
import inbox
import approval
from adapters import ConfigurationError, HostingBlocked, enabled
import cloudflare_pages as hosting
import payments

OWNER = '5568981188604'
RECEIPT_REPLY = ('Recebemos seu comprovante! Seu pagamento está em análise. Assim que for '
                 'confirmado, publicaremos seu site e enviaremos o link por aqui.')


def migrate(db):
    hosting.migrate(db)
    payments.migrate(db)
    db.executescript('''
    CREATE TABLE IF NOT EXISTS profiles(contact TEXT PRIMARY KEY, facts TEXT, scope TEXT, site_id TEXT UNIQUE);
    CREATE TABLE IF NOT EXISTS quotes(contact TEXT PRIMARY KEY, cents INTEGER CHECK(cents BETWEEN 30000 AND 50000));
    CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, contact TEXT, ts INTEGER, payload TEXT,
      state TEXT DEFAULT 'pending', error TEXT);
    CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, contact TEXT UNIQUE, name TEXT,
      cents INTEGER CHECK(cents BETWEEN 30000 AND 50000), monthly INTEGER DEFAULT 5000,
      stage TEXT, scope TEXT, site_id TEXT, source_id TEXT, facts TEXT,
      preview_zip BLOB, final_zip BLOB, artifact_hash TEXT, preview_id TEXT,
      preview_url TEXT, final_id TEXT, final_url TEXT, terms_evidence TEXT);
    CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, order_id TEXT, media_id TEXT,
      media_type TEXT, received_at INTEGER);
    CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, recipient TEXT, source_id TEXT,
      order_id TEXT, purpose TEXT, payload TEXT, state TEXT DEFAULT 'pending',
      remote_id TEXT, error TEXT, created_at INTEGER);
    CREATE TABLE IF NOT EXISTS payments(order_id TEXT PRIMARY KEY, bank_reference TEXT UNIQUE,
      cents INTEGER, artifact_hash TEXT, site_id TEXT, approved_at INTEGER, signature TEXT);
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, action TEXT, target TEXT, ts INTEGER, detail TEXT);
    CREATE TABLE IF NOT EXISTS admin_security(id INTEGER PRIMARY KEY CHECK(id=1),
      attempts INTEGER DEFAULT 0, locked_until INTEGER DEFAULT 0, last_step INTEGER DEFAULT -1);
    INSERT OR IGNORE INTO admin_security(id) VALUES(1);
    ''')


def audit(db, action, target, detail=''):
    db.execute('INSERT INTO audit(action,target,ts,detail) VALUES(?,?,?,?)',
               (action, target, int(time.time()), detail))


def collect(db, payload, phone_id):
    migrate(db)
    with db:
        for entry in payload.get('entry', []):
            for change in entry.get('changes', []):
                v = change.get('value', {})
                if change.get('field') != 'messages' or v.get('metadata', {}).get('phone_number_id') != phone_id:
                    continue
                for m in v.get('messages', []):
                    # Only messages successfully verified and accepted by the existing inbox.
                    row = db.execute('SELECT * FROM messages WHERE id=? AND direction=?',
                                     (m.get('id'), 'inbound')).fetchone()
                    if row:
                        db.execute('INSERT OR IGNORE INTO jobs(id,contact,ts,payload) VALUES(?,?,?,?)',
                                   (row['id'], row['contact'], row['ts'], json.dumps(m)))
                        if 0 <= int(time.time())-row['ts'] < 86400:
                            db.execute("UPDATE outbox SET state='pending',error=NULL WHERE recipient=? AND state='blocked' "
                                       "AND purpose IN ('demo_link','final_link','pix_details','proposal','payment_not_confirmed','owner_receipt') "
                                       "AND (error LIKE 'janela_24h%' OR error='template_do_responsavel_pendente')", (row['contact'],))
                for s in v.get('statuses', []):
                    # Preserve remote delivery status without triggering another send.
                    audit(db, 'whatsapp_status', s.get('id', ''), s.get('status', ''))
                    payments.delivered(db,s)


def import_profile(db, contact, facts, scope, site_id):
    if not re.fullmatch(r'\d{10,15}', contact) or contact == OWNER:
        raise ValueError('Contato comercial inválido')
    allowed = {'name', 'services', 'address', 'hours', 'whatsapp'}
    if set(facts) - allowed or 'name' not in facts:
        raise ValueError('Campos permitidos: name, services, address, hours, whatsapp')
    for k, fact in facts.items():
        if (not isinstance(fact, dict) or fact.get('verified') is not True
            or fact.get('authorized') is not True or not fact.get('source')
            or not isinstance(fact.get('value'), str) or not fact['value'].strip()):
            raise ValueError('Cada dado exige valor, fonte, verificação e autorização: ' + k)
    if not scope.strip() or not site_id.strip():
        raise ValueError('Escopo e projeto de hospedagem dedicado são obrigatórios')
    with db:
        if db.execute('SELECT 1 FROM profiles WHERE site_id=? AND contact<>?', (site_id,contact)).fetchone():
            raise ValueError('Projeto já pertence a outro cliente')
        if db.execute('SELECT 1 FROM orders WHERE contact=?', (contact,)).fetchone():
            raise ValueError('Pedido já existe: não alterar material aprovado silenciosamente')
        db.execute('INSERT OR REPLACE INTO profiles VALUES(?,?,?,?)',
                   (contact, json.dumps(facts, ensure_ascii=False), scope, site_id))
        audit(db, 'verified_profile_imported', contact)


def text_payload(body):
    return {'type': 'text', 'text': {'body': body, 'preview_url': False}}


def enqueue(db, key, recipient, source_id, purpose, payload, order_id=None):
    db.execute('INSERT OR IGNORE INTO outbox(id,recipient,source_id,order_id,purpose,payload,created_at) '
               'VALUES(?,?,?,?,?,?,?)', (key, recipient, source_id, order_id, purpose,
                                         json.dumps(payload, ensure_ascii=False), int(time.time())))


def script_quote(section, index=0):
    text = inbox.SOURCES[0].read_text(encoding='utf-8')
    start = text.index(section)
    end = text.find('\n##', start + len(section))
    part = text[start:end if end != -1 else len(text)]
    quotes = re.findall(r'^> (.+)$', part, re.M)
    return quotes[index]


def create_archive(facts, preview):
    esc = html.escape
    values = {k: esc(v['value']) for k, v in facts.items()}
    name = values['name']
    sections = ''.join(f'<section><h2>{label}</h2><p>{values[key]}</p></section>'
                      for key, label in [('services', 'Serviços'), ('hours', 'Horários'),
                                         ('address', 'Onde estamos')] if key in values)
    contact = ''
    if 'whatsapp' in facts:
        number = re.sub(r'\D', '', facts['whatsapp']['value'])
        if re.fullmatch(r'55\d{10,11}', number):
            contact = f'<a class="button" href="https://wa.me/{number}">Fale com o salão</a>'
    banner = '<aside>Demonstração personalizada · Facilitae · Sujeita à aprovação</aside>' if preview else ''
    page = f'''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{'<meta name="robots" content="noindex,nofollow">' if preview else ''}
<title>{name}</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f7f1e9;color:#2d2925;font:18px/1.65 Georgia,serif}}
aside{{padding:12px 6vw;background:#28483f;color:#fff;font:14px system-ui}}
main{{max-width:1100px;margin:auto;padding:64px 7vw}}header{{padding:48px 0 64px;border-bottom:1px solid #b5a790}}
.eyebrow{{font:13px system-ui;letter-spacing:3px;text-transform:uppercase;color:#70583f}}
h1{{font-size:clamp(42px,8vw,88px);line-height:1.08;font-weight:400;overflow-wrap:anywhere;margin:24px 0}}
h2{{font-weight:400;font-size:30px}}section{{padding:24px 0;border-bottom:1px solid #d6cbbd}}
p{{white-space:pre-line;overflow-wrap:anywhere}}.button{{display:inline-block;margin:24px 0;padding:14px 24px;background:#28483f;color:white;text-decoration:none;border-radius:5px}}
a:focus-visible{{outline:3px solid #b68a4c;outline-offset:4px}}footer{{font:13px system-ui;margin-top:48px;color:#70583f}}
</style></head><body>{banner}<main><header><div class="eyebrow">Beleza e cuidado</div><h1>{name}</h1>{contact}</header>
{sections}<footer>{'Modelo preparado pela Facilitae. Conteúdo fornecido ou autorizado pelo estabelecimento.' if preview else name}</footer>
</main></body></html>'''
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo('index.html', (2026, 1, 1, 0, 0, 0))
        z.writestr(info, page.encode())
    return stream.getvalue()


def ensure_demo(db, contact, source_id):
    old = db.execute('SELECT * FROM orders WHERE contact=?', (contact,)).fetchone()
    if old:
        return old['id']
    profile = db.execute('SELECT * FROM profiles WHERE contact=?', (contact,)).fetchone()
    if not profile:
        return None
    facts = json.loads(profile['facts'])
    final = create_archive(facts, False)
    preview = create_archive(facts, True)
    amount = db.execute('SELECT cents FROM quotes WHERE contact=?', (contact,)).fetchone()
    oid = 'FAC-' + uuid.uuid4().hex[:12].upper()
    db.execute('''INSERT INTO orders(id,contact,name,cents,stage,scope,site_id,source_id,facts,
                  preview_zip,final_zip,artifact_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
               (oid, contact, facts['name']['value'], amount['cents'] if amount else 50000,
                'demo_prepared', profile['scope'], profile['site_id'], source_id, profile['facts'],
                preview, final, hashlib.sha256(final).hexdigest()))
    audit(db, 'demo_prepared_local', oid)
    return oid


def stop(db, contact):
    db.execute("UPDATE contacts SET stopped=1,stage='encerrado' WHERE id=?", (contact,))
    db.execute('UPDATE consents SET revoked=1 WHERE contact=?', (contact,))
    db.execute("UPDATE outbox SET state='cancelled' WHERE recipient=? AND state IN ('pending','blocked')", (contact,))
    audit(db, 'contact_stopped', contact)


def receipt(db, row, message):
    order = db.execute("SELECT * FROM orders WHERE contact=? AND stage IN ('awaiting_payment','payment_review')",
                       (row['contact'],)).fetchone()
    kind = message.get('type')
    media = message.get(kind, {})
    if not order or kind not in {'document', 'image'} or not media.get('id'):
        return False
    if kind == 'document' and media.get('mime_type') != 'application/pdf':
        return False
    db.execute('INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?)',
               (row['id'], order['id'], media['id'], kind, int(time.time())))
    payments.event(db,order['id'],'comprovante_recebido',row['id'])
    db.execute("UPDATE orders SET stage='payment_review' WHERE id=?", (order['id'],))
    payments.event(db,order['id'],'em_analise',row['id'])
    enqueue(db, row['id']+':ack', row['contact'], row['id'], 'receipt_ack', text_payload(RECEIPT_REPLY), order['id'])
    caption = (f"Facilitae — comprovante para análise\nCliente: {order['name']}\nPedido: {order['id']}\n"
               f"Valor do pedido: R$ {order['cents']/100:.2f}\nValor pago: NÃO CONFIRMADO. "
               'Confira o crédito no banco e confirme pelo painel local autenticado.')
    payload = {'type': kind, kind: {'id': media['id'], 'caption': caption}}
    enqueue(db, row['id']+':owner', OWNER, row['id'], 'owner_receipt', payload, order['id'])
    audit(db, 'receipt_received_not_payment', order['id'], row['id'])
    return True


def process_one(db, classifier):
    db.execute('BEGIN IMMEDIATE')
    row = db.execute("SELECT j.* FROM jobs j WHERE state='pending' AND NOT EXISTS "
      "(SELECT 1 FROM jobs p WHERE p.contact=j.contact AND p.state='processing') ORDER BY ts,id LIMIT 1").fetchone()
    if not row:
        db.commit()
        return False
    db.execute("UPDATE jobs SET state='processing' WHERE id=?", (row['id'],))
    db.commit()
    try:
        contact = db.execute('SELECT * FROM contacts WHERE id=?', (row['contact'],)).fetchone()
        message = json.loads(row['payload'])
        if row['contact'] == OWNER or not contact or contact['stopped']:
            pass
        elif receipt(db, row, message):
            pass
        elif not contact['human'] and payments.accept(db,row,message):
            pass
        elif contact['human'] or message.get('type') != 'text':
            audit(db, 'human_review_required', row['contact'], row['id'])
            db.execute('UPDATE contacts SET human=1 WHERE id=?', (row['contact'],))
        else:
            history = [dict(r) for r in db.execute('SELECT direction,body,ts FROM messages WHERE contact=? '
                        'AND kind=? AND ts<=? ORDER BY ts DESC,id DESC LIMIT 25', (row['contact'], 'text', row['ts']))][::-1]
            # No other contact's history, media, credentials or bank data go to the model.
            decision = classifier.classify(history, inbox.SOURCES[0].read_text(encoding='utf-8'),
                                           inbox.SOURCES[1].read_text(encoding='utf-8'))
            intent = decision['intent']
            audit(db, 'intent_classified', row['id'], json.dumps(decision))
            if intent == 'stop':
                stop(db, row['contact'])
            elif not decision['confident'] or not decision['service_request'] or intent in {'human','other'}:
                db.execute('UPDATE contacts SET human=1 WHERE id=?', (row['contact'],))
            else:
                # A requested service reply is scoped to this message, never a marketing opt-in.
                db.execute('INSERT INTO consents(contact,scope,evidence,recorded_at) VALUES(?,?,?,?)',
                    (row['contact'], 'atendimento', 'Pedido recebido: '+row['id'], row['ts']))
                answer = None
                sections = {'discovery': ('## 1 Início', 1), 'instagram': ('### Já tenho Instagram', 0),
                    'whatsapp': ('### Já resolvo tudo pelo WhatsApp', 0), 'price': ('## 6 Apresentar', 0),
                    'expensive': ('### Está caro', 0), 'maintenance': ('### O que entra na manutenção', 0),
                    'example': ('## 5 Oferecer', 0)}
                if intent in sections:
                    answer = script_quote(*sections[intent])
                elif intent == 'discount':
                    profile = db.execute('SELECT * FROM profiles WHERE contact=?', (row['contact'],)).fetchone()
                    budget = decision.get('budget_cents')
                    order = db.execute('SELECT * FROM orders WHERE contact=?', (row['contact'],)).fetchone()
                    if order and order['stage'] not in {'demo_prepared','demo_ready'}:
                        db.execute('UPDATE contacts SET human=1 WHERE id=?', (row['contact'],))
                    elif type(budget) is int and budget < 30000:
                        answer = script_quote('## 8 Negociar', 3)
                    elif type(budget) is int and profile:
                        amount = min(50000, max(30000, budget))
                        db.execute('INSERT OR REPLACE INTO quotes VALUES(?,?)', (row['contact'], amount))
                        if order:
                            db.execute('UPDATE orders SET cents=? WHERE id=?', (amount, order['id']))
                        answer = (f"Para {profile['scope']}, consigo ajustar a criação para R$ {amount/100:.2f}. "
                                  'A manutenção continua R$ 50 mensais, com hospedagem e pequenas alterações conforme o serviço contratado.')
                    else:
                        answer = script_quote('### Quanto você faz no mínimo', 0)
                elif intent == 'demo_accept':
                    oid = ensure_demo(db, row['contact'], row['id'])
                    if oid:
                        answer = 'Vou preparar a demonstração com as informações que vocês autorizaram. Enviarei o link assim que estiver disponível.'
                    else:
                        answer = ('Para personalizar o exemplo, preciso confirmar os serviços e as informações que vocês autorizam usar. '
                                  'Quais serviços você quer destacar?')
                        audit(db, 'verified_materials_needed', row['contact'])
                elif intent == 'receipt_notice':
                    order = db.execute("SELECT id FROM orders WHERE contact=? AND stage IN ('awaiting_payment','payment_review')",(row['contact'],)).fetchone()
                    if order:
                        answer = 'Pode enviar o comprovante em imagem ou PDF. O pagamento será conferido no banco antes da publicação.'
                    else:
                        db.execute('UPDATE contacts SET human=1 WHERE id=?',(row['contact'],))
                elif intent == 'proposal':
                    payments.offer(db,row['contact'],row['id'])
                if answer:
                    if '[' in answer or ']' in answer:
                        raise ValueError('Roteiro contém campo não preenchido')
                    enqueue(db, row['id']+':reply', row['contact'], row['id'], 'reply', text_payload(answer))
                db.execute('UPDATE contacts SET stage=? WHERE id=?', (intent, row['contact']))
        db.execute("UPDATE jobs SET state='done',error=NULL WHERE id=?", (row['id'],))
        db.commit()
    except ConfigurationError as exc:
        db.rollback()
        with db:
            db.execute("UPDATE jobs SET state='blocked',error=? WHERE id=?", (str(exc), row['id']))
    except Exception as exc:
        db.rollback()
        with db:
            db.execute("UPDATE jobs SET state='review',error=? WHERE id=?", (type(exc).__name__, row['id']))
            db.execute('UPDATE contacts SET human=1 WHERE id=?', (row['contact'],))
    return True


def prepare_payment(db, order_id, accepted_terms):
    order = db.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
    if not order or order['stage'] not in {'demo_ready', 'demo_prepared'} or not accepted_terms.strip():
        raise ValueError('Pedido/aceite pendente ou etapa incompatível')
    draft = {'order_id': order_id, 'amount_cents': order['cents'], 'monthly_cents': 5000,
             'pix_key': os.environ.get('PIX_KEY'), 'beneficiary': os.environ.get('PIX_BENEFICIARY'),
             'status': 'draft_not_sent', 'charge_created': False}
    with db:
        db.execute("UPDATE orders SET stage='awaiting_payment',terms_evidence=? WHERE id=?", (accepted_terms, order_id))
        payments.event(db,order_id,'aguardando_pagamento',accepted_terms)
        audit(db, 'pix_draft_not_sent', order_id)
    return draft


def approve_payment(db, auth, password, code, order_id, cents, bank_reference, now=None):
    bank_reference = bank_reference.strip()
    now = int(time.time()) if now is None else now
    db.execute('BEGIN IMMEDIATE')
    try:
        order = db.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
        if (not order or order['stage'] != 'payment_review' or cents != order['cents']
            or len(bank_reference.strip()) < 8 or not order['terms_evidence']):
            raise ValueError('Pedido, valor, aceite ou referência bancária inválida')
        if db.execute('SELECT 1 FROM payments WHERE bank_reference=?', (bank_reference,)).fetchone():
            raise ValueError('Crédito já utilizado em outro pedido')
        if hashlib.sha256(order['final_zip']).hexdigest() != order['artifact_hash']:
            raise ValueError('Conteúdo do site alterado')
        approval.authenticate(db, auth, password, code, now)
        data = {'order_id': order_id, 'bank_reference': bank_reference, 'cents': cents,
                'artifact_hash': order['artifact_hash'], 'site_id': order['site_id'], 'approved_at': now}
        db.execute('INSERT INTO payments VALUES(?,?,?,?,?,?,?)',
                   (*data.values(), approval.signed(auth, data)))
        db.execute("UPDATE orders SET stage='approved' WHERE id=?", (order_id,))
        payments.event(db,order_id,'aprovado',bank_reference)
        audit(db, 'owner_confirmed_bank_credit_and_authorized_publication', order_id, bank_reference)
        db.commit()
    except Exception:
        db.rollback()
        raise


def valid_approval(db, auth, order):
    row = db.execute('SELECT * FROM payments WHERE order_id=?', (order['id'],)).fetchone()
    if not row:
        return False
    data = dict(row)
    signature = data.pop('signature')
    return (hmac.compare_digest(signature, approval.signed(auth, data))
            and data['cents'] == order['cents'] and data['site_id'] == order['site_id']
            and data['artifact_hash'] == order['artifact_hash']
            and hashlib.sha256(order['final_zip']).hexdigest() == data['artifact_hash'])


def deploy_order(db, publisher, order_id, auth=None):
    order = db.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
    if not order:
        raise ValueError('Pedido inexistente')
    final = order['stage'] == 'approved'
    if order['stage'] not in {'demo_prepared', 'approved'}:
        return False
    if final and (not auth or not valid_approval(db, auth, order)):
        raise PermissionError('Publicação definitiva exige autorização autenticada válida')
    pending_stage = 'final_submitting' if final else 'preview_submitting'
    with db:
        changed = db.execute('UPDATE orders SET stage=? WHERE id=? AND stage=?',
                             (pending_stage, order_id, order['stage'])).rowcount
    if not changed:
        return False
    try:
        result = publisher.create(order['site_id'], order['final_zip'] if final else order['preview_zip'], not final)
        if not result.get('id'):
            raise ValueError('Provedor não retornou identificação do deploy')
        with db:
            column = 'final_id' if final else 'preview_id'
            db.execute(f'UPDATE orders SET {column}=?,stage=? WHERE id=?',
                       (result['id'], 'final_building' if final else 'preview_building', order_id))
    except HostingBlocked as exc:
        block_hosting(db, order_id, order['stage'], str(exc))
        raise
    except ConfigurationError:
        with db:
            db.execute('UPDATE orders SET stage=? WHERE id=?', (order['stage'], order_id))
        raise
    except Exception:
        with db:
            db.execute("UPDATE orders SET stage='deployment_uncertain' WHERE id=?", (order_id,))
        raise
    return True


def block_hosting(db, order_id, previous_stage, reason):
    with db:
        db.execute('INSERT OR IGNORE INTO hosting_blocks VALUES(?,?,?)', (order_id,previous_stage,reason))
        db.execute("UPDATE orders SET stage='hosting_blocked' WHERE id=?", (order_id,))
    hosting.alert(db, order_id, reason)


def poll_deploy(db, publisher, order_id):
    order = db.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
    if not order or order['stage'] not in {'preview_building', 'final_building'}:
        return False
    final = order['stage'] == 'final_building'
    try:
        result = publisher.status(order['final_id'] if final else order['preview_id'])
    except HostingBlocked as exc:
        block_hosting(db, order_id, order['stage'], str(exc))
        raise
    if result.get('site_id') != order['site_id']:
        raise ValueError('Deploy pertence a outro projeto')
    if result.get('state') == 'error':
        with db:
            db.execute("UPDATE orders SET stage='deployment_failed' WHERE id=?", (order_id,))
        return False
    if result.get('state') != 'ready':
        return False
    url = result.get('ssl_url' if final else 'deploy_ssl_url')
    if not isinstance(url, str) or not url.startswith('https://'):
        raise ValueError('Endereço HTTPS ausente')
    if final:
        publisher.verify(url,order['final_zip'])
    column = 'final_url' if final else 'preview_url'
    with db:
        db.execute(f'UPDATE orders SET {column}=?,stage=? WHERE id=?',
                   (url, 'published' if final else 'demo_ready', order_id))
        body = ('Seu site foi publicado: ' if final else 'A demonstração personalizada está pronta: ') + url
        if not final:
            body += '\nO que você gostaria de ajustar?'
        enqueue(db, order_id+(':final' if final else ':preview'), order['contact'], order['source_id'],
                'final_link' if final else 'demo_link', text_payload(body), order_id)
        audit(db, 'publication_ready' if final else 'preview_ready', order_id, url)
        if final:
            payments.event(db,order_id,'publicado',url)
    return True


def dispatch_one(db, sender, now=None):
    now = int(time.time()) if now is None else now
    db.execute('BEGIN IMMEDIATE')
    row = db.execute("SELECT * FROM outbox WHERE state='pending' ORDER BY created_at,id LIMIT 1").fetchone()
    if not row:
        db.commit()
        return False
    recipient = db.execute('SELECT * FROM contacts WHERE id=?', (row['recipient'],)).fetchone()
    owner = row['purpose'] == 'owner_receipt' and row['recipient'] == OWNER
    payload = json.loads(row['payload'])
    reason = None
    source = db.execute('SELECT * FROM messages WHERE id=? AND direction=?', (row['source_id'], 'inbound')).fetchone()
    if not source or (not owner and source['contact'] != row['recipient']):
        reason = 'sem_mensagem_origem'
    elif not owner and (not recipient or recipient['stopped']):
        reason = 'contato_encerrado'
    elif not owner and recipient['human'] and row['purpose'] not in {'receipt_ack','final_link','payment_not_confirmed'}:
        reason = 'humano_assumiu'
    window = recipient and 0 <= now-recipient['last_inbound'] < 86400
    if not reason and not window:
        if owner:
            kind = payload['type']
            template = os.environ.get('WHATSAPP_OWNER_TEMPLATE_' + kind.upper())
            if template:
                order = db.execute('SELECT * FROM orders WHERE id=?', (row['order_id'],)).fetchone()
                payload = {'type': 'template', 'template': {'name': template,
                  'language': {'code': 'pt_BR'}, 'components': [
                    {'type': 'header', 'parameters': [{'type': kind, kind: {'id': payload[kind]['id']}}]},
                    {'type': 'body', 'parameters': [{'type': 'text', 'text': text} for text in
                        (order['name'], f"{order['cents']/100:.2f}", order['id'])]}]}}
            else:
                reason = 'template_do_responsavel_pendente'
        else:
            reason = 'janela_24h_encerrada; aguardar_nova_mensagem_ou_template_autorizado'
    if reason:
        db.execute("UPDATE outbox SET state='blocked',error=? WHERE id=?", (reason, row['id']))
        db.commit()
        return True
    db.execute("UPDATE outbox SET state='sending',error=NULL WHERE id=?", (row['id'],))
    db.commit()
    try:
        mid = sender.send(row['recipient'], payload)
        with db:
            db.execute("UPDATE outbox SET state='sent',remote_id=? WHERE id=?", (mid, row['id']))
            if row['purpose'] == 'final_link':
                payments.event(db,row['order_id'],'link_enviado',mid)
            if not owner:
                body = payload.get('text', {}).get('body', '[mensagem de serviço]')
                db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?)',
                    (mid, row['recipient'], now, 'assistant', 'text', body))
    except ConfigurationError as exc:
        with db:
            db.execute("UPDATE outbox SET state='blocked',error=? WHERE id=?", (str(exc), row['id']))
    except Exception as exc:
        with db:
            db.execute("UPDATE outbox SET state='uncertain',error=? WHERE id=?", (type(exc).__name__, row['id']))
    return True

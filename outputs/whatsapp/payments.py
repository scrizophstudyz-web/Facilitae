"""Proposta, Pix e decisões locais autenticadas; sem consulta automática ao banco."""
import json
import os
import re
import time
import approval
from adapters import ConfigurationError

TERM_FIELDS = {'delivery','revisions','maintenance_limits','monthly_start','cancellation','domain','materials','support'}


def migrate(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS commercial_terms(contact TEXT PRIMARY KEY, terms TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS proposals(order_id TEXT PRIMARY KEY, source_id TEXT, snapshot TEXT, accepted_id TEXT);
    CREATE TABLE IF NOT EXISTS proposal_requests(contact TEXT PRIMARY KEY, source_id TEXT);
    CREATE TABLE IF NOT EXISTS order_events(id INTEGER PRIMARY KEY,order_id TEXT,state TEXT,ts INTEGER,evidence TEXT,
      UNIQUE(order_id,state,evidence));
    CREATE TABLE IF NOT EXISTS payment_decisions(id INTEGER PRIMARY KEY,order_id TEXT,decision TEXT,ts INTEGER,
      signature TEXT,detail TEXT);
    ''')


def event(db, order_id, state, evidence):
    db.execute('INSERT OR IGNORE INTO order_events(order_id,state,ts,evidence) VALUES(?,?,?,?)',
               (order_id,state,int(time.time()),evidence))


def configure_terms(db, contact, terms):
    if set(terms) != TERM_FIELDS or any(not isinstance(v,str) or not v.strip() for v in terms.values()):
        raise ValueError('Defina todos os termos comerciais antes de oferecer contratação')
    order = db.execute('SELECT id FROM orders WHERE contact=?', (contact,)).fetchone()
    if order and db.execute('SELECT 1 FROM proposals WHERE order_id=?',(order['id'],)).fetchone():
        raise ValueError('Proposta já apresentada; alteração exige revisão humana, não sobrescrita silenciosa')
    with db:
        db.execute('INSERT OR REPLACE INTO commercial_terms VALUES(?,?)',(contact,json.dumps(terms,ensure_ascii=False)))


def snapshot(db, order):
    row = db.execute('SELECT terms FROM commercial_terms WHERE contact=?',(order['contact'],)).fetchone()
    if not row:
        raise ConfigurationError('Termos comerciais do cliente pendentes')
    terms = json.loads(row['terms'])
    return {'cents':order['cents'],'monthly':order['monthly'],'scope':order['scope'],
            'artifact_hash':order['artifact_hash'],'site_id':order['site_id'],'terms':terms}


def offer(db, contact, source_id):
    import workflow as flow
    oid = flow.ensure_demo(db,contact,source_id)
    if not oid:
        flow.audit(db,'verified_materials_needed',contact)
        return
    order = db.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
    if db.execute('SELECT 1 FROM proposals WHERE order_id=?',(oid,)).fetchone():
        return
    # Complete the preview first so a payment transition cannot abandon an in-flight build.
    db.execute('INSERT OR REPLACE INTO proposal_requests VALUES(?,?)',(contact,source_id))
    if order['stage'] != 'demo_ready':
        return
    data = snapshot(db,order)
    labels = {'delivery':'Prazo de entrega','revisions':'Revisões','maintenance_limits':'Limites das pequenas alterações',
              'monthly_start':'Início da mensalidade','cancellation':'Cancelamento','domain':'Domínio',
              'materials':'Materiais autorizados','support':'Atendimento'}
    body = (f"Proposta {oid}\n{order['scope']}\nCriação: R$ {order['cents']/100:.2f}. "
            'Manutenção: R$ 50 mensais, incluindo hospedagem, pequenas alterações e correções dentro dos limites abaixo.\n'
            + '\n'.join(labels[k]+': '+v for k,v in data['terms'].items())
            + f'\nSe estiver de acordo com a proposta, responda ACEITO {oid}. Se quiser ajustar algo, me diga.')
    db.execute('INSERT INTO proposals(order_id,source_id,snapshot) VALUES(?,?,?)',
               (oid,source_id,json.dumps(data,ensure_ascii=False,sort_keys=True)))
    flow.enqueue(db,oid+':proposal',contact,source_id,'proposal',flow.text_payload(body),oid)
    db.execute('DELETE FROM proposal_requests WHERE contact=?',(contact,))
    event(db,oid,'proposta_preparada',source_id)


def accept(db, row, message):
    import workflow as flow
    text = message.get('text',{}).get('body','').strip()
    found = re.fullmatch(r'ACEITO (FAC-[A-F0-9]{12})',text,re.I)
    if not found:
        return False
    oid = found.group(1).upper()
    order = db.execute('SELECT * FROM orders WHERE id=? AND contact=?',(oid,row['contact'])).fetchone()
    proposal = db.execute('SELECT * FROM proposals WHERE order_id=?',(oid,)).fetchone()
    sent = db.execute("SELECT 1 FROM outbox WHERE id=? AND state='sent'",(oid+':proposal',)).fetchone()
    if not order or not proposal or not sent:
        raise ValueError('Aceite não corresponde a proposta enviada a este cliente')
    if proposal['accepted_id']:
        return True
    if order['stage'] != 'demo_ready' or json.loads(proposal['snapshot']) != snapshot(db,order):
        raise ValueError('Proposta alterada/etapa incompatível; revisar antes de aceitar')
    # No commits here: the inbound job and the acceptance are one atomic transaction.
    db.execute('UPDATE proposals SET accepted_id=? WHERE order_id=?',(row['id'],oid))
    db.execute("UPDATE orders SET stage='awaiting_payment',terms_evidence=? WHERE id=?",(row['id'],oid))
    event(db,oid,'aguardando_pagamento',row['id'])
    flow.audit(db,'proposal_accepted',oid,row['id'])
    return True


def queue_pix(db):
    import workflow as flow
    key = os.environ.get('PIX_KEY','').strip()
    if not key:
        return 0
    beneficiary = os.environ.get('PIX_BENEFICIARY','').strip()
    rows = db.execute("SELECT o.*,p.accepted_id FROM orders o JOIN proposals p ON p.order_id=o.id "
                      "WHERE o.stage='awaiting_payment' AND p.accepted_id IS NOT NULL").fetchall()
    with db:
        for order in rows:
            body = (f"Pedido {order['id']} — criação do site: R$ {order['cents']/100:.2f}.\n"
                    f'Chave Pix: {key}\n' + (f'Recebedor: {beneficiary}\n' if beneficiary else '')
                    + f"Se o aplicativo permitir, inclua {order['id']} na descrição. "
                    'Confira o destinatário mostrado pelo seu banco antes de confirmar. '
                    'A manutenção de R$ 50 mensais segue as condições aceitas na proposta. '
                    'Após o pagamento, envie o comprovante por aqui. A publicação depende da conferência do crédito na conta.')
            flow.enqueue(db,order['id']+':pix',order['contact'],order['accepted_id'],'pix_details',flow.text_payload(body),order['id'])
    return len(rows)


def reject(db, auth, password, code, order_id, now=None):
    import workflow as flow
    now = int(time.time()) if now is None else now
    db.execute('BEGIN IMMEDIATE')
    try:
        order = db.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
        if not order or order['stage'] != 'payment_review':
            raise ValueError('Somente pagamentos em análise podem ser rejeitados')
        approval.authenticate(db,auth,password,code,now)
        detail = {'order_id':order_id,'decision':'not_confirmed','ts':now,'cents':order['cents']}
        db.execute('INSERT INTO payment_decisions(order_id,decision,ts,signature,detail) VALUES(?,?,?,?,?)',
                   (order_id,'not_confirmed',now,approval.signed(auth,detail),json.dumps(detail)))
        db.execute("UPDATE orders SET stage='awaiting_payment' WHERE id=?",(order_id,))
        source = db.execute('SELECT id FROM receipts WHERE order_id=? ORDER BY received_at DESC,rowid DESC LIMIT 1',(order_id,)).fetchone()
        flow.enqueue(db,order_id+':not-confirmed:'+str(now),order['contact'],source['id'],'payment_not_confirmed',
            flow.text_payload('O pagamento ainda não foi confirmado na nossa conta. Por isso, o site permanece aguardando publicação. '
                              'Você pode conferir a transação no seu banco e nos enviar uma atualização por aqui.'),order_id)
        event(db,order_id,'aguardando_pagamento','rejeicao_autenticada:'+str(now))
        flow.audit(db,'owner_did_not_confirm_bank_credit',order_id)
        db.commit()
    except Exception:
        db.rollback()
        raise


def delivered(db, status):
    if status.get('status') not in ('delivered','read'):
        return
    row = db.execute("SELECT * FROM outbox WHERE remote_id=? AND purpose='final_link' AND state='sent'",
                     (status.get('id'),)).fetchone()
    if not row or status.get('recipient_id') != row['recipient']:
        return
    db.execute("UPDATE orders SET stage='delivered' WHERE id=? AND stage='published'",(row['order_id'],))
    event(db,row['order_id'],'entregue',row['remote_id'])

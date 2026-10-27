"""Aprovação local com senha + TOTP. Nunca usa mensagens do WhatsApp como autorização."""
import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from pathlib import Path
import getpass
import os
import subprocess


def restrict_file(path):
    if os.name == 'nt':
        subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r',getpass.getuser()+':(F)','SYSTEM:(F)'],
                       check=True,capture_output=True,creationflags=0x08000000)
    else:
        os.chmod(path,0o600)


def totp(secret, step):
    key = base64.b32decode(secret)
    digest = hmac.new(key, struct.pack('>Q', step), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return str((struct.unpack('>I', digest[offset:offset+4])[0] & 0x7fffffff) % 1000000).zfill(6)


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def enroll(path, password):
    path = Path(path)
    if len(password) < 14:
        raise ValueError('Use uma senha exclusiva com pelo menos 14 caracteres')
    path.parent.mkdir(parents=True, exist_ok=True)
    salt, secret = secrets.token_hex(16), base64.b32encode(secrets.token_bytes(20)).decode()
    auth = {'salt': salt, 'hash': password_hash(password, salt), 'totp': secret,
            'signing_key': secrets.token_hex(32)}
    with path.open('x', encoding='utf-8') as file:
        json.dump(auth, file)
    return secret


def authenticate(db, auth, password, code, now=None):
    now = int(time.time()) if now is None else now
    row = db.execute('SELECT * FROM admin_security WHERE id=1').fetchone()
    if row['locked_until'] > now:
        raise PermissionError('Aguarde o bloqueio temporário de autenticação')
    valid_step = next((step for step in range(now//30-1, now//30+2)
        if step > row['last_step'] and hmac.compare_digest(totp(auth['totp'], step), code)), None)
    valid = hmac.compare_digest(password_hash(password, auth['salt']), auth['hash']) and valid_step is not None
    if not valid:
        attempts = row['attempts'] + 1
        db.execute('UPDATE admin_security SET attempts=?,locked_until=? WHERE id=1',
                   (attempts, now+900 if attempts >= 5 else 0))
        db.commit()
        raise PermissionError('Senha/código inválido ou código já utilizado')
    db.execute('UPDATE admin_security SET attempts=0,locked_until=0,last_step=? WHERE id=1', (valid_step,))


def signed(auth, data):
    return hmac.new(bytes.fromhex(auth['signing_key']),
                    json.dumps(data, sort_keys=True, separators=(',', ':')).encode(), hashlib.sha256).hexdigest()

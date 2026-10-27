"""Inicialização segura da Facilitae antes das credenciais externas."""
import os
import secrets
import time
from http.server import HTTPServer

import facilitae
import inbox
import workflow as flow
from adapters import CloudWhatsApp, OpenAIClassifier
from cloudflare_pages import CloudflarePagesPublisher


def main():
    db_path = os.environ.get("FACILITAE_DB_PATH", str(inbox.DB))
    db = inbox.connect(db_path)
    flow.migrate(db)
    phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
    secret = os.environ.get("META_APP_SECRET") or secrets.token_hex(32)
    verify_token = os.environ.get("WHATSAPP_VERIFY_TOKEN") or secrets.token_hex(32)
    handler = inbox.make_handler(db_path, phone_id, secret, verify_token, connector=flow.collect)
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8088"))
    server = HTTPServer((host, port), handler)
    print("Facilitae aguardando configuração externa; /health permanece 503 até ficar pronta.", flush=True)
    try:
        import threading
        threading.Thread(target=server.serve_forever, daemon=True).start()
        classifier = OpenAIClassifier()
        sender = CloudWhatsApp()
        publisher = CloudflarePagesPublisher(db)
        while True:
            try:
                facilitae.tick(db, classifier, sender, publisher)
            except Exception:
                pass
            time.sleep(3)
    except KeyboardInterrupt:
        server.shutdown()
    finally:
        db.close()


if __name__ == "__main__":
    main()

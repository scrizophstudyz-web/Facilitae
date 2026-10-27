"""GitHub -> Pages Git Integration. Só exporta o HTML público já autorizado.

Não cria planos, domínios, repositórios ou projetos. O fluxo workflow.py é o único
chamador de create: ele verifica senha/TOTP, pagamento e hash antes de produção.
"""
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError
import urllib.request
from urllib.parse import quote, urlparse
import uuid
import zipfile
from adapters import request, env, enabled, ConfigurationError, HostingBlocked


def migrate(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS hosting_runs(
      id TEXT PRIMARY KEY, account TEXT, project TEXT, repository TEXT, branch TEXT,
      mode TEXT, sha TEXT, digest TEXT, created_at INTEGER, state TEXT, last_poll INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS hosting_alerts(
      id INTEGER PRIMARY KEY, created_at INTEGER, subject TEXT, message TEXT,
      UNIQUE(subject,message));
    CREATE TABLE IF NOT EXISTS hosting_blocks(
      order_id TEXT PRIMARY KEY, previous_stage TEXT, reason TEXT);
    ''')


def alert(db, subject, message):
    with db:
        db.execute('INSERT OR IGNORE INTO hosting_alerts(created_at,subject,message) VALUES(?,?,?)',
                   (int(time.time()), subject, message))
    print('AVISO DE HOSPEDAGEM:', subject, '—', message, flush=True)


def mappings():
    data = json.loads(Path(env('FACILITAE_HOSTING_MAP')).read_text(encoding='utf-8-sig'))
    seen = set()
    for project, cfg in data.items():
        repo = cfg.get('repository', '')
        if (not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,57}', project)
            or not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', repo)
            or repo.lower() in seen or cfg.get('dedicated') is not True):
            raise HostingBlocked('Mapa inválido: um repositório dedicado e exclusivo por projeto')
        seen.add(repo.lower())
    if len(data) > 100:
        raise HostingBlocked('Limite documentado: 100 projetos Pages por conta')
    return data


def static_html(archive):
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        # Current generator emits one file. Do not silently deploy server code,
        # credentials, extra files, functions or zip paths outside the public root.
        if z.namelist() != ['index.html']:
            raise HostingBlocked('Pacote incompatível: esperado somente index.html estático')
        if z.getinfo('index.html').file_size > 25 * 1024 * 1024:
            raise HostingBlocked('Limite Pages: arquivo maior que 25 MiB')
        return z.read('index.html').decode('utf-8')


class CloudflarePagesPublisher:
    def __init__(self, db):
        self.db = db
        migrate(db)

    def cf(self, path):
        account = env('CLOUDFLARE_ACCOUNT_ID')
        if not re.fullmatch(r'[a-fA-F0-9]{32}', account):
            raise HostingBlocked('Account ID Cloudflare inválido')
        try:
            data = request('https://api.cloudflare.com/client/v4/accounts/' + account + '/pages/projects' + path,
                           env('CLOUDFLARE_API_TOKEN'))
        except HTTPError as exc:
            if exc.code == 429:
                raise HostingBlocked('Limite de requisições Cloudflare atingido; aguarde e confira o painel') from None
            raise
        if data.get('success') is not True:
            raise HostingBlocked('Cloudflare recusou a consulta; confira limites e permissões no painel')
        return data

    def gh(self, repository, path='', data=None, method=None):
        # Repository is validated by mappings(), never taken from customer text.
        try:
            return request('https://api.github.com/repos/' + repository + path,
                           env('GITHUB_TOKEN'), data, method)
        except HTTPError as exc:
            if exc.code in (403, 429):
                raise HostingBlocked('GitHub bloqueou a operação: confira limite de API e permissões') from None
            raise

    def config(self, project):
        cfg = mappings().get(project)
        if not cfg:
            raise HostingBlocked('Projeto sem repositório dedicado configurado: ' + project)
        remote = self.cf('/' + project)['result']
        source = remote.get('source', {})
        git = source.get('config', {})
        build = remote.get('build_config', {})
        repo = cfg['repository']
        if (remote.get('name') != project or source.get('type') != 'github'
            or (git.get('owner', '') + '/' + git.get('repo_name', '')).lower() != repo.lower()
            or remote.get('production_branch') != 'main' or git.get('production_branch') != 'main'
            or git.get('deployments_enabled') is not True
            or git.get('production_deployments_enabled') is not True
            or git.get('preview_deployment_setting') != 'custom'
            or git.get('preview_branch_includes') != ['demo'] or git.get('preview_branch_excludes', [])
            or build.get('destination_dir', '').strip('/') != 'public'
            or build.get('root_dir', '') not in ('', '/')
            or build.get('build_command', '') not in ('', 'exit 0')
            or remote.get('uses_functions') is True):
            raise HostingBlocked('Pages incompatível: GitHub correto, main produção, somente demo preview, saída public, sem Functions')
        repository = self.gh(repo)
        if repository.get('private') is not True or repository.get('archived') is True:
            raise HostingBlocked('Use repositório privado, dedicado e ativo por cliente')
        return cfg, remote

    def reserve(self, project, repo, draft, digest):
        account = env('CLOUDFLARE_ACCOUNT_ID')
        start = int(datetime.now(timezone.utc).replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp())
        cap = int(os.environ.get('FACILITAE_MONTHLY_BUILD_CAP', '450'))
        if not 1 <= cap <= 500:
            raise HostingBlocked('Orçamento de builds deve ficar entre 1 e 500')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            used = self.db.execute('SELECT COUNT(*) FROM hosting_runs WHERE account=? AND created_at>=?',
                                   (account,start)).fetchone()[0]
            if used >= cap:
                raise HostingBlocked(f'Orçamento local de builds atingido: {used}/{cap}; sem upgrade automático')
            rid = 'cf-' + uuid.uuid4().hex
            self.db.execute('INSERT INTO hosting_runs(id,account,project,repository,branch,mode,digest,created_at,state) '
                            'VALUES(?,?,?,?,?,?,?,?,?)', (rid,account,project,repo,'demo' if draft else 'main',
                            'preview' if draft else 'production',digest,int(time.time()),'reserved'))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        if used+1 >= min(400,cap):
            alert(self.db, 'quota_local', f'Builds reservados neste mês: {used+1}/{cap}. Confira também consumo externo no painel.')
        return rid

    def create(self, site_id, archive, draft):
        if not enabled('FACILITAE_DEPLOY_ENABLED'):
            raise ConfigurationError('Publicação externa desativada')
        page = static_html(archive)
        cfg, _ = self.config(site_id)
        repo, branch = cfg['repository'], 'demo' if draft else 'main'
        # A separate immutable binding prevents changing the destination after an approval.
        bound = self.db.execute('SELECT repository,account FROM hosting_runs WHERE project=? LIMIT 1', (site_id,)).fetchone()
        if bound and (bound['repository'] != repo or bound['account'] != env('CLOUDFLARE_ACCOUNT_ID')):
            raise HostingBlocked('Destino já vinculado: alteração requer revisão e nova autorização')
        exists = True
        try:
            head = self.gh(repo, '/git/ref/heads/' + branch)['object']['sha']
        except HTTPError as exc:
            if exc.code != 404 or not draft:
                raise
            exists = False
            head = self.gh(repo, '/git/ref/heads/main')['object']['sha']
        old = self.gh(repo, '/git/trees/' + quote(head) + '?recursive=1')
        allowed = {'README.md','public','public/index.html','public/.gitkeep'}
        if old.get('truncated') or any(x['path'] not in allowed or x['mode'] not in ('100644','040000') for x in old['tree']):
            raise HostingBlocked('Repositório contém outros arquivos; não é o modelo estático dedicado')
        rid = self.reserve(site_id, repo, draft, hashlib.sha256(archive).hexdigest())
        try:
            # Construct a fresh tree: no local folder, SQLite, receipts, keys or scripts are uploaded.
            tree = self.gh(repo, '/git/trees', {'tree': [
                {'path':'public/index.html','mode':'100644','type':'blob','content':page},
                {'path':'README.md','mode':'100644','type':'blob','content':'Site estático gerenciado pela Facilitae.\n'}]})['sha']
            sha = self.gh(repo, '/git/commits', {'message':'Facilitae ' + ('demo' if draft else 'site aprovado'),
                                               'tree':tree,'parents':[head]})['sha']
            with self.db:
                self.db.execute("UPDATE hosting_runs SET sha=?,state='submitting' WHERE id=?", (sha,rid))
            if exists:
                self.gh(repo, '/git/refs/heads/' + branch, {'sha':sha,'force':False}, 'PATCH')
            else:
                self.gh(repo, '/git/refs', {'ref':'refs/heads/demo','sha':sha}, 'POST')
            with self.db:
                self.db.execute("UPDATE hosting_runs SET state='submitted' WHERE id=?", (rid,))
        except Exception as exc:
            with self.db:
                self.db.execute("UPDATE hosting_runs SET state='uncertain' WHERE id=?", (rid,))
            alert(self.db, rid, 'Operação Git interrompida. Conferir SHA e Cloudflare antes de qualquer repetição.')
            # After mutations begin, never report a safely retryable configuration failure.
            raise RuntimeError('Commit/publicação requer reconciliação: ' + rid) from exc
        return {'id':rid}

    def status(self, deploy_id):
        run = self.db.execute('SELECT * FROM hosting_runs WHERE id=?', (deploy_id,)).fetchone()
        if not run or run['account'] != env('CLOUDFLARE_ACCOUNT_ID'):
            raise HostingBlocked('Publicação desconhecida ou conta alterada')
        result = {'site_id':run['project'],'state':'building'}
        now = int(time.time())
        if now - run['last_poll'] < 60:
            return result
        with self.db:
            self.db.execute('UPDATE hosting_runs SET last_poll=? WHERE id=?', (now,deploy_id))
        for page in range(1,11):
            response = self.cf('/'+run['project']+'/deployments?per_page=100&page='+str(page)+'&env='+run['mode'])
            items = response['result']
            for item in items:
                meta = item.get('deployment_trigger',{}).get('metadata',{})
                if (meta.get('commit_hash') != run['sha'] or meta.get('branch') != run['branch']
                    or item.get('project_name') != run['project'] or item.get('environment') != run['mode']):
                    continue
                stage = item.get('latest_stage',{})
                if item.get('is_skipped') or stage.get('status') in ('failure','canceled','skipped'):
                    alert(self.db,deploy_id,'Build falhou/cancelado/ignorado. Confira logs e quotas no Cloudflare; sem nova tentativa automática.')
                    result['state'] = 'error'
                elif stage.get('name') == 'deploy' and stage.get('status') == 'success':
                    url = item.get('url','')
                    parsed = urlparse(url)
                    if parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith('.'+run['project']+'.pages.dev'):
                        raise HostingBlocked('URL retornada não pertence ao projeto esperado')
                    if item.get('uses_functions'):
                        raise HostingBlocked('Deploy contém Functions não previstas')
                    result.update(state='ready', deploy_ssl_url=url, ssl_url=url)
                if result['state'] in ('error','ready'):
                    with self.db:
                        self.db.execute('UPDATE hosting_runs SET state=? WHERE id=?', (result['state'],deploy_id))
                    return result
            if len(items) < 100:
                break
        if now-run['created_at'] > 1800:
            raise HostingBlocked('Build sem sucesso confirmado após 30 minutos; confira quota, Git e logs. Não reenviar commit.')
        return result

    def report(self):
        start = int(datetime.now(timezone.utc).replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp())
        return {'provider':'cloudflare_pages_git','usage_scope':'reservas locais; não é o total faturado da conta',
                'monthly_cap':os.environ.get('FACILITAE_MONTHLY_BUILD_CAP','450'),
                'monthly_reserved':self.db.execute('SELECT COUNT(*) FROM hosting_runs WHERE created_at>=?',(start,)).fetchone()[0],
                'alerts':[dict(r) for r in self.db.execute('SELECT * FROM hosting_alerts ORDER BY id DESC LIMIT 20')],
                'blocked':[dict(r) for r in self.db.execute('SELECT * FROM hosting_blocks')]}

    def verify(self, url, archive):
        """GET público sem token, TLS padrão e sem redirecionamentos; conferir conteúdo exato."""
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not re.fullmatch(r'[a-z0-9-]+\.[a-z0-9-]+\.pages\.dev',parsed.netloc):
            raise ValueError('Endereço de publicação inválido')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):
                return None
        with urllib.request.build_opener(NoRedirect).open(url,timeout=20) as response:
            if response.status != 200 or 'text/html' not in response.headers.get('Content-Type',''):
                raise ValueError('Site ainda não acessível como HTML')
            actual = response.read(25*1024*1024+1)
        if actual != static_html(archive).encode('utf-8'):
            raise ValueError('Conteúdo publicado difere da versão autorizada')
        return True

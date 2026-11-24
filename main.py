"""LDV Vistoria Inteligente | MVP multiusuário com fotos capturadas ao vivo.

Execute: ADMIN_PASSWORD='sua-senha-forte' SESSION_SECRET='string-longa-aleatoria' uvicorn main:app --host 0.0.0.0 --port 8000
"""
import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, Cookie, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv('DATA_DIR', str(ROOT/'data')))
PHOTOS = DATA/'photos'
PHOTOS.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA/'ldv_vistorias.db'
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')
SESSION_SECRET = os.getenv('SESSION_SECRET', '')
SECURE_COOKIES = os.getenv('SECURE_COOKIES', 'false').lower() == 'true'
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')
ANGLES = [
    ('frente','Frente'), ('dianteira_direita','Diagonal dianteira direita'),
    ('lateral_direita','Lateral direita'),('traseira_direita','Diagonal traseira direita'),
    ('traseira','Traseira'),('traseira_esquerda','Diagonal traseira esquerda'),
    ('lateral_esquerda','Lateral esquerda'),('dianteira_esquerda','Diagonal dianteira esquerda'),
    ('painel','Painel / hodômetro'),('interior','Interior'),
    ('pneus_dianteiros','Pneus dianteiros'),('pneus_traseiros','Pneus traseiros'),
    ('vidros','Vidros e para-brisa'),('kit_reparo','Kit de reparo'),
    ('chave','Chave do veículo'),('bancos','Bancos e revestimentos')
]
ANGLE_IDS = {x[0] for x in ANGLES}
CHECKLIST = [
    ('pneu_de','Pneu dianteiro esquerdo','Pneus'),
    ('pneu_dd','Pneu dianteiro direito','Pneus'),
    ('pneu_te','Pneu traseiro esquerdo','Pneus'),
    ('pneu_td','Pneu traseiro direito','Pneus'),
    ('parabrisa','Para-brisa','Vidros'),
    ('vidro_traseiro','Vidro traseiro','Vidros'),
    ('vidros_laterais','Vidros laterais','Vidros'),
    ('kit_reparo','Kit de reparo / compressor / selante','Acessórios'),
    ('chave_veiculo','Chave do veículo','Acessórios'),
    ('chave_roda','Chave de roda (se aplicável)','Acessórios'),
    ('banco_motorista','Banco do motorista','Bancos'),
    ('banco_passageiro','Banco do passageiro','Bancos'),
    ('banco_traseiro','Banco traseiro','Bancos'),
]
CHECK_IDS = {i[0] for i in CHECKLIST}
CHECK_STATUSES = {'ok','avaria','ausente','nao_aplicavel'}
KM_PATTERN = re.compile(r'^(0|[1-9][0-9]{0,6})$')

def normalize_km(raw):
    value = str(raw).strip()
    if not KM_PATTERN.fullmatch(value):
        raise HTTPException(422, 'Informe a quilometragem obrigatória em números, sem pontos (0 a 9999999).')
    return value

STATUSES = {'aguardando', 'em_andamento', 'pendente', 'aprovada', 'revisao_solicitada', 'reprovada'}
app = FastAPI(title='LDV Vistoria Inteligente', version='0.1.0', docs_url=None, redoc_url=None)


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def connect():
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


def init_db():
    with connect() as db:
        db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS inspections (
          id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE,
          plate TEXT NOT NULL, model TEXT NOT NULL, driver TEXT NOT NULL DEFAULT '',
          kind TEXT NOT NULL, km TEXT NOT NULL DEFAULT '',
          checklist_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL, expires_at INTEGER NOT NULL,
          submitted_at TEXT, reviewed_at TEXT, review_note TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'aguardando'
        );
        CREATE TABLE IF NOT EXISTS photos (
          id TEXT PRIMARY KEY, inspection_id TEXT NOT NULL REFERENCES inspections(id),
          angle TEXT NOT NULL, filename TEXT NOT NULL, captured_at TEXT NOT NULL,
          received_at TEXT NOT NULL, sha256 TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
          analysis_json TEXT, analysis_error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_photos_inspection ON photos(inspection_id);
        CREATE TABLE IF NOT EXISTS findings (
          id TEXT PRIMARY KEY, inspection_id TEXT NOT NULL REFERENCES inspections(id),
          photo_id TEXT REFERENCES photos(id), area TEXT NOT NULL,
          kind TEXT NOT NULL, severity TEXT NOT NULL DEFAULT 'A avaliar',
          description TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'manual',
          decision TEXT NOT NULL DEFAULT 'confirmada', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT, inspection_id TEXT NOT NULL,
          actor TEXT NOT NULL, action TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', at TEXT NOT NULL
        );
        ''')
        # Atualiza bancos criados em versões anteriores sem apagar vistorias.
        cols={x['name'] for x in db.execute('PRAGMA table_info(inspections)')}
        if 'checklist_json' not in cols:
            db.execute("ALTER TABLE inspections ADD COLUMN checklist_json TEXT NOT NULL DEFAULT '{}'")


init_db()


def sign(value: str):
    return hmac.new(SESSION_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()


def admin_auth(session: Optional[str]):
    if not SESSION_SECRET or not session:
        raise HTTPException(401, 'Acesso de equipe necessário')
    try:
        expiry, digest = session.split('.',1)
        if int(expiry) < time.time() or not hmac.compare_digest(sign(expiry),digest):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(401, 'Sessão expirada. Entre novamente.')


def rowdict(r):
    return dict(r) if r is not None else None


def log(db, inspection_id, actor, action, note=''):
    db.execute('INSERT INTO audit(inspection_id,actor,action,note,at) VALUES(?,?,?,?,?)',
               (inspection_id,actor,action,note,now()))


def driver_inspection(db, token):
    r = db.execute('SELECT * FROM inspections WHERE token=?',(token,)).fetchone()
    if not r or r['expires_at'] < time.time():
        raise HTTPException(404,'Link da vistoria inválido ou expirado')
    return r


def inspection_details(db, r, driver=False):
    obj = rowdict(r)
    if driver:
        obj.pop('token', None)
    photos = []
    for p in db.execute('SELECT * FROM photos WHERE inspection_id=? AND active=1 ORDER BY received_at', (r['id'],)):
        p = rowdict(p)
        if p['analysis_json']:
            try: p['analysis'] = json.loads(p.pop('analysis_json'))
            except json.JSONDecodeError: p['analysis']=None
        else:
            p.pop('analysis_json',None)
            p['analysis']=None
        p.pop('filename',None)
        photos.append(p)
    obj['photos']=photos
    try: obj['checklist']=json.loads(obj.pop('checklist_json') or '{}')
    except (ValueError,TypeError): obj['checklist']={}
    if not driver:
        obj['findings']=[rowdict(f) for f in db.execute('SELECT * FROM findings WHERE inspection_id=? ORDER BY created_at', (r['id'],))]
        obj['audit']=[rowdict(a) for a in db.execute('SELECT * FROM audit WHERE inspection_id=? ORDER BY id DESC',(r['id'],))]
    return obj


class Login(BaseModel):
    password: str


class NewInspection(BaseModel):
    plate: str = Field(min_length=5, max_length=10)
    model: str = Field(min_length=2, max_length=80)
    driver: str = Field(default='', max_length=100)
    kind: str = Field(default='periodica')
    km: str = Field(default='', max_length=20)


class DriverMeta(BaseModel):
    driver: str = Field(default='', max_length=100)
    km: str = Field(default='', max_length=20)


class CheckItem(BaseModel):
    status: str
    note: str = Field(default='',max_length=350)

class DriverChecklist(BaseModel):
    items: dict[str,CheckItem]

class Review(BaseModel):
    status: str
    note: str = Field(default='', max_length=700)


class Finding(BaseModel):
    photo_id: Optional[str] = None
    area: str = Field(min_length=2, max_length=100)
    kind: str = Field(min_length=2, max_length=100)
    severity: str = Field(default='A avaliar', max_length=30)
    description: str = Field(default='', max_length=800)
    source: str = 'manual'
    decision: str = 'confirmada'


@app.get('/api/config')
def get_config():
    return {'angles':[{'id':i,'name':n} for i,n in ANGLES], 'checklist':[{'id':i,'name':n,'group':g} for i,n,g in CHECKLIST], 'ai_enabled':bool(OPENAI_API_KEY), 'brand':'LDV Rent Car'}


@app.post('/api/admin/login')
def login(payload: Login):
    if not ADMIN_PASSWORD or not SESSION_SECRET:
        raise HTTPException(503,'Configure ADMIN_PASSWORD e SESSION_SECRET no servidor')
    if not hmac.compare_digest(payload.password.encode(), ADMIN_PASSWORD.encode()):
        raise HTTPException(401,'Senha incorreta')
    expiry=str(int(time.time()+12*3600))
    res=JSONResponse({'ok':True})
    res.set_cookie('ldv_session', expiry+'.'+sign(expiry), max_age=12*3600,
                   secure=SECURE_COOKIES, httponly=True, samesite='strict', path='/')
    return res


@app.get('/api/admin/me')
def me(ldv_session: Optional[str]=Cookie(default=None)):
    admin_auth(ldv_session)
    return {'ok':True}


@app.post('/api/admin/logout')
def logout(ldv_session: Optional[str]=Cookie(default=None)):
    admin_auth(ldv_session)
    res=JSONResponse({'ok':True})
    res.delete_cookie('ldv_session', path='/')
    return res


@app.get('/api/admin/inspections')
def list_inspections(ldv_session: Optional[str]=Cookie(default=None)):
    admin_auth(ldv_session)
    with connect() as db:
        rows=db.execute('''SELECT i.*, (SELECT COUNT(*) FROM photos p WHERE p.inspection_id=i.id AND p.active=1) photo_count,
          (SELECT COUNT(*) FROM findings f WHERE f.inspection_id=i.id AND f.decision='confirmada') finding_count
          FROM inspections i ORDER BY i.created_at DESC LIMIT 500''').fetchall()
        return {'inspections':[rowdict(r) for r in rows]}


@app.post('/api/admin/inspections')
def create_inspection(payload: NewInspection, ldv_session: Optional[str]=Cookie(default=None)):
    admin_auth(ldv_session)
    plate=re.sub(r'[^A-Z0-9]','',payload.plate.upper())
    if not 5<=len(plate)<=8:
        raise HTTPException(422,'Placa inválida')
    if payload.kind not in {'entrega','devolucao','periodica'}:
        raise HTTPException(422,'Tipo inválido')
    iid=str(uuid.uuid4()); token=secrets.token_urlsafe(24)
    if payload.km.strip(): normalize_km(payload.km)
    with connect() as db:
        db.execute('INSERT INTO inspections(id,token,plate,model,driver,kind,km,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)',
                   (iid,token,plate,payload.model.strip(),payload.driver.strip(),payload.kind,payload.km,now(),int(time.time()+14*86400)))
        log(db,iid,'equipe','vistoria_criada')
    return {'id':iid,'token':token,'url':'/?vistoria='+token}


@app.get('/api/admin/inspections/{iid}')
def get_admin_inspection(iid: str, ldv_session: Optional[str]=Cookie(default=None)):
    admin_auth(ldv_session)
    with connect() as db:
        r=db.execute('SELECT * FROM inspections WHERE id=?',(iid,)).fetchone()
        if not r: raise HTTPException(404,'Vistoria não encontrada')
        return inspection_details(db,r)


@app.post('/api/admin/inspections/{iid}/review')
def review_inspection(iid: str, payload: Review, ldv_session: Optional[str]=Cookie(default=None)):
    admin_auth(ldv_session)
    if payload.status not in {'aprovada','reprovada','revisao_solicitada'}:
        raise HTTPException(422,'Status inválido')
    if payload.status=='revisao_solicitada' and not payload.note.strip():
        raise HTTPException(422,'Explique ao motorista quais fotos deve refazer')
    with connect() as db:
        r=db.execute('SELECT * FROM inspections WHERE id=?',(iid,)).fetchone()
        if not r: raise HTTPException(404,'Vistoria não encontrada')
        if r['status'] not in {'pendente','revisao_solicitada'}:
            raise HTTPException(409,'A vistoria ainda não foi enviada ou já foi encerrada')
        db.execute('UPDATE inspections SET status=?,reviewed_at=?,review_note=? WHERE id=?',
                   (payload.status,now(),payload.note.strip(),iid))
        log(db,iid,'equipe',payload.status,payload.note.strip())
    return {'ok':True}


@app.post('/api/admin/inspections/{iid}/findings')
def add_finding(iid: str, payload: Finding, ldv_session: Optional[str]=Cookie(default=None)):
    admin_auth(ldv_session)
    if payload.decision not in {'confirmada','descartada'}:
        raise HTTPException(422,'Decisão inválida')
    with connect() as db:
        r=db.execute('SELECT id FROM inspections WHERE id=?',(iid,)).fetchone()
        if not r: raise HTTPException(404,'Vistoria não encontrada')
        if payload.photo_id and not db.execute('SELECT id FROM photos WHERE id=? AND inspection_id=?',(payload.photo_id,iid)).fetchone():
            raise HTTPException(422,'Foto não pertence a esta vistoria')
        fid=str(uuid.uuid4())
        db.execute('INSERT INTO findings(id,inspection_id,photo_id,area,kind,severity,description,source,decision,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (fid,iid,payload.photo_id,payload.area,payload.kind,payload.severity,payload.description,payload.source,payload.decision,now()))
        log(db,iid,'equipe','avaria_'+payload.decision,payload.kind+' – '+payload.area)
    return {'id':fid}


@app.get('/api/driver/{token}')
def get_driver_inspection(token: str):
    with connect() as db:
        r=driver_inspection(db,token)
        return inspection_details(db,r,driver=True)


@app.post('/api/driver/{token}/meta')
def update_driver_meta(token: str, payload: DriverMeta):
    if not payload.driver.strip(): raise HTTPException(422,'Nome do motorista é obrigatório')
    km = normalize_km(payload.km)
    with connect() as db:
        r=driver_inspection(db,token)
        if r['status'] not in {'aguardando','em_andamento','revisao_solicitada'}:
            raise HTTPException(409,'Vistoria já enviada')
        db.execute('UPDATE inspections SET driver=?,km=? WHERE id=?',(payload.driver.strip(),km,r['id']))
        log(db,r['id'],'motorista','quilometragem_registrada',km)
    return {'ok':True}

@app.post('/api/driver/{token}/checklist')
def update_driver_checklist(token: str, payload: DriverChecklist):
    if set(payload.items) - CHECK_IDS: raise HTTPException(422,'Item de checklist inválido')
    cleaned={}
    for cid,item in payload.items.items():
        if item.status not in CHECK_STATUSES: raise HTTPException(422,'Situação do item inválida')
        if item.status in {'avaria','ausente'} and not item.note.strip():
            raise HTTPException(422,'Descreva a avaria ou ausência do item: '+cid)
        cleaned[cid]={'status':item.status,'note':item.note.strip()}
    with connect() as db:
        r=driver_inspection(db,token)
        if r['status'] not in {'aguardando','em_andamento','revisao_solicitada'}:
            raise HTTPException(409,'Vistoria já enviada')
        db.execute('UPDATE inspections SET checklist_json=? WHERE id=?',
                   (json.dumps(cleaned,ensure_ascii=False),r['id']))
        log(db,r['id'],'motorista','checklist_salvo',str(len(cleaned))+' itens')
    return {'ok':True,'saved':len(cleaned)}


def analyze_photo(pid: str, path: Path, iid: str, angle: str, plate: str):
    if not OPENAI_API_KEY:
        return
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY, timeout=50.0)
        with connect() as db:
            prev=db.execute('''SELECT p.filename FROM photos p JOIN inspections i ON i.id=p.inspection_id
                WHERE i.plate=? AND i.id!=? AND i.status='aprovada' AND p.angle=? AND p.active=1
                ORDER BY i.reviewed_at DESC LIMIT 1''', (plate,iid,angle)).fetchone()
        contents=[{'type':'input_text','text':(
            'Você é um assistente de triagem visual de vistoria de carros de locadora. Analise a primeira foto atual do veículo, no ângulo '+angle+'. '
            'Se houver segunda imagem, é uma foto histórica aprovada; compare cuidadosamente, sem presumir que a iluminação/posição é idêntica. '
            'Liste somente avarias VISUALMENTE perceptíveis: amassados, riscos, trincas, peças soltas/quebradas. '
            'Não confunda reflexos, sujeira e sombra com avarias. Se não estiver seguro, use possivel e explique. '
            'Responda APENAS JSON válido, sem markdown: '
            '{"qualidade":"boa|ruim|parcial", "observacao_qualidade":"...", "avarias":[{"area":"...", '
            '"tipo":"risco|amassado|trinca|quebra|outro", "gravidade":"leve|moderada|alta|indeterminada", '
            '"descricao":"...", "comparacao":"nova|preexistente|indeterminada", "certeza":"alta|media|baixa"}], '
            '"resumo":"..."}. A ausência de avaria visível não prova ausência de dano. Não alegue certeza de defeitos internos.'
        )}]
        contents.append({'type':'input_image','image_url':'data:image/jpeg;base64,'+base64.b64encode(path.read_bytes()).decode(),'detail':'high'})
        if prev:
            old=PHOTOS/prev['filename']
            if old.is_file():
                contents.append({'type':'input_image','image_url':'data:image/jpeg;base64,'+base64.b64encode(old.read_bytes()).decode(),'detail':'high'})
        result=client.responses.create(model=OPENAI_MODEL, input=[{'role':'user','content':contents}], max_output_tokens=1000, store=False)
        raw=result.output_text.strip()
        try: analysis=json.loads(raw)
        except json.JSONDecodeError:
            match=re.search(r'\{[\s\S]*\}',raw)
            analysis=json.loads(match.group()) if match else {'resumo':raw[:500],'avarias':[]}
        with connect() as db:
            db.execute('UPDATE photos SET analysis_json=?,analysis_error=NULL WHERE id=?',
                       (json.dumps(analysis,ensure_ascii=False),pid))
    except Exception as e:
        with connect() as db:
            db.execute('UPDATE photos SET analysis_error=? WHERE id=?',(str(e)[:180],pid))


@app.post('/api/driver/{token}/photos')
async def upload_driver_photo(token: str, background_tasks: BackgroundTasks,
                              angle: str=Form(...), captured_at: str=Form(...),
                              image: UploadFile=File(...)):
    if angle not in ANGLE_IDS: raise HTTPException(422,'Ângulo inválido')
    with connect() as db:
        r=driver_inspection(db,token)
        if r['status'] not in {'aguardando','em_andamento','revisao_solicitada'}:
            raise HTTPException(409,'Vistoria encerrada para novas fotos')
        iid=r['id']; plate=r['plate']
    blob=await image.read(8*1024*1024+1)
    if len(blob)>8*1024*1024 or len(blob)<2000:
        raise HTTPException(413,'Imagem fora do limite de tamanho (8 MB)')
    try:
        with Image.open(io.BytesIO(blob)) as original:
            original.verify()
        with Image.open(io.BytesIO(blob)) as original:
            if original.width<480 or original.height<360:
                raise HTTPException(422,'Foto com resolução insuficiente')
            converted=original.convert('RGB')
            converted.thumbnail((1920,1920))
            result=io.BytesIO(); converted.save(result,format='JPEG',quality=82,optimize=True)
            clean=result.getvalue()
    except (UnidentifiedImageError,OSError,ValueError):
        raise HTTPException(422,'Arquivo não é uma imagem válida')
    pid=str(uuid.uuid4()); filename=pid+'.jpg'; path=PHOTOS/filename
    path.write_bytes(clean)
    digest=hashlib.sha256(clean).hexdigest()
    with connect() as db:
        db.execute('UPDATE photos SET active=0 WHERE inspection_id=? AND angle=? AND active=1',(iid,angle))
        db.execute('''INSERT INTO photos(id,inspection_id,angle,filename,captured_at,received_at,sha256)
                      VALUES(?,?,?,?,?,?,?)''',(pid,iid,angle,filename,captured_at[:60],now(),digest))
        db.execute('UPDATE inspections SET status=? WHERE id=?',('em_andamento',iid))
        log(db,iid,'motorista','foto_capturada',angle+' / '+digest[:12])
    background_tasks.add_task(analyze_photo,pid,path,iid,angle,plate)
    return {'id':pid,'angle':angle,'received_at':now(),'analysis_status':'pendente' if OPENAI_API_KEY else 'nao_configurada'}


@app.post('/api/driver/{token}/submit')
def submit_driver(token: str):
    with connect() as db:
        r=driver_inspection(db,token)
        if r['status'] not in {'aguardando','em_andamento','revisao_solicitada'}:
            raise HTTPException(409,'Vistoria já enviada')
        angles={x['angle'] for x in db.execute('SELECT angle FROM photos WHERE inspection_id=? AND active=1',(r['id'],))}
        missing=ANGLE_IDS-angles
        if missing: raise HTTPException(422,'Faltam fotos: '+', '.join(sorted(missing)))
        if not r['driver'].strip(): raise HTTPException(422,'Preencha o nome do motorista')
        normalize_km(r['km'])
        try: checklist=json.loads(r['checklist_json'] or '{}')
        except (ValueError,TypeError): checklist={}
        missing_checks=CHECK_IDS - set(checklist)
        if missing_checks: raise HTTPException(422,'Preencha e salve todos os itens da vistoria: '+', '.join(sorted(missing_checks)))
        if any(v.get('status') not in CHECK_STATUSES or
               (v.get('status') in {'avaria','ausente'} and not v.get('note','').strip())
               for v in checklist.values()):
            raise HTTPException(422,'Há itens inválidos ou avarias sem descrição no checklist')
        db.execute('UPDATE inspections SET status=?,submitted_at=? WHERE id=?',('pendente',now(),r['id']))
        log(db,r['id'],'motorista','vistoria_enviada')
    return {'ok':True}


@app.get('/api/driver/{token}/photos/{pid}')
def get_driver_photo(token: str, pid: str):
    with connect() as db:
        r=driver_inspection(db,token)
        p=db.execute('SELECT filename FROM photos WHERE id=? AND inspection_id=?',(pid,r['id'])).fetchone()
        if not p: raise HTTPException(404,'Foto não encontrada')
        return FileResponse(PHOTOS/p['filename'],media_type='image/jpeg',headers={'Cache-Control':'private, no-store'})


@app.get('/api/admin/photos/{pid}')
def get_admin_photo(pid: str, ldv_session: Optional[str]=Cookie(default=None)):
    admin_auth(ldv_session)
    with connect() as db:
        p=db.execute('SELECT filename FROM photos WHERE id=?',(pid,)).fetchone()
        if not p: raise HTTPException(404,'Foto não encontrada')
        return FileResponse(PHOTOS/p['filename'],media_type='image/jpeg',headers={'Cache-Control':'private, no-store'})


@app.get('/api/admin/export.csv')
def export_csv(ldv_session: Optional[str]=Cookie(default=None)):
    admin_auth(ldv_session)
    import csv
    output=io.StringIO(); writer=csv.writer(output,delimiter=';')
    writer.writerow(['Placa','Modelo','Motorista','Tipo','KM','Status','Criação','Envio','Validação','Avarias confirmadas','Itens com avaria ou ausência'])
    with connect() as db:
        rows=db.execute('''SELECT i.*,(SELECT COUNT(*) FROM findings f WHERE f.inspection_id=i.id AND f.decision='confirmada') n FROM inspections i ORDER BY created_at DESC''')
        for i in rows:
            writer.writerow([i['plate'],i['model'],i['driver'],i['kind'],i['km'],i['status'],i['created_at'],i['submitted_at'],i['reviewed_at'],i['n'],sum(1 for v in json.loads(i['checklist_json'] or '{}').values() if v.get('status') in ('avaria','ausente'))])
    return Response(content='\ufeff'+output.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="ldv-vistorias.csv"'})


@app.get('/api/health')
def health():
    return {'ok':True,'ai_enabled':bool(OPENAI_API_KEY)}


app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')


@app.get('/')
def home():
    return FileResponse(ROOT/'static'/'index.html')


@app.get('/demo')
def demo():
    return FileResponse(ROOT/'static'/'demo.html')

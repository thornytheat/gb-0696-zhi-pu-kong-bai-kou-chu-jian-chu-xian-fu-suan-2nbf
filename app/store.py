import sqlite3, json, hashlib
from datetime import datetime, timezone
from app.config import DATA_DIR, RULE_VERSION
from app.errors import BusinessError

def connect():
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(DATA_DIR/"runs.sqlite", timeout=15)
    db.row_factory=sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, decision TEXT NOT NULL DEFAULT '待复核', created TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS lloq_raw (id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS lloq_dossier (id TEXT PRIMARY KEY, digest TEXT NOT NULL, analyte TEXT NOT NULL, content TEXT NOT NULL, created TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS lloq_active (analyte TEXT PRIMARY KEY, dossier_id TEXT NOT NULL, switched TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS lloq_activation (seq INTEGER PRIMARY KEY AUTOINCREMENT, analyte TEXT NOT NULL, from_id TEXT, to_id TEXT NOT NULL, record TEXT NOT NULL, created TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS lloq_result (run_id TEXT NOT NULL, scope TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY (run_id,scope))")
    return db

def save(text, result):
    digest=hashlib.sha256((RULE_VERSION+"\n"+text).encode()).hexdigest()
    ident=digest[:16]
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO runs(id,digest,result,created) VALUES(?,?,?,?)",(ident,digest,json.dumps(dict(result,rule_version=RULE_VERSION),ensure_ascii=False),datetime.now(timezone.utc).isoformat()))
    return get(ident)

def get(ident):
    with connect() as db:
        row=db.execute("SELECT * FROM runs WHERE id=?",(ident,)).fetchone()
    if row is None: raise BusinessError("NOT_FOUND","批次不存在",404)
    item=dict(row);item["result"]=json.loads(item["result"]);item["rule_version"]=item["result"].get("rule_version","2026.11.18.1")
    return item

def history():
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT id,revision,decision,created FROM runs ORDER BY created DESC LIMIT 40")]

def review(ident,revision,decision):
    if not isinstance(decision,str) or decision not in {"通过","退回"}: raise BusinessError("DECISION","复核决定无效")
    current=get(ident)
    if decision=="通过" and current["result"].get("blocked"):
        raise BusinessError("BLOCKED","存在阻断项，请先修正数据",409)
    with connect() as db:
        changed=db.execute("UPDATE runs SET revision=revision+1,decision=? WHERE id=? AND revision=?",(decision,ident,revision)).rowcount
        if not changed: raise BusinessError("STALE","版本已更新，请刷新",409)
    return get(ident)

# ---- M8 定量下限档案 ----
# 原始记录与档案只插不改（INSERT OR IGNORE，无UPDATE/DELETE路径）；
# 在用版本指针仅在单事务内切换，失败整体回滚，保留上一版。

def _canonical(obj):
    return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(",",":"))

def _digest(obj):
    return hashlib.sha256(_canonical(obj).encode()).hexdigest()

def _now():
    return datetime.now(timezone.utc).isoformat()

def lloq_put_raw(record):
    digest=_digest(record);ident=digest[:16]
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO lloq_raw(id,kind,payload,created) VALUES(?,?,?,?)",(ident,record["kind"],_canonical(record),_now()))
    return ident

def lloq_get_raw(ident):
    with connect() as db:
        row=db.execute("SELECT * FROM lloq_raw WHERE id=?",(ident,)).fetchone()
    return dict(row,payload=json.loads(row["payload"])) if row else None

def lloq_raw_count():
    with connect() as db:
        return db.execute("SELECT COUNT(*) c FROM lloq_raw").fetchone()["c"]

def lloq_put_dossier(content):
    digest=_digest(content);ident=digest[:16]
    with connect() as db:
        if db.execute("SELECT 1 FROM lloq_dossier WHERE id=?",(ident,)).fetchone():
            return ident,False
        db.execute("INSERT INTO lloq_dossier(id,digest,analyte,content,created) VALUES(?,?,?,?,?)",(ident,digest,content["analyte"],_canonical(content),_now()))
    return ident,True

def lloq_get_dossier(ident):
    with connect() as db:
        row=db.execute("SELECT * FROM lloq_dossier WHERE id=?",(ident,)).fetchone()
    if row is None: raise BusinessError("NOT_FOUND","档案不存在",404)
    item=dict(row);item["content"]=json.loads(item["content"])
    return item

def lloq_verify(item):
    if _digest(item["content"])!=item["digest"]:
        raise BusinessError("TAMPER","档案内容校验失败，已拒绝切换",409)

def lloq_list_dossiers():
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT id,analyte,created FROM lloq_dossier ORDER BY created")]

def lloq_dossier_count():
    with connect() as db:
        return db.execute("SELECT COUNT(*) c FROM lloq_dossier").fetchone()["c"]

def lloq_active(analyte):
    with connect() as db:
        row=db.execute("SELECT dossier_id FROM lloq_active WHERE analyte=?",(analyte,)).fetchone()
    return row["dossier_id"] if row else None

def lloq_switch(analyte,from_id,to_id,record):
    # 单事务：先写切换记录再动指针；任一步失败整体回滚，在用版本保持上一版
    with connect() as db:
        db.execute("INSERT INTO lloq_activation(analyte,from_id,to_id,record,created) VALUES(?,?,?,?,?)",(analyte,from_id,to_id,_canonical(record),_now()))
        db.execute("INSERT INTO lloq_active(analyte,dossier_id,switched) VALUES(?,?,?) ON CONFLICT(analyte) DO UPDATE SET dossier_id=excluded.dossier_id,switched=excluded.switched",(analyte,to_id,_now()))

def lloq_activations(analyte=None):
    sql="SELECT * FROM lloq_activation"+(" WHERE analyte=?" if analyte else "")+" ORDER BY seq DESC LIMIT 20"
    with connect() as db:
        rows=db.execute(sql,(analyte,) if analyte else ()).fetchall()
    return [dict(r,record=json.loads(r["record"])) for r in rows]

def lloq_put_result(run_id,scope,payload):
    with connect() as db:
        if db.execute("SELECT 1 FROM lloq_result WHERE run_id=? AND scope=?",(run_id,scope)).fetchone():
            return False
        db.execute("INSERT INTO lloq_result(run_id,scope,payload,created) VALUES(?,?,?,?)",(run_id,scope,_canonical(payload),_now()))
    return True

def lloq_run_results(run_id):
    with connect() as db:
        return db.execute("SELECT COUNT(*) c FROM lloq_result WHERE run_id=?",(run_id,)).fetchone()["c"]

def lloq_scope(mapping):
    return _digest(mapping)[:16]

def lloq_runs():
    with connect() as db:
        return [{"id":r["id"],"result":json.loads(r["result"])} for r in db.execute("SELECT id,result FROM runs")]

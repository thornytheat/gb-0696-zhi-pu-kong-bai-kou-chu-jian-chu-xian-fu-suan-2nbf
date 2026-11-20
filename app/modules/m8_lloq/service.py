import hashlib
import json
from collections import Counter
from decimal import Decimal
from app import store
from app.errors import BusinessError
from app.report import plain
from app.modules.m8_lloq import rules

AFFECTED_CAP = 200


def _source(text):
    return hashlib.sha256(text.encode()).hexdigest()


def confirm(text, params=None):
    """确认文件→档案。内容寻址：同一确认文件重试得到同一档案，不新增。"""
    params = rules.merge_params(params)
    analyte, blanks, spikes, quarantined = rules.parse_confirmation(text)
    stats = rules.derive(blanks, spikes, params)
    src = _source(text)
    raw_blanks = store.lloq_put_raw({"kind": "blank", "analyte": analyte, "unit": "ug/L",
                                     "values": [str(v) for v in blanks], "source": src})
    raw_spikes = store.lloq_put_raw({"kind": "spike", "analyte": analyte, "unit": "ug/L",
                                     "nominal": str(stats["nominal"]),
                                     "values": [str(v) for v in spikes[stats["nominal"]]], "source": src})
    content = {"schema": "lloq/1", "analyte": analyte, "unit": "ug/L", "params": params,
                   "inputs": {"blanks": raw_blanks, "spikes": raw_spikes},
                   "nominal": str(stats["nominal"]), "stats": plain(stats),
                   "status": stats["status"], "validity": stats["validity"],
                   "warnings": stats["warnings"],
                   "basis": rules.basis_text(analyte, stats, params)}
    ident, created = store.lloq_put_dossier(content)
    return {"dossier_id": ident, "created": created, "analyte": analyte,
            "status": content["status"], "validity": content["validity"],
            "warnings": content["warnings"], "stats": content["stats"],
            "zones": rules.zones(content["stats"]), "basis": content["basis"],
            "quarantine": plain(quarantined)}


def _validate_switchable(content, force):
    stats = content["stats"]
    lod, safe, lloq = (Decimal(str(stats[k])) for k in ("lod", "lod_safe", "lloq"))
    if not lod <= safe <= lloq:
        raise BusinessError("THRESHOLD", "档案阈值次序异常，已拒绝切换", 409)
    for key in ("blanks", "spikes"):
        if store.lloq_get_raw(content["inputs"][key]) is None:
            raise BusinessError("RAW_MISSING", "档案引用的原始记录缺失，已拒绝切换", 409)
    if content["validity"] == "无效" and not force:
        raise BusinessError("INVALID", "档案无效（回收率不可信），如需切换请显式force", 409)


def _affected(prev_stats, new_stats, analyte):
    """重算所有常规批次样品在新旧两版档案下的分类，列出发生变化的样品。"""
    out, truncated = [], False
    for run in store.lloq_runs():
        for row in run["result"].get("final", []):
            if row.get("kind") != "sample" or row.get("analyte") != analyte:
                continue
            before = rules.classify(row["concentration"], prev_stats) if prev_stats else "无档案"
            after = rules.classify(row["concentration"], new_stats)
            if before != after:
                if len(out) >= AFFECTED_CAP:
                    truncated = True
                    continue
                out.append({"run_id": run["id"], "sample_id": row["sample_id"],
                            "analyte": analyte, "concentration": row["concentration"],
                            "from": before, "to": after})
    return out, truncated


def activate(dossier_id, force=False):
    """切换在用档案。校验全部前置，日志与指针同事务提交；任何失败都保留上一版。"""
    item = store.lloq_get_dossier(dossier_id)          # 不存在→404，不动指针
    store.lloq_verify(item)                            # 内容被改→409，不动指针
    content, analyte = item["content"], item["analyte"]
    _validate_switchable(content, force)               # 阈值/原始记录/有效性，不动指针
    prev_id = store.lloq_active(analyte)
    if prev_id == dossier_id:
        return {"note": "已是在用档案，未重复切换", "analyte": analyte,
                "from": prev_id, "to": dossier_id, "changes": [], "affected": []}
    prev_stats = None
    if prev_id:
        prev = store.lloq_get_dossier(prev_id)
        store.lloq_verify(prev)
        prev_stats = prev["content"]["stats"]
    changes = rules.diff_zones(prev_stats, content["stats"]) if prev_stats else []
    affected, truncated = _affected(prev_stats, content["stats"], analyte)
    record = {"analyte": analyte, "from": prev_id, "to": dossier_id,
              "status": content["status"], "zones": rules.zones(content["stats"]),
              "changes": changes, "affected": affected, "truncated": truncated,
              "basis": content["basis"], "first": prev_id is None}
    store.lloq_switch(analyte, prev_id, dossier_id, record)   # 单事务，失败回滚
    return record


def apply(run_id):
    """常规批次按在用档案给出 未检出/暂扣/检出/定量。同批次同档案重试复用同一结果。"""
    run = store.get(run_id)
    samples = [r for r in run["result"].get("final", []) if r.get("kind") == "sample"]
    analytes = sorted({r["analyte"] for r in samples})
    active = {a: store.lloq_active(a) for a in analytes}
    if not any(active.values()):
        raise BusinessError("LLOQ_NONE", "该批次分析项无在用定量下限档案", 409)
    scope = store.lloq_scope({a: d for a, d in active.items() if d})
    cached = _cached(run_id, scope)
    if cached is not None:
        return dict(cached, reused=True)
    dossiers = {a: store.lloq_get_dossier(d)["content"] for a, d in active.items() if d}
    rows = []
    for r in samples:
        a = r["analyte"]
        if not active.get(a):
            rows.append({"line": r["line"], "sample_id": r["sample_id"], "analyte": a,
                         "concentration": r["concentration"], "class": "无档案",
                         "report": "无在用档案，未判定"})
            continue
        stats = dossiers[a]["stats"]
        cls = rules.classify(r["concentration"], stats)
        rows.append({"line": r["line"], "sample_id": r["sample_id"], "analyte": a,
                     "concentration": r["concentration"], "class": cls,
                     "report": _report_text(cls, r["concentration"], stats)})
    payload = {"run_id": run_id, "scope": scope,
               "dossiers": {a: d for a, d in active.items() if d},
               "rows": rows, "counts": dict(Counter(x["class"] for x in rows))}
    store.lloq_put_result(run_id, scope, payload)
    return payload


def _cached(run_id, scope):
    with store.connect() as db:
        row = db.execute("SELECT payload FROM lloq_result WHERE run_id=? AND scope=?",
                         (run_id, scope)).fetchone()
    return json.loads(row["payload"]) if row else None


def _report_text(cls, concentration, stats):
    g = rules.g
    if cls == "定量":
        return "%s ug/L" % concentration
    if cls == "检出":
        return "检出（低于定量下限 %s ug/L）" % g(stats["lloq"])
    if cls == "暂扣":
        return "暂扣（证据不足区间 [%s, %s)，待补充确认）" % (g(stats["lod"]), g(stats["lod_safe"]))
    return "未检出（低于检出限 %s ug/L）" % g(stats["lod"])


def overview():
    dossiers = store.lloq_list_dossiers()
    analytes = {}
    for d in dossiers:
        analytes.setdefault(d["analyte"], None)
    for analyte in analytes:
        active_id = store.lloq_active(analyte)
        entry = {"active": active_id}
        if active_id:
            content = store.lloq_get_dossier(active_id)["content"]
            entry.update({"status": content["status"], "validity": content["validity"],
                          "warnings": content["warnings"], "zones": rules.zones(content["stats"]),
                          "basis": content["basis"], "stats": content["stats"]})
        analytes[analyte] = entry
    return {"analytes": analytes, "dossiers": dossiers,
            "activations": store.lloq_activations()}


def report(dossier_id):
    item = store.lloq_get_dossier(dossier_id)
    store.lloq_verify(item)
    content = item["content"]
    activation = next((a for a in store.lloq_activations(content["analyte"])
                       if a["to_id"] == dossier_id), None)
    return {"dossier": item, "zones": rules.zones(content["stats"]),
            "activation": activation}

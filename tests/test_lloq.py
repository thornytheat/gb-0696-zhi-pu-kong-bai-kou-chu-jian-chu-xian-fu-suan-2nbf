from decimal import Decimal
import pytest
from app import store
from app.errors import BusinessError
from app.pipeline import run
from app.modules.m8_lloq import rules, service


@pytest.fixture(autouse=True)
def clean_lloq():
    with store.connect() as db:
        for table in ("lloq_raw", "lloq_dossier", "lloq_active", "lloq_activation", "lloq_result"):
            db.execute("DELETE FROM " + table)
    yield

BLANKS3 = ["0.012", "0.009", "0.011", "0.010", "0.013", "0.008"]
SPIKES3 = ["0.094", "0.118", "0.087"]
BLANKS7 = ["0.010", "0.012", "0.009", "0.011", "0.010", "0.013", "0.008"]
SPIKES7 = ["0.097", "0.103", "0.099", "0.101", "0.098", "0.104", "0.096"]


def confirm_text(analyte, blanks, spikes, nominal="0.1", unit="ug/L"):
    lines = ["kind,analyte,nominal,value,unit"]
    lines += ["blank,%s,0,%s,%s" % (analyte, v, unit) for v in blanks]
    lines += ["spike,%s,%s,%s,%s" % (analyte, nominal, v, unit) for v in spikes]
    return "\n".join(lines) + "\n"


RUN_TEXT = "\n".join([
    "sample_id,kind,analyte,area,internal_area,level,dilution,unit,batch",
    "C0,standard,Xpt,100,100,0,1,ug/L,BX1",
    "C1,standard,Xpt,300,100,1,1,ug/L,BX1",
    "C2,standard,Xpt,500,100,2,1,ug/L,BX1",
    "C3,standard,Xpt,700,100,3,1,ug/L,BX1",
    "C4,standard,Xpt,900,100,4,1,ug/L,BX1",
    "C5,standard,Xpt,1100,100,5,1,ug/L,BX1",
    "B0,blank,Xpt,110,100,0,1,ug/L,BX1",
    "B1,blank,Xpt,120,100,0,1,ug/L,BX1",
    "B2,blank,Xpt,130,100,0,1,ug/L,BX1",
    "X1,sample,Xpt,128,100,0,1,ug/L,BX1",   # 浓度 0.04
    "X2,sample,Xpt,134,100,0,1,ug/L,BX1",   # 浓度 0.07
    "X3,sample,Xpt,144,100,0,1,ug/L,BX1",   # 浓度 0.12
    "X4,sample,Xpt,160,100,0,1,ug/L,BX1",   # 浓度 0.20
]) + "\n"


def derive(blanks, spikes, **over):
    params = rules.merge_params(over or None)
    return params, rules.derive([Decimal(x) for x in blanks],
                                {Decimal("0.1"): [Decimal(x) for x in spikes]}, params)


def test_derive_provisional_when_only_three_reps():
    _, s = derive(BLANKS3, SPIKES3)
    assert s["status"] == "暂定"
    assert s["lod"] < s["lod_safe"] < s["lloq"]
    assert abs(s["recovery"] - Decimal("0.9967")) < Decimal("0.001")
    assert s["rsd"] < Decimal("0.20") and not s["warnings"]


def test_derive_confirmed_when_seven_reps():
    _, s = derive(BLANKS7, SPIKES7)
    assert s["status"] == "确认"
    assert s["lod"] < s["lod_safe"] < s["lloq"]


def test_classify_boundaries():
    stats = {"lod": "0.05", "lod_safe": "0.08", "lloq": "0.15"}
    assert rules.classify("0.049", stats) == "未检出"
    assert rules.classify("0.05", stats) == "暂扣"
    assert rules.classify("0.079", stats) == "暂扣"
    assert rules.classify("0.08", stats) == "检出"
    assert rules.classify("0.149", stats) == "检出"
    assert rules.classify("0.15", stats) == "定量"


def test_diff_zones_shows_expansion():
    _, old = derive(BLANKS3, SPIKES3)
    _, new = derive(BLANKS7, SPIKES7)
    changes = rules.diff_zones(old, new)
    assert changes and all(c["direction"] == "扩大" for c in changes)
    pairs = {(c["from"], c["to"]) for c in changes}
    assert ("暂扣", "检出") in pairs and ("检出", "定量") in pairs


def test_parse_unit_conversion_and_multi_level():
    text = confirm_text("Cd", ["0.000012", "0.000009"], ["0.000094", "0.000118", "0.000087"],
                        nominal="0.0001", unit="mg/L")
    analyte, blanks, spikes, _ = rules.parse_confirmation(text)
    assert analyte == "Cd" and abs(blanks[0] - Decimal("0.012")) < Decimal("1e-9")
    text2 = confirm_text("Cd", BLANKS3[:3], SPIKES3) + "spike,Cd,0.5,0.51,ug/L\nspike,Cd,0.5,0.49,ug/L\n"
    _, _, spikes2, _ = rules.parse_confirmation(text2)
    params = rules.merge_params(None)
    stats = rules.derive([Decimal(x) for x in BLANKS3[:3]], spikes2, params)
    assert stats["nominal"] == Decimal("0.1")
    assert any("多个加标水平" in w for w in stats["warnings"])


def test_parse_rejects_multiple_analytes():
    with pytest.raises(BusinessError, match="单一分析项"):
        rules.parse_confirmation(confirm_text("Pb", BLANKS3, SPIKES3) + "blank,Cd,0,0.01,ug/L\nblank,Cd,0,0.02,ug/L\n")


def test_confirm_retry_same_file_creates_no_new_dossier():
    text = confirm_text("Ndt", BLANKS3, SPIKES3)
    before_d, before_r = store.lloq_dossier_count(), store.lloq_raw_count()
    a = service.confirm(text)
    assert a["created"] is True and a["status"] == "暂定"
    b = service.confirm(text)
    assert b["created"] is False and b["dossier_id"] == a["dossier_id"]
    assert store.lloq_dossier_count() == before_d + 1
    assert store.lloq_raw_count() == before_r + 2


def test_activate_apply_and_expansion_flow():
    v1 = service.confirm(confirm_text("Xpt", BLANKS3, SPIKES3))
    first = service.activate(v1["dossier_id"])
    assert first["first"] is True and store.lloq_active("Xpt") == v1["dossier_id"]
    again = service.activate(v1["dossier_id"])
    assert again["note"].startswith("已是") and not again["changes"]

    batch = store.save(RUN_TEXT, run(RUN_TEXT))
    got = service.apply(batch["id"])
    cls = {r["sample_id"]: r["class"] for r in got["rows"]}
    assert cls == {"X1": "未检出", "X2": "暂扣", "X3": "检出", "X4": "定量"}
    assert all(d == v1["dossier_id"] for d in got["dossiers"].values())
    assert service.apply(batch["id"])["reused"] is True
    assert store.lloq_run_results(batch["id"]) == 1

    v2 = service.confirm(confirm_text("Xpt", BLANKS7, SPIKES7))
    assert v2["status"] == "确认" and v2["dossier_id"] != v1["dossier_id"]
    record = service.activate(v2["dossier_id"])
    assert store.lloq_active("Xpt") == v2["dossier_id"]
    assert record["changes"] and all(c["direction"] == "扩大" for c in record["changes"])
    affected = {s["sample_id"]: (s["from"], s["to"]) for s in record["affected"]}
    assert affected["X1"] == ("未检出", "检出")
    assert affected["X2"] == ("暂扣", "检出")
    assert affected["X3"] == ("检出", "定量")
    assert "X4" not in affected

    got2 = service.apply(batch["id"])
    assert got2["counts"] == {"检出": 2, "定量": 2}
    assert store.lloq_run_results(batch["id"]) == 2


def test_failed_switch_keeps_previous_version():
    v1 = service.confirm(confirm_text("Kp", BLANKS3, SPIKES3))
    service.activate(v1["dossier_id"])
    with pytest.raises(BusinessError, match="档案不存在"):
        service.activate("0000000000000000")
    assert store.lloq_active("Kp") == v1["dossier_id"]

    v2 = service.confirm(confirm_text("Kp", BLANKS7, SPIKES7))
    with store.connect() as db:  # 模拟档案落库后被篡改
        db.execute("UPDATE lloq_dossier SET content=? WHERE id=?",
                   ('{"schema":"lloq/1","analyte":"Kp","tampered":true}', v2["dossier_id"]))
    with pytest.raises(BusinessError, match="校验失败"):
        service.activate(v2["dossier_id"])
    assert store.lloq_active("Kp") == v1["dossier_id"]


def test_raw_records_never_overwritten():
    text1 = confirm_text("Im", BLANKS3, SPIKES3)
    text2 = confirm_text("Im", BLANKS7, SPIKES7)
    d1 = service.confirm(text1)
    inputs = store.lloq_get_dossier(d1["dossier_id"])["content"]["inputs"]
    snapshot = {k: store.lloq_get_raw(v)["payload"] for k, v in inputs.items()}
    service.confirm(text2)
    service.confirm(text1)  # 重试与新增都不得触碰原始记录
    for k, v in inputs.items():
        assert store.lloq_get_raw(v)["payload"] == snapshot[k]


def test_invalid_dossier_requires_force():
    bad = service.confirm(confirm_text("Fc", BLANKS3, ["0.194", "0.218", "0.187"]))
    assert bad["validity"] == "无效"
    with pytest.raises(BusinessError, match="force"):
        service.activate(bad["dossier_id"])
    assert store.lloq_active("Fc") is None
    service.activate(bad["dossier_id"], force=True)
    assert store.lloq_active("Fc") == bad["dossier_id"]


def test_apply_without_dossier_reports_no_limit():
    batch = store.save(RUN_TEXT.replace("Xpt", "Nodoc"), run(RUN_TEXT.replace("Xpt", "Nodoc")))
    with pytest.raises(BusinessError, match="无在用定量下限档案"):
        service.apply(batch["id"])

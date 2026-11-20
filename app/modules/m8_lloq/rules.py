from decimal import Decimal
from app.numbers import number, fmt
from app.reader import read_rows
from app.errors import BusinessError

FIELDS = "kind analyte nominal value unit".split()
UNITS = {"ug/L": Decimal(1), "mg/L": Decimal(1000), "ng/L": Decimal("0.001")}
CLASSES = ["未检出", "暂扣", "检出", "定量"]
RANK = {c: i for i, c in enumerate(CLASSES)}

DEFAULT_PARAMS = {
    "k_lod": "3",           # LOD = 空白均值 + k_lod·sd_low
    "k_lloq": "10",         # LLOQ 空白项 = 空白均值 + k_lloq·sd_low
    "z_ref": "1.645",       # 正态单侧95%分位，t因子放大的基准
    "min_replicates": 7,    # 达到该平行数方为“确认”，否则“暂定”
    "recovery_low": "0.8",
    "recovery_high": "1.2",
    "rsd_max": "0.20",
}

# 单侧95% t分布分位（自由度1..30），>30按 1/df 插值到 z_ref
_T95 = [None, "6.3138", "2.9200", "2.3534", "2.1318", "2.0150", "1.9432", "1.8946",
        "1.8595", "1.8331", "1.8125", "1.7959", "1.7823", "1.7709", "1.7613", "1.7531",
        "1.7459", "1.7396", "1.7341", "1.7291", "1.7247", "1.7207", "1.7171", "1.7139",
        "1.7109", "1.7081", "1.7056", "1.7033", "1.7011", "1.6991", "1.6973"]


def t95(df):
    if df < 1:
        raise BusinessError("CONFIRM_N", "平行数不足")
    if df <= 30:
        return Decimal(_T95[df])
    z = Decimal(DEFAULT_PARAMS["z_ref"])
    return z + (Decimal(_T95[30]) - z) * 30 / df


def mean(xs):
    return sum(xs) / len(xs)


def stdev(xs):
    if len(xs) < 2:
        raise BusinessError("CONFIRM_N", "至少2个平行才能计算标准差")
    m = mean(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)).sqrt()


def g(x):
    return fmt(Decimal(str(x)), 6)


def merge_params(over):
    p = dict(DEFAULT_PARAMS)
    if over is None:
        return p
    if not isinstance(over, dict):
        raise BusinessError("PARAMS", "参数须为对象")
    for k, v in over.items():
        if k not in p:
            raise BusinessError("PARAMS", "未知参数 " + str(k))
        if k == "min_replicates":
            if type(v) is not int or not 2 <= v <= 30:
                raise BusinessError("PARAMS", "min_replicates须为2..30整数")
            p[k] = v
        else:
            try:
                d = Decimal(str(v))
            except Exception:
                raise BusinessError("PARAMS", "参数须为数值")
            if not d.is_finite() or d <= 0:
                raise BusinessError("PARAMS", "参数须为正且有限")
            p[k] = str(d)
    return p


def parse_confirmation(text):
    """解析方法确认CSV：kind取blank/spike，nominal为加标标示浓度，统一为ug/L。"""
    rows, bad, _blank_lines = read_rows(text, FIELDS)
    quarantined = list(bad)
    blanks, spikes, analytes = [], {}, set()
    for r in rows:
        try:
            kind = r["kind"]
            if kind not in {"blank", "spike"}:
                raise BusinessError("KIND", "类型须为blank或spike")
            if r["unit"] not in UNITS:
                raise BusinessError("UNIT", "浓度单位无效")
            factor = UNITS[r["unit"]]
            value = number(r["value"]) * factor
            if kind == "blank":
                if r["nominal"] != "" and number(r["nominal"]) != 0:
                    raise BusinessError("NOMINAL", "空白标示浓度须为0或空")
                blanks.append(value)
            else:
                nominal = number(r["nominal"]) * factor
                if nominal <= 0:
                    raise BusinessError("NOMINAL", "加标标示浓度须大于0")
                spikes.setdefault(nominal, []).append(value)
            if not r["analyte"]:
                raise BusinessError("ANALYTE", "分析项缺失")
            analytes.add(r["analyte"])
        except BusinessError as e:
            quarantined.append({"line": r.get("line", 0), "reason": e.code, "raw": r})
    if len(analytes) != 1:
        raise BusinessError("ANALYTE", "确认文件须为单一分析项")
    if len(blanks) < 2:
        raise BusinessError("CONFIRM_BLANK", "至少2个空白平行")
    if not spikes:
        raise BusinessError("CONFIRM_SPIKE", "缺少加标平行")
    return analytes.pop(), blanks, spikes, quarantined


def derive(blanks, spikes, params):
    """由空白与最低水平加标推导 LOD/暂扣上限/LLOQ，全部中间量留档可复核。"""
    nominal = min(spikes)
    vals = spikes[nominal]
    n = len(vals)
    if n < 2:
        raise BusinessError("CONFIRM_N", "最低加标水平至少2个平行")
    k_lod, k_lloq, z_ref = Decimal(params["k_lod"]), Decimal(params["k_lloq"]), Decimal(params["z_ref"])
    bm, bs = mean(blanks), stdev(blanks)
    sm, ss = mean(vals), stdev(vals)
    sd_low = max(bs, ss)
    if sd_low <= 0:
        raise BusinessError("CONFIRM_SD", "空白与加标均无离散，无法建立下限")
    t = t95(n - 1)
    inflation = t / z_ref
    lod = bm + k_lod * sd_low
    lloq_blank = bm + k_lloq * sd_low
    lloq_spike = sm + t * ss
    lloq = max(lloq_blank, lloq_spike)
    lod_safe = min(bm + k_lod * sd_low * inflation, lloq)
    recovery = sm / nominal
    rsd = ss / sm if sm > 0 else None
    warnings = []
    if not Decimal(params["recovery_low"]) <= recovery <= Decimal(params["recovery_high"]):
        warnings.append("回收率超出80–120%")
    if rsd is None or rsd > Decimal(params["rsd_max"]):
        warnings.append("加标RSD超过20%")
    if len(spikes) > 1:
        warnings.append("确认文件含多个加标水平，仅取最低水平")
    validity = "无效" if recovery < Decimal("0.5") or recovery > Decimal("1.5") else "有效"
    status = "确认" if n >= params["min_replicates"] else "暂定"
    return {
        "nominal": nominal, "n_blanks": len(blanks), "n_spikes": n,
        "blank_mean": bm, "blank_sd": bs, "spike_mean": sm, "spike_sd": ss,
        "recovery": recovery, "rsd": rsd, "sd_low": sd_low, "t": t, "inflation": inflation,
        "lod": lod, "lod_safe": lod_safe, "lloq": lloq,
        "lloq_blank": lloq_blank, "lloq_spike": lloq_spike,
        "status": status, "validity": validity, "warnings": warnings,
    }


def classify(value, stats):
    v = value if isinstance(value, Decimal) else Decimal(str(value))
    lod, safe, lloq = (Decimal(str(stats[k])) for k in ("lod", "lod_safe", "lloq"))
    if v < lod:
        return "未检出"
    if v < safe:
        return "暂扣"
    if v < lloq:
        return "检出"
    return "定量"


def zones(stats):
    lod, safe, lloq = (g(stats[k]) for k in ("lod", "lod_safe", "lloq"))
    return [
        {"range": "[0, %s)" % lod, "class": "未检出"},
        {"range": "[%s, %s)" % (lod, safe), "class": "暂扣"},
        {"range": "[%s, %s)" % (safe, lloq), "class": "检出"},
        {"range": "[%s, ∞)" % lloq, "class": "定量"},
    ]


def diff_zones(old, new):
    """两版档案的分区差异：逐区间给出 旧类→新类 与 扩大/收窄。"""
    pts = {Decimal(0)}
    for s in (old, new):
        for k in ("lod", "lod_safe", "lloq"):
            pts.add(Decimal(str(s[k])))
    pts = sorted(pts)
    raw = []
    for lo, hi in list(zip(pts, pts[1:])) + [(pts[-1], None)]:
        mid = lo + max(Decimal(1), lo) if hi is None else (lo + hi) / 2
        o, n = classify(mid, old), classify(mid, new)
        if o != n:
            if raw and raw[-1]["to"] == n and raw[-1]["from"] == o and raw[-1]["hi"] == lo:
                raw[-1]["hi"] = hi
            else:
                raw.append({"lo": lo, "hi": hi, "from": o, "to": n})
    return [{"range": "[%s, %s)" % (g(c["lo"]), "∞" if c["hi"] is None else g(c["hi"])),
             "from": c["from"], "to": c["to"],
             "direction": "扩大" if RANK[c["to"]] > RANK[c["from"]] else "收窄"} for c in raw]


def basis_text(analyte, stats, params):
    s = stats
    pct = lambda x: str((x * 100).quantize(Decimal("0.1"))) + "%"
    lines = [
        "依据（分析项 %s，单位 ug/L）：" % analyte,
        "空白 n=%d，均值 %s，SD %s；加标水平 %s，n=%d，均值 %s，SD %s，回收率 %s，RSD %s。"
        % (s["n_blanks"], g(s["blank_mean"]), g(s["blank_sd"]), g(s["nominal"]),
           s["n_spikes"], g(s["spike_mean"]), g(s["spike_sd"]), pct(s["recovery"]),
           pct(s["rsd"]) if s["rsd"] is not None else "不可算"),
        "低浓度离散取空白与加标较大者 sd_low=%s。" % g(s["sd_low"]),
        "检出限 LOD = 空白均值 + %s×sd_low = %s。" % (params["k_lod"], g(s["lod"])),
        "暂扣上限 = 空白均值 + %s×sd_low×(t95(%d)/%s) = %s（平行越少放大越多）。"
        % (params["k_lod"], s["n_spikes"] - 1, params["z_ref"], g(s["lod_safe"])),
        "定量下限 LLOQ = max(空白均值 + %s×sd_low, 加标均值 + t95(%d)×加标SD) = max(%s, %s) = %s。"
        % (params["k_lloq"], s["n_spikes"] - 1, g(s["lloq_blank"]), g(s["lloq_spike"]), g(s["lloq"])),
    ]
    if s["status"] == "暂定":
        lines.append("状态：暂定（平行数%d<%d，证据不足；落入暂扣区的样品先暂扣，待补足平行后确认，避免把噪声当检出）。"
                     % (s["n_spikes"], params["min_replicates"]))
    else:
        lines.append("状态：确认（平行数%d≥%d）。" % (s["n_spikes"], params["min_replicates"]))
    return "\n".join(lines)

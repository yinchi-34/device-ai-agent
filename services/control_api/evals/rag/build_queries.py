"""Generate ``queries.jsonl`` — the gold test set for RAG retrieval evaluation.

Run from ``services/control_api``::

    python -m evals.rag.build_queries

Each case has:

``id``              stable identifier
``category``        what failure mode the case targets (see README)
``query``           the user / agent query exactly as it would be sent
``device_model_id`` retrieval scope, as in ``search_document_chunks``
``evidence``        list of evidence groups; ALL groups are needed to answer.
                    Each group is a list of alternative strings; a chunk
                    covers the group if it contains ANY alternative after
                    loose normalisation (NFKC, CJK radical folding, casefold,
                    whitespace and '|' removed).
``context_terms``   optional terms that must appear in the SAME chunk as the
                    first evidence group for the chunk to be self-contained
                    (e.g. a register row is useless without its column header).
``distractors``     strings that must NOT be relied on (obsolete edition or
                    another model). Used for distractor@k.
``answer``          short reference answer (for later generation-level evals)
``answerable``      false for negative-rejection cases (no evidence exists)
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent / "queries.jsonl"

AHU200, AHU300, VFD, CH500 = 1, 2, 3, 4

CASES: list[dict] = []


def case(
    cid: str,
    category: str,
    query: str,
    scope: int,
    evidence: list[list[str]] | None,
    answer: str,
    *,
    context_terms: list[str] | None = None,
    distractors: list[str] | None = None,
    tags: list[str] | None = None,
    source: str = "handwritten",
) -> None:
    CASES.append(
        {
            "id": cid,
            "track": "vertical",
            "source": source,
            "category": category,
            "query": query,
            "device_model_id": scope,
            "evidence": evidence or [],
            "context_terms": context_terms or [],
            "distractors": distractors or [],
            "answer": answer,
            "answerable": bool(evidence),
            "tags": tags or [],
        }
    )


# ---------------------------------------------------------------------------
# 1. exact_id — identifiers that dense models handle poorly
# ---------------------------------------------------------------------------
case("id-01", "exact_id", "40013", AHU200, [["40013 过滤器压差"]], "40013 是过滤器压差，单位 Pa。")
case("id-02", "exact_id", "寄存器 40019 是什么", AHU200, [["40019 运行模式"]],
     "40019 为运行模式：0=自动，1=制冷，2=制热，3=通风。")
case("id-03", "exact_id", "E11 是什么报警", AHU200, [["E11 火警联动停机"]], "E11 为火警联动停机。")
case("id-04", "exact_id", "E06", AHU200, [["E06 冷水阀故障"]], "E06 冷水阀故障。")
case("id-05", "exact_id", "40016 40017 累计运行时间", AHU200, [["40016 累计运行时间"]],
     "累计运行时间占用 40016～40017，UINT32，高字在前。")
case("id-06", "exact_id", "P0.05", VFD, [["P0.05 Acceleration time"]], "P0.05 加速时间，默认 10.0 s。")
case("id-07", "exact_id", "P3.04 parity", VFD, [["P3.04 Parity"]], "P3.04 校验位，默认 1（Even）。")
case("id-08", "exact_id", "0x3002", VFD, [["0x3002 DC bus voltage"]], "0x3002 直流母线电压，0.1 V。")
case("id-09", "exact_id", "fault code OH", VFD, [["OH Heatsink"]], "OH：散热器过温。")
case("id-10", "exact_id", "CE fault", VFD, [["CE Communication error"]], "CE：通讯错误。")
case("id-11", "exact_id", "AV-1 是什么对象", CH500, [["AV-1 CHW_SP"]], "AV-1 为冷冻水出水温度设定值。")
case("id-12", "exact_id", "A201", CH500, [["A201 冷冻水断流"]], "A201 冷冻水断流。")
case("id-13", "exact_id", "MSV-1", CH500, [["MSV-1 UNIT_MODE"]], "MSV-1 运行模式：1=制冷，2=制热，3=待机。")
case("id-14", "exact_id", "0x8000 register", VFD, [["0x8000 Current fault code"]], "0x8000 当前故障代码。")
case("id-15", "exact_id", "P1.03", VFD, [["P1.03 Motor rated speed"]], "P1.03 电机额定转速，默认 1460 rpm。")

# ---------------------------------------------------------------------------
# 2. table_lookup — a value in a table row; row alone lacks column meaning
# ---------------------------------------------------------------------------
case("tbl-01", "table_lookup", "AHU-200 送风温度寄存器的缩放系数是多少", AHU200,
     [["40002 送风温度 INT16 ℃ 0.1"]], "40002 送风温度缩放系数 0.1。", context_terms=["缩放系数"])
case("tbl-02", "table_lookup", "回风湿度的单位和数据类型", AHU200,
     [["40006 回风湿度 UINT16 %RH"]], "40006，UINT16，%RH。", context_terms=["数据类型", "单位"])
case("tbl-03", "table_lookup", "新风阀开度可以写吗", AHU200,
     [["40012 新风阀开度 UINT16 % 1 R/W"]], "可以，40012 为 R/W。", context_terms=["读写"])
case("tbl-04", "table_lookup", "启停命令寄存器怎么写入，写什么值启动", AHU200,
     [["40018 启停命令"]], "向 40018 写 1 启动，写 0 停止。", context_terms=["读写"])
case("tbl-05", "table_lookup", "送风温度设定值允许的范围", AHU200,
     [["范围 12.0~30.0"]], "12.0～30.0 ℃。")
case("tbl-06", "table_lookup", "What is the default deceleration time of VFD-750?", VFD,
     [["P0.06 Deceleration time 0.1-3600.0 10.0"]], "默认 10.0 s。", context_terms=["Default"])
case("tbl-07", "table_lookup", "default Modbus baud rate setting of the drive", VFD,
     [["P3.03 Baud rate"]], "P3.03 默认 1，即 9600。", context_terms=["Default"])
case("tbl-08", "table_lookup", "scaling of output current register", VFD,
     [["0x3001 Output current R 0.1 A"]], "0x3001，0.1 A。", context_terms=["Scaling"])
case("tbl-09", "table_lookup", "what value in the control word resets a fault", VFD,
     [["7 = fault reset"]], "写 7 复位故障。")
case("tbl-10", "table_lookup", "冷冻水出水温度设定值默认多少度", CH500,
     [["默认 7.0"]], "AV-1 默认 7.0 ℃，范围 5.0～15.0。")
case("tbl-11", "table_lookup", "压缩机运行状态是哪个 BACnet 对象", CH500,
     [["BI-1 COMP_STATUS"]], "BI-1。")
case("tbl-12", "table_lookup", "AHU-200 额定风量", AHU200, [["额定风量 20000"]], "20000 m³/h。",
     distractors=["额定风量 30000"])
case("tbl-13", "table_lookup", "AHU-200 整机多重", AHU200, [["整机重量 2350"]], "2350 kg。")
case("tbl-14", "table_lookup", "rated output current of VFD-750", VFD,
     [["Rated output current 17 A"]], "17 A。")
case("tbl-15", "table_lookup", "当前报警代码寄存器的数值和报警代码怎么对应", AHU200,
     [["1~12 对应 E01~E12"]], "40015：0 无报警，1～12 对应 E01～E12。")

# ---------------------------------------------------------------------------
# 3. numeric_unit — thresholds, units and numeric conditions
# ---------------------------------------------------------------------------
case("num-01", "numeric_unit", "送风温度高于设定值多少度会报警", AHU200,
     [["高于设定值 8 ℃ 且持续 10 分钟"]], "高于设定值 8 ℃ 持续 10 分钟触发 E07。",
     distractors=["高于设定值 6 ℃"])
case("num-02", "numeric_unit", "防冻保护温度默认值", AHU200, [["默认 5.0 ℃"]], "5.0 ℃（40021）。")
case("num-03", "numeric_unit", "维护前断电后要等多久", AHU200, [["等待至少 5 分钟"]], "至少 5 分钟。")
case("num-04", "numeric_unit", "皮带挠度应该是多少毫米", AHU200, [["10~15 mm"]], "10～15 mm。")
case("num-05", "numeric_unit", "轴承多久加一次润滑脂", AHU200, [["2000 小时"]], "每运行 2000 小时。")
case("num-06", "numeric_unit", "At what heatsink temperature does the OH fault trip?", VFD,
     [["above 95 °C"]], "95 °C 以上。")
case("num-07", "numeric_unit", "undervoltage threshold of the DC bus supply", VFD,
     [["below 320 V"]], "供电低于 320 V。")
case("num-08", "numeric_unit", "how much overload can the drive handle and for how long", VFD,
     [["150% rated current for 60 s"]], "150% 额定电流 60 s。")
case("num-09", "numeric_unit", "排气压力超过多少触发高压保护", CH500, [["高于 2.8 MPa"]], "2.8 MPa。")
case("num-10", "numeric_unit", "冷冻水断流报警的延时", CH500, [["断开超过 5 s"]], "水流开关断开超过 5 s。")
case("num-11", "numeric_unit", "derating above 40 degrees ambient", VFD,
     [["derate the output current by 1.5% per °C"]], "每 °C 降额 1.5%，最高 50 °C。")

# ---------------------------------------------------------------------------
# 4. paraphrase — colloquial / symptom wording, little lexical overlap
# ---------------------------------------------------------------------------
case("para-01", "paraphrase", "房间越来越热，出风口吹出来的风不凉了怎么办", AHU200,
     [["7.2 送风温度持续升高", "房间越来越热，送风温度不断上升"]],
     "按 7.2 节：读 40002/40007，读冷水阀开度 40010，检查冷冻水供水温度和盘管。")
case("para-02", "paraphrase", "出风口的风特别小", AHU200,
     [["出风口风很小"]], "按 7.1 节：查过滤器压差 40013、风机频率 40008/40009、皮带。")
case("para-03", "paraphrase", "机器老是自己停了又开", AHU200,
     [["每隔几分钟自动停机又启动"]], "按 7.3 节：检查 BMS 是否反复写 40018 及防冻保护 E05。")
case("para-04", "paraphrase", "上位机看不到这台空调箱了", AHU200,
     [["上位机显示设备离线"]], "按 7.4 节：查 A/B 线、120 Ω 终端电阻、地址与波特率。")
case("para-05", "paraphrase", "什么时候需要换滤网", AHU200,
     [["初效过滤器每 3 个月检查一次"]], "每 3 个月检查，压差超阈值立即更换。")
case("para-06", "paraphrase", "能不能在风机转的时候开门检查", AHU200,
     [["严禁在风机运行时打开检修门"]], "不能，严禁运行时开检修门。")
case("para-07", "paraphrase", "the drive trips when the motor speeds up", VFD,
     [["OC1 Overcurrent during"]], "OC1：增大加速时间 P0.05。")
case("para-08", "paraphrase", "drive trips while slowing down a heavy fan", VFD,
     [["OV2 Overvoltage during"], ["OC2 Overcurrent during"]],
     "OV2/OC2：增大 P0.06 或加制动电阻。")
case("para-09", "paraphrase", "how much space to leave around the inverter in the cabinet", VFD,
     [["minimum clearance of 100 mm above and below"]], "上下 100 mm，两侧 50 mm。")
case("para-10", "paraphrase", "can the drive come back on by itself after tripping", VFD,
     [["will not restart automatically unless P5.01"]], "默认不会，除非 P5.01=1。")
case("para-11", "paraphrase", "冷水机组压缩机出口压力太高", CH500,
     [["A101 高压保护"]], "A101 高压保护：检查冷却水流量与冷却塔风机。")
case("para-12", "paraphrase", "制冷剂可能漏了会报什么", CH500,
     [["A102 低压保护"]], "A102 低压保护。")
case("para-13", "paraphrase", "怎么让机组只送风不制冷", AHU200,
     [["3=通风"]], "40019 写 3（通风模式）。")
case("para-14", "paraphrase", "风机皮带断了会有什么提示", AHU200,
     [["E10 风机皮带断裂"]], "E10 风机皮带断裂。")
case("para-15", "paraphrase", "传感器没接好的时候温度读数是多少", AHU200,
     [["-3276.8"]], "送风温度读数为 -3276.8。")

# ---------------------------------------------------------------------------
# 5. cross_lingual — query language differs from the manual text
# ---------------------------------------------------------------------------
case("xl-01", "cross_lingual", "VFD-750 的最大频率参数默认值", VFD,
     [["P0.03 Maximum frequency"]], "P0.03，默认 50.00 Hz。", context_terms=["Default"])
case("xl-02", "cross_lingual", "变频器散热器温度在哪个寄存器", VFD,
     [["0x3003 Heatsink temperature"]], "0x3003，单位 1 °C。")
case("xl-03", "cross_lingual", "变频器通讯超时时间怎么设置", VFD,
     [["P3.05 Communication timeout"]], "P3.05，默认 2.0 s，0 为关闭。")
case("xl-04", "cross_lingual", "制动电阻接在哪两个端子", VFD,
     [["between terminals PB and (+)"]], "PB 与 (+) 之间。", tags=["ocr"])
case("xl-05", "cross_lingual", "What is the chilled water leaving temperature object?", CH500,
     [["AI-1 CHW_LWT"]], "AI-1 CHW_LWT。")
case("xl-06", "cross_lingual", "BACnet UDP port of the chiller", CH500,
     [["UDP 端口 47808"]], "47808（0xBAC0）。")
case("xl-07", "cross_lingual", "What is the default slave address of the AHU-200?", AHU200,
     [["默认从站地址为 1"]], "1。", distractors=["默认从站地址为 2"])
case("xl-08", "cross_lingual", "AHU-200 supported Modbus function codes", AHU200,
     [["支持功能码 0x03"]], "0x03、0x06、0x10，不支持 0x04。")
case("xl-09", "cross_lingual", "冷水机组模拟量 COV 默认变化量", CH500,
     [["COV increment for analog inputs defaults to 0.5", "模拟量 COV 变化量默认 0.5"]], "0.5 ℃。")
case("xl-10", "cross_lingual", "电机额定电流参数", VFD,
     [["P1.02 Motor rated current"]], "P1.02，默认 15.4 A。")

# ---------------------------------------------------------------------------
# 6. multi_hop — needs two or more separate pieces of evidence
# ---------------------------------------------------------------------------
case("mh-01", "multi_hop", "出现 E07 报警时应读取哪些寄存器，它们的缩放系数分别是多少", AHU200,
     [["E07 送风温度过高"], ["40002 送风温度 INT16"], ["40010 冷水阀开度 UINT16"]],
     "E07 要求读 40002（0.1）和 40010（0.1）。")
case("mh-02", "multi_hop", "E04 报警的阈值保存在哪个寄存器，它能不能修改", AHU200,
     [["E04 过滤器堵塞"], ["40020 过滤器压差报警阈值 UINT16 Pa 1 R/W"]],
     "阈值在 40020，R/W 可改，默认 250 Pa。")
case("mh-03", "multi_hop", "E05 防冻保护比较的是哪个寄存器的温度设定，单位是什么", AHU200,
     [["E05 防冻保护"], ["40021 防冻保护温度 INT16 ℃"]], "40021，℃，缩放 0.1。")
case("mh-04", "multi_hop", "CE fault: which parameters should I check and what are their defaults?", VFD,
     [["CE Communication error"], ["P3.03 Baud rate"], ["P3.04 Parity"]],
     "检查 P3.03（默认 9600）、P3.04（默认 Even）及 P3.05。")
case("mh-05", "multi_hop", "OL fault depends on which parameter and what is its default value", VFD,
     [["OL Motor overload"], ["P1.02 Motor rated current"]], "P1.02，默认 15.4 A。")
case("mh-06", "multi_hop", "A301 防冻报警对应的对象是什么，设定值对象的范围是多少", CH500,
     [["A301 冷冻水防冻"], ["AV-1 CHW_SP"]], "AI-1；AV-1 范围 5.0～15.0 ℃。")
case("mh-07", "multi_hop", "通讯超时报警后，应该核对哪些通讯参数", AHU200,
     [["E09 通讯超时"], ["默认从站地址为 1，波特率 9600"]], "核对地址 1、9600、偶校验、终端电阻。")
case("mh-08", "multi_hop", "风量不足时要对比的两个频率寄存器的缩放系数", AHU200,
     [["40008 风机频率 UINT16 Hz 0.01"], ["40009 风机频率给定 UINT16 Hz 0.01"]],
     "40008 与 40009 都是 0.01（固件 V2.3+）。", distractors=["40008 风机频率 UINT16 Hz 0.1 R"])
case("mh-09", "multi_hop", "To stop the drive via Modbus, which register and value, and what must P0.02 be?", VFD,
     [["5 = stop"], ["P0.02 Run command source"]], "写 0x2000=5；P0.02 需为 2（Modbus）。")
case("mh-10", "multi_hop", "写 BO-1 启动机组失败返回 WRITE_ACCESS_DENIED 是为什么，模式对象怎么设", CH500,
     [["WRITE_ACCESS_DENIED"], ["MSV-1 UNIT_MODE"]], "MSV-1 处于待机(3)；改为 1/2 后再写 BO-1。")

# ---------------------------------------------------------------------------
# 7. long_context — the fact sits in the middle of a long page / section
# ---------------------------------------------------------------------------
case("long-01", "long_context", "minimum clearance above the drive", VFD,
     [["minimum clearance of 100 mm above and below"]], "100 mm。")
case("long-02", "long_context", "side clearance between drives", VFD,
     [["50 mm on each side"]], "每侧 50 mm。")
case("long-03", "long_context", "can drives be stacked vertically in one cabinet", VFD,
     [["side by side rather than stacked"]], "不建议堆叠，应并排安装。")
case("long-04", "long_context", "altitude derating of the drive", VFD,
     [["derate the output current by 1% per 100 m"]], "1000 m 以上每 100 m 降额 1%。")
case("long-05", "long_context", "earth conductor cross-section", VFD,
     [["at least 4 mm²"]], "至少 4 mm²。")
case("long-06", "long_context", "motor cable to signal cable separation distance", VFD,
     [["at least 30 cm"]], "至少 30 cm。")

# ---------------------------------------------------------------------------
# 8. ocr_scanned — evidence only exists on the image-only (scanned) page
# ---------------------------------------------------------------------------
case("ocr-01", "ocr_scanned", "recommended braking resistor for VFD-750", VFD,
     [["VFD-750 | 60 ohm | 75 ohm | 1000 W"]], "推荐 75 Ω，1000 W（最小 60 Ω）。", tags=["ocr"])
case("ocr-02", "ocr_scanned", "VFD-750 制动电阻最小阻值", VFD,
     [["VFD-750 | 60 ohm"]], "60 Ω。", tags=["ocr"])
case("ocr-03", "ocr_scanned", "braking resistor power rating for the 5.5 kW model VFD-550", VFD,
     [["VFD-550 | 80 ohm | 100 ohm | 800 W"]], "100 Ω / 800 W。", tags=["ocr"])
case("ocr-04", "ocr_scanned", "braking resistor selection table", VFD,
     [["Braking resistor selection"]], "见附录 B。", tags=["ocr"])
case("ocr-05", "ocr_scanned", "VFD-1100 recommended resistor", VFD,
     [["VFD-1100 | 40 ohm | 50 ohm | 1500 W"]], "50 Ω / 1500 W。", tags=["ocr"])

# ---------------------------------------------------------------------------
# 9. version_conflict — same scope holds an obsolete edition with other values
# ---------------------------------------------------------------------------
case("ver-01", "version_conflict", "风机频率 40008 的缩放系数", AHU200,
     [["40008 风机频率 UINT16 Hz 0.01"]], "0.01（固件 V2.3 起）。",
     distractors=["40008 风机频率 UINT16 Hz 0.1 R"], context_terms=["缩放系数"])
case("ver-02", "version_conflict", "过滤器压差报警阈值出厂默认是多少", AHU200,
     [["默认 250 Pa", "由 300 Pa 调整为 250 Pa"]], "250 Pa（V2.3 起）。",
     distractors=["默认 300 Pa"])
case("ver-03", "version_conflict", "手册适用于哪个固件版本", AHU200,
     [["固件 V2.3 及以上"]], "V2.3 及以上。", distractors=["适用于固件 V2.1"])
case("ver-04", "version_conflict", "E04 过滤器堵塞的触发条件", AHU200,
     [["40020，默认 250 Pa"]], "压差超过 40020 阈值（默认 250 Pa）。",
     distractors=["40020，默认 300 Pa"])
case("ver-05", "version_conflict", "固件升级后哪些参数变了", AHU200,
     [["缩放系数由 0.1 改为 0.01"]], "40008 缩放 0.1→0.01；阈值 300→250 Pa。")

# ---------------------------------------------------------------------------
# 10. scope_distractor — a sibling model has near-identical text
#     (evaluated scoped; the runner also reports an unscoped diagnostic)
# ---------------------------------------------------------------------------
case("scope-01", "scope_distractor", "送风温度寄存器地址", AHU200,
     [["40002 送风温度"]], "40002。", distractors=["40102 送风温度"])
case("scope-02", "scope_distractor", "默认波特率", AHU200,
     [["波特率 9600 bps"]], "9600 bps。", distractors=["波特率 19200 bps"])
case("scope-03", "scope_distractor", "校验方式和停止位", AHU200,
     [["偶校验，1 位停止位"]], "偶校验，1 位停止位。", distractors=["无校验，2 位停止位"])
case("scope-04", "scope_distractor", "冷水阀开度寄存器", AHU200,
     [["40010 冷水阀开度"]], "40010。", distractors=["40110 冷水阀开度"])
case("scope-05", "scope_distractor", "送风机功率", AHU200, [["送风机功率 15 kW"]], "15 kW。",
     distractors=["送风机功率 22 kW"])
case("scope-06", "scope_distractor", "AHU-300 默认从站地址", AHU300,
     [["默认从站地址为 2"]], "2。", distractors=["默认从站地址为 1"])
case("scope-07", "scope_distractor", "AHU-300 E07 报警条件", AHU300,
     [["高于设定值 6 ℃ 且持续 15 分钟"]], "高于设定值 6 ℃ 持续 15 分钟。",
     distractors=["高于设定值 8 ℃"])
case("scope-08", "scope_distractor", "AHU-300 过滤器报警阈值默认值", AHU300,
     [["默认 350 Pa"]], "350 Pa。", distractors=["默认 250 Pa", "默认 300 Pa"])

# ---------------------------------------------------------------------------
# 11. unanswerable — nothing in scope answers; retrieval should look weak
# ---------------------------------------------------------------------------
for cid, query, scope in [
    ("neg-01", "AHU-200 的保修期是几年", AHU200),
    ("neg-02", "AHU-200 支持 BACnet MS/TP 吗", AHU200),
    ("neg-03", "AHU-200 加湿器的蒸汽量是多少", AHU200),
    ("neg-04", "How do I wire the STO safe torque off input on VFD-750?", VFD),
    ("neg-05", "VFD-750 PID setpoint parameter", VFD),
    ("neg-06", "What is the EtherCAT node address of the drive?", VFD),
    ("neg-07", "CH-500 制冷剂充注量是多少公斤", CH500),
    ("neg-08", "CH-500 支持 Modbus TCP 吗，寄存器 40001 是什么", CH500),
    ("neg-09", "AHU-200 的电加热功率", AHU200),
    ("neg-10", "VFD-750 的 IP 防护等级能否达到 IP55", VFD),
    ("neg-11", "AHU-300 的故障排查步骤", AHU300),
    ("neg-12", "CH-500 cooling tower fan VFD speed register", CH500),
]:
    case(cid, "unanswerable", query, scope, None, "手册中没有相关信息，应明确说明无法回答。")

# ---------------------------------------------------------------------------
# 12. injection — queries whose natural neighbours include an injected passage
# ---------------------------------------------------------------------------
case("inj-01", "injection", "How do I restart the VFD-750 after a fault?", VFD,
     [["To reset a fault, first remove the cause"]], "先排除故障再按 STOP/RESET 或写 7 到 0x2000。")
case("inj-02", "injection", "VFD-750 fault reset via Modbus", VFD,
     [["write 7 to the control word 0x2000"]], "写 7 到 0x2000。")
case("inj-03", "injection", "register 0x2000 values", VFD,
     [["0x2000 Control word"]], "1 正转，2 反转，5 停止，7 复位。")
case("inj-04", "injection", "变频器故障后如何自动重启", VFD,
     [["P5.01"]], "需设置 P5.01=1；默认不自动重启。")


# ---------------------------------------------------------------------------
# 13. template — CRAG-style templated questions generated from the structured
#     tables in corpus_spec (CRAG built its KG questions from ~600 templates).
#     They add statistical power for easy/medium strata cheaply; hard cases stay
#     handwritten. A random sample must be human-verified (see annotation.py).
# ---------------------------------------------------------------------------


def generate_template_cases() -> None:
    from evals.rag import corpus_spec as s

    regs = s.ahu200_registers("0.01")
    for addr, name, dtype, unit, scale, access, _note in regs:
        row = f"{addr} {name} {dtype}"
        case(f"tpl-ahu-reg-{addr}-name", "exact_id", f"AHU-200 寄存器 {addr} 是什么", AHU200,
             [[f"{addr} {name}"]], f"{addr} 为{name}。", source="template")
        case(f"tpl-ahu-reg-{addr}-addr", "table_lookup", f"{name}的寄存器地址是多少", AHU200,
             [[f"{addr} {name}"]], f"{addr}。", source="template",
             distractors=[f"{int(addr) + 100} {name}"])
        dist = ["40008 风机频率 UINT16 Hz 0.1 R"] if addr == "40008" else []
        case(f"tpl-ahu-reg-{addr}-scale", "table_lookup", f"{name}的单位和缩放系数", AHU200,
             [[f"{row} {unit} {scale}"]], f"单位 {unit}，缩放系数 {scale}。", source="template",
             context_terms=["缩放系数"], distractors=dist)
    for code, name, cond, action in s.ahu200_alarms("250"):
        case(f"tpl-ahu-alarm-{code}-name", "exact_id", f"AHU-200 报警 {code} 是什么意思", AHU200,
             [[f"{code} {name}"]], f"{code}：{name}。", source="template")
        case(f"tpl-ahu-alarm-{code}-fix", "table_lookup", f"出现{name}报警应该怎么处理", AHU200,
             [[f"{code} {name}"]], action, source="template")
    for code, name, rng, default, unit in s.VFD_PARAMS:
        case(f"tpl-vfd-param-{code}", "exact_id", f"VFD-750 parameter {code}", VFD,
             [[f"{code} {name}"]], f"{code} {name}, default {default} {unit}.", source="template")
        case(f"tpl-vfd-param-{code}-default", "table_lookup", f"What is the default {name.lower()} of the VFD-750?",
             VFD, [[f"{code} {name}"]], f"{default} {unit}", source="template", context_terms=["Default"])
        case(f"tpl-vfd-param-{code}-zh", "cross_lingual", f"变频器参数 {code} 的默认值是多少", VFD,
             [[f"{code} {name}"]], f"默认 {default} {unit}。", source="template")
    for addr, name, access, scaling in s.VFD_REGISTERS:
        case(f"tpl-vfd-reg-{addr}", "table_lookup", f"Which register holds the {name.lower()}?", VFD,
             [[f"{addr} {name}"]], f"{addr}，{scaling}。", source="template")
    for code, desc, cause, remedy in s.VFD_FAULTS:
        case(f"tpl-vfd-fault-{code}", "exact_id", f"VFD-750 fault {code} meaning", VFD,
             [[f"{code} {desc}"]], f"{desc}. {remedy}.", source="template")
        case(f"tpl-vfd-fault-{code}-zh", "cross_lingual", f"变频器报 {code} 故障的原因和处理方法", VFD,
             [[f"{code} {desc}"]], f"{cause}；{remedy}。", source="template")
    for obj, oname, zh, unit, _note in s.CH500_OBJECTS:
        case(f"tpl-ch-obj-{obj}", "exact_id", f"CH-500 BACnet 对象 {obj} 是什么", CH500,
             [[f"{obj} {oname} {zh}"]], f"{obj} {zh}（{oname}）。", source="template")
        case(f"tpl-ch-obj-{obj}-zh", "table_lookup", f"{zh}对应哪个 BACnet 对象", CH500,
             [[f"{obj} {oname} {zh}"]], f"{obj}。", source="template")
    for code, name, cond, action in s.CH500_ALARMS:
        case(f"tpl-ch-alarm-{code}", "exact_id", f"冷水机组报警 {code}", CH500,
             [[f"{code} {name}"]], f"{code} {name}：{cond}。", source="template")
    for addr, name, *_rest in s.AHU300_REGISTERS:
        case(f"tpl-ahu300-reg-{addr}", "table_lookup", f"AHU-300 {name}寄存器地址", AHU300,
             [[f"{addr} {name}"]], f"{addr}。", source="template",
             distractors=[f"{int(addr) - 100} {name}"])


def main() -> None:
    generate_template_cases()
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)), "duplicate case id"
    with OUT.open("w", encoding="utf-8") as fh:
        for c in CASES:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"{len(CASES)} cases written to {OUT}")


if __name__ == "__main__":
    main()

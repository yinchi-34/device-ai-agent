"""Regenerate the retrieval evaluation manuals.

The generated PDF/DOCX files are committed, so this script only needs to run
when the manual text below changes. It needs two packages that are not
project dependencies:

    uv run --with reportlab --with python-docx \
        python evals/retrieval/build_fixtures.py

After regenerating, run the dataset validity test
(tests/document/test_retrieval_evaluation.py) to make sure every labelled
evidence snippet still appears in the parsed text.
"""

from datetime import UTC, datetime
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

AHU_200_PAGES = [
    (
        "第 1 章 概述与安全须知",
        [
            ("AHU-200 组合式空调机组适用于商业楼宇和洁净车间的集中送风，"
            "由新回风混合段、过滤段、表冷段、送风机段和消声段组成。"),
            ("安全须知：任何维护作业前必须断开机组主电源并上锁挂牌，"
            "确认风机完全停止转动后方可打开检修门。"),
            "机组运行时严禁打开风机段检修门。电气作业须由持证电工完成。",
            "本手册适用于控制器固件 V2.3 及以上版本。",
        ],
    ),
    (
        "第 2 章 技术参数",
        [
            "额定风量 20000 m³/h，机外余压 450 Pa。",
            "额定制冷量 120 kW，额定制热量 90 kW。",
            "电源 380V/3N/50Hz，送风机电机功率 15 kW，防护等级 IP55。",
            "过滤等级：初效 G4 板式过滤器，中效 F7 袋式过滤器。",
            "表冷器设计进水温度 7℃，出水温度 12℃，水侧压降不大于 60 kPa。",
            "机组外形尺寸 3800 × 1900 × 2100 mm，运行重量约 2600 kg。",
        ],
    ),
    (
        "第 3 章 安装",
        [
            ("机组应安装在平整的混凝土基础上，基础高出地面不少于 150 mm，"
            "以便安装冷凝水存水弯。"),
            "冷凝水排水管须设置水封，水封高度不小于机外余压对应水柱高度加 50 mm。",
            "风管与机组之间应采用软接头连接，避免振动传递。",
            "冷冻水进出水管应设置截止阀、过滤器和温度计，便于检修。",
        ],
    ),
    (
        "第 4 章 电气接线",
        [
            "主电源进线接至电控柜端子 L1、L2、L3、N、PE，进线截面不小于 10 mm²。",
            "送风机由变频器驱动，变频器故障输出接至控制器数字输入 DI3。",
            "急停按钮串接在控制回路中，按下后机组立即停机且不可远程复位。",
            "RS-485 通讯线采用屏蔽双绞线，屏蔽层单端接地，总线末端接 120 Ω 终端电阻。",
        ],
    ),
    (
        "第 5 章 Modbus 通讯设置",
        [
            "控制器支持 Modbus RTU 和 Modbus TCP 两种方式。",
            ("RTU 默认参数：波特率 9600，8 位数据位，无校验，1 位停止位，"
            "默认从站地址为 1。"),
            "TCP 默认端口 502，默认 IP 地址 192.168.1.200。",
            ("主站轮询间隔建议不小于 1 秒。超过 30 秒未收到主站轮询时控制器触发 "
            "A12 通讯超时报警。"),
            "浮点数采用 IEEE 754 格式，高字在前（ABCD 字节序）。",
        ],
    ),
    (
        "第 6 章 Modbus 寄存器表",
        [
            "地址 名称 数据类型 单位 读写",
            "30001 送风温度 float32 0.1℃ 只读",
            "30003 回风温度 float32 0.1℃ 只读",
            "30005 新风温度 float32 0.1℃ 只读",
            "30007 送风相对湿度 float32 %RH 只读",
            "30009 冷水阀开度反馈 float32 % 只读",
            "30011 过滤器压差 float32 Pa 只读",
            "30013 送风静压 float32 Pa 只读",
            "30015 送风机频率反馈 float32 0.1Hz 只读",
            "30017 新风阀开度反馈 float32 % 只读",
            "30019 回风阀开度反馈 float32 % 只读",
            "30021 热水阀开度反馈 float32 % 只读",
            "30023 表冷器出风温度 float32 0.1℃ 只读",
            "30025 送风二氧化碳浓度 float32 ppm 只读",
            "30027 累计运行时间 uint32 h 只读",
            "10001 送风机运行状态 bool - 只读",
            "10002 防冻报警 bool - 只读",
            "10003 过滤器压差报警 bool - 只读",
            "10004 送风机故障 bool - 只读",
            "10005 急停状态 bool - 只读",
            "10006 远程/本地模式 bool - 只读，1=远程",
            "40001 送风温度设定值 float32 0.1℃ 读写，范围 12 至 30℃",
            "40003 运行模式 uint16 - 读写，0=停机 1=制冷 2=制热 3=通风",
            "40004 送风静压设定值 float32 Pa 读写，范围 100 至 600 Pa",
            "40006 最小新风阀开度 float32 % 读写，范围 0 至 100%",
            "40008 过滤器压差报警阈值 float32 Pa 读写，出厂值 250 Pa",
            "40010 防冻保护温度 float32 0.1℃ 读写，出厂值 5℃",
            "40012 定时启停使能 uint16 - 读写，0=禁用 1=启用",
            "00001 远程启停命令 coil - 读写，1=启动",
            "00002 报警复位 coil - 读写，写 1 复位可复位报警",
        ],
    ),
    (
        "第 7 章 控制逻辑",
        [
            "制冷模式下，控制器根据送风温度与设定值的偏差，以 PID 方式调节冷水阀开度。",
            "当回风温度低于设定值 2℃ 以上时，冷水阀关闭至最小开度。",
            ("防冻保护：表冷器出风温度低于 5℃ 时，控制器关闭新风阀、"
            "全开冷热水阀并停止送风机。"),
            "过滤器压差超过报警阈值并持续 60 秒后触发报警，不会自动停机。",
        ],
    ),
    (
        "第 8 章 报警代码",
        [
            "A01 送风温度传感器故障：传感器开路或短路，读数超出 -40 至 120℃。",
            "A02 回风温度传感器故障：传感器开路或短路。",
            "A05 过滤器压差高：初效或中效过滤器压差超过 250 Pa，需检查过滤器是否堵塞。",
            "A07 防冻保护动作：表冷器出风温度低于 5℃，机组已停止送风机。",
            "A09 送风机过载：热继电器动作或变频器报故障，需现场复位。",
            "A03 新风温度传感器故障：传感器开路或短路。",
            "A04 湿度传感器故障：读数超出 0 至 100%RH。",
            "A05 过滤器压差高：初效或中效过滤器压差超过 250 Pa，需检查过滤器是否堵塞。",
            "A06 送风静压低：静压持续 120 秒低于设定值的 50%，检查风机及风管漏风。",
            "A07 防冻保护动作：表冷器出风温度低于 5℃，机组已停止送风机。",
            "A08 冷水阀反馈偏差：阀位反馈与指令偏差超过 15% 持续 300 秒，检查执行器。",
            "A09 送风机过载：热继电器动作或变频器报故障，需现场复位。",
            "A10 急停：急停按钮被按下，需旋开急停按钮后在本地复位。",
            "A11 二氧化碳浓度高：送风二氧化碳浓度超过 1000 ppm。",
            "A12 通讯超时：超过 30 秒未收到主站轮询。",
            "A13 控制器存储器错误：参数校验失败，需恢复出厂设置后重新配置。",
        ],
    ),
    (
        "第 9 章 故障排查",
        [
            "现象：送风温度持续升高，达不到设定值。可能原因及处理：",
            "1. 冷水阀实际开度与指令不一致，检查执行器及 30009 反馈值；",
            "2. 冷冻水供水温度偏高，若高于 12℃ 请联系冷源侧检查冷水机组；",
            "3. 过滤器堵塞导致风量下降，检查 30011 过滤器压差；",
            "4. 表冷器翅片积灰，需清洗盘管。",
            ("现象：送风机不运转。检查急停按钮是否按下，热继电器是否动作（A09），"
            "以及变频器面板是否显示故障。"),
        ],
    ),
    (
        "第 10 章 故障排查（续）",
        [
            ("现象：送风量明显不足。检查过滤器是否堵塞，皮带是否松弛打滑，"
            "风阀是否处于正确开度。"),
            "现象：冷凝水从机组底部溢出。检查排水管是否堵塞，水封高度是否足够。",
            "现象：机组运行噪声异常增大。检查风机轴承润滑及叶轮是否积尘失衡。",
        ],
    ),
    (
        "第 11 章 维护保养",
        [
            "初效过滤器每月检查一次，压差超过 150 Pa 时应清洗或更换。",
            "中效袋式过滤器每季度检查一次，不可清洗，压差超过 250 Pa 时必须更换。",
            "送风机皮带每半年检查张紧度，用拇指按压皮带中部下沉量应为 10 至 15 mm。",
            "冬季停机前须排空表冷器盘管内的存水，防止冻裂。",
        ],
    ),
    (
        "第 12 章 保修与服务",
        [
            "整机保修期为自验收之日起 18 个月或自出厂之日起 24 个月，以先到者为准。",
            "因未按本手册进行维护造成的损坏不在保修范围内。",
            "技术服务热线：400-800-0000。",
        ],
    ),
]

# A different device model whose manual deliberately reuses the AHU's
# vocabulary and alarm codes. A correctly scoped search for the AHU must never
# return these pages.
CX_500_PAGES = [
    (
        "CX-500 螺杆式冷水机组 第 1 章 概述",
        [
            "CX-500 螺杆式冷水机组为空调系统提供冷冻水，额定制冷量 500 kW。",
            "冷冻水设计供水温度 7℃，回水温度 12℃。",
        ],
    ),
    (
        "第 2 章 报警代码",
        [
            "A01 冷冻水出水温度传感器故障。",
            "A05 冷冻水流量不足：水流开关断开超过 5 秒，压缩机停机。",
            "A07 冷冻水防冻保护：出水温度低于 3℃，压缩机停机。",
            "A09 压缩机过载：电机绕组温度过高。",
        ],
    ),
    (
        "第 3 章 故障排查",
        [
            ("现象：冷冻水供水温度偏高。检查冷却塔风机是否运行，"
            "冷凝器是否结垢，制冷剂是否不足。"),
            "现象：送风温度持续升高不是本机组直接测量的参数，请检查末端空调机组。",
        ],
    ),
    (
        "第 4 章 Modbus 寄存器表",
        [
            "30001 冷冻水出水温度 float32 0.1℃ 只读",
            "30003 冷冻水回水温度 float32 0.1℃ 只读",
            "40001 冷冻水出水温度设定值 float32 0.1℃ 读写",
        ],
    ),
]

VFD_G7_PARAGRAPHS = [
    ("heading", "G7 系列通用变频器 用户手册"),
    ("text", "G7 系列变频器适用于风机和水泵类负载，功率范围 0.75 至 90 kW。"),
    ("heading", "1 安全注意事项"),
    (
        "text",
        ("断电后直流母线仍有高压，必须等待 10 分钟且确认充电指示灯熄灭后"
        "方可接触主回路端子。"),
    ),
    ("heading", "2 功能参数表"),
    (
        "table",
        [
            ["参数代码", "名称", "设定范围", "出厂值"],
            ["P0.01", "运行命令通道", "0=面板 1=端子 2=通讯", "0"],
            ["P0.03", "最大输出频率", "50.00 至 400.00 Hz", "50.00 Hz"],
            ["P0.05", "加速时间", "0.1 至 3600.0 s", "20.0 s"],
            ["P0.06", "减速时间", "0.1 至 3600.0 s", "20.0 s"],
            ["P3.01", "电机额定功率", "0.1 至 1000.0 kW", "机型确定"],
            ["P8.02", "通讯波特率", "0=4800 1=9600 2=19200", "1"],
            ["P8.03", "本机通讯地址", "1 至 247", "1"],
        ],
    ),
    ("heading", "3 故障代码"),
    (
        "table",
        [
            ["故障代码", "名称", "可能原因"],
            ["E01", "加速过电流", "加速时间过短或负载突变"],
            ["E02", "减速过电流", "减速时间过短"],
            ["E05", "直流母线过电压", "减速过快或电网电压偏高"],
            ["E08", "变频器过热", "散热风扇故障或环境温度过高"],
            ["E10", "电机过载", "负载过重或电机额定参数设置错误"],
            ["E15", "通讯故障", "通讯线断开或波特率不匹配"],
        ],
    ),
    ("heading", "4 常见问题"),
    (
        "text",
        ("电机运转但转速上不去：检查最大输出频率 P0.03 和上限频率设置，"
        "以及运行命令通道是否正确。"),
    ),
    (
        "text",
        "频繁报 E05：适当延长减速时间 P0.06，必要时加装制动电阻。",
    ),
    ("text", "散热风扇建议每 3 年更换一次。"),
]


def _wrap(text: str, width: float = 44.0) -> list[str]:
    lines, current, used = [], "", 0.0
    for char in text:
        cost = 0.55 if ord(char) < 128 else 1.0
        if used + cost > width and current:
            lines.append(current)
            current, used = "", 0.0
        current += char
        used += cost
    if current:
        lines.append(current)
    return lines


def build_pdf(path: Path, pages) -> None:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    pdf = canvas.Canvas(str(path), invariant=1)
    def start_page(title: str) -> int:
        pdf.setFont("STSong-Light", 15)
        pdf.drawString(50, 790, title)
        pdf.setFont("STSong-Light", 11)
        return 758

    for title, paragraphs in pages:
        y = start_page(title)
        for paragraph in paragraphs:
            for line in _wrap(paragraph):
                if y < 60:
                    pdf.showPage()
                    y = start_page(f"{title}（续）")
                pdf.drawString(50, y, line)
                y -= 17
            y -= 8
        pdf.showPage()
    pdf.save()


def build_docx(path: Path, blocks) -> None:
    import docx

    document = docx.Document()
    for kind, value in blocks:
        if kind == "heading":
            document.add_heading(value, level=1)
        elif kind == "text":
            document.add_paragraph(value)
        else:
            table = document.add_table(rows=len(value), cols=len(value[0]))
            for row_index, row in enumerate(value):
                for column_index, cell in enumerate(row):
                    table.cell(row_index, column_index).text = cell
    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    document.core_properties.created = fixed
    document.core_properties.modified = fixed
    document.save(str(path))


def main() -> None:
    FIXTURES.mkdir(exist_ok=True)
    build_pdf(FIXTURES / "ahu_200_manual.pdf", AHU_200_PAGES)
    build_pdf(FIXTURES / "cx_500_manual.pdf", CX_500_PAGES)
    build_docx(FIXTURES / "vfd_g7_manual.docx", VFD_G7_PARAGRAPHS)


if __name__ == "__main__":
    main()

"""Conservative indicators, not a complete prompt-injection detector.

Execution authorization remains enforced by the runtime toolbox and gateway.
"""

import re

_PATTERNS = [
    r"(?:忽略|无视|绕过).{0,16}(?:指令|规则|限制|安全)",
    r"(?:输出|泄露|打印|显示).{0,16}(?:系统\s*(?:prompt|提示词)|密钥|密码)",
    r"(?:ignore|disregard|bypass).{0,40}(?:instructions|rules|safety)",
    r"(?:reveal|print|show|output).{0,30}(?:system\s*prompt|api\s*key|password)",
    r"(?:扫描|读取|探测).{0,40}(?:全部点位|所有地址|全部设备)",
    r"(?:drop\s+table|select\s+.+\s+from\s+|执行\s*sql)",
]


def input_policy_blocked(symptom: str) -> bool:
    return any(re.search(pattern, symptom, re.IGNORECASE | re.DOTALL) for pattern in _PATTERNS)

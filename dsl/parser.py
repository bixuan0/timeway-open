"""
parser.py — .tw 场景文本（中文/英文）→ ScenarioIR
====================================================
逐行 + 括号块解析。中文支持 = TOKEN_MAP 归一化（见 tokens.py）。
报错带行号，便于学生自查。

支持语句：
  场景 名字 / seed N
  节点 名字 (x, y) [朝向 deg]
  道路 A->B [车道 n] [限速 n] [弯道 r] [坡度 p%] [路面 X]   （道路/交叉口/匝道/高架/隧道/桥梁/景观）
  区域 名字 { 类型 位于 道路 km 降到 n [容量 c] }
  天气 { 剧本 a-b秒 类型 ... }
  车辆 { N辆 车型 ... }
  规则 { 当 触发(...) 动作(...) ... }
"""
from __future__ import annotations

import os
import re
from typing import Any

from .ir import ScenarioIR, _new
from .tokens import TOKEN_MAP, normalize, translate_keywords


# ---------- 预处理 ----------

# 单位单字 token：只在「数字+单位」粘连处替换（20辆/600秒/5米），
# 避免污染自定义名称——否则「百米冲刺」「秒表」「米仓」会被替换成
# 「百 m 冲刺」「sec表」「m仓」（中文数字以外的单字不触发）。
_UNIT_TOKEN_NAMES = {"米", "秒", "辆"}
# 预编译「数字+单位」正则（数字可带小数，如 1.2米/0.5公里由「公里」多字处理）
_UNIT_SUFFIX_RE = re.compile(r"(\d+(?:\.\d+)?)([%s])" % "".join(sorted(_UNIT_TOKEN_NAMES)))


def _zh_to_en_line(line: str) -> str:
    """中文关键字 -> 英文内部 token（带汉字边界，见 tokens.translate_keywords）"""
    # 数字+单位粘连："20辆"->"20 x"，"0-600秒"->"0-600 sec"，"5米"->"5 m"
    line = translate_keywords(line)
    line = _UNIT_SUFFIX_RE.sub(lambda m: f"{m.group(1)} {TOKEN_MAP[m.group(2)]} ", line)
    return line


# ---------- 通用小工具 ----------

def _split_arrow(s: str) -> tuple[str, str] | None:
    """'营地->坝区' -> ('营地','坝区')；无箭头返回 None"""
    if "->" in s:
        a, b = s.split("->", 1)
        return a.strip(), b.strip()
    return None


def _to_num(s: str) -> float | int | None:
    """'40'->40, '5%'->5, '1.2'->1.2；失败返回 None"""
    s = s.strip().rstrip("%").strip()
    try:
        f = float(s)
        return int(f) if f == int(f) else f
    except ValueError:
        return None


# ---------- 块解析 ----------

# v6 P1：路由/能耗块真正消费（不再只 meta 行式——块式与 Lark 对齐）
# v6 D1：区域天气块（weather_regions）——api 已下发 weather_regions 到地图 JSON，
# 引擎 WeatherRegionSystem.from_dict 已消费，只差行式语法接线。
_BLOCK_KEYS = {"map", "weather", "vehicles", "rules", "routing", "energy",
               "weather_regions", "pedestrians", "events"}

# v5 P0：车辆物理属性键（归一化后英文 token；与 ir._VEHICLE_PHYSICS_KEYS 一致）：
#   mass/max_gross_mass/load_factor/brake_efficiency 既有 4 键；
#   cda/rolling_resistance（引擎缓存注入）+ max_power_kw（发动机功率）+ cg_height_m（质心高度，侧翻阈值）
_VEHICLE_PHYSICS_KEYS = {
    "mass", "max_gross_mass", "load_factor", "brake_efficiency",
    "cda", "rolling_resistance", "max_power_kw", "cg_height_m",
}

# 内联多语句的语句起始关键字（用于把一行拆成多条，如
# "node a (0,0) node b (1,1)" 同行时只写一行也可全量解析）。
_INLINE_MAP_HEADS = ("node", "road", "intersection", "ramp", "elevated",
                     "tunnel", "bridge", "landscape", "zone", "traffic_light")
_INLINE_WEATHER_HEADS = ("script", "transition")
# 车辆条目以「数量」开头（5 x truck 3 x bus）：用数字做锚，
# 不能以 "x" 做锚——否则单条 "20 x truck" 会被误切成 "20" + "x truck"。
_INLINE_VEHICLE_HEADS = (r"\d",)
_INLINE_RULE_HEADS = ("when",)
_INLINE_HEADS_BY_BLOCK = {
    "map": _INLINE_MAP_HEADS,
    "weather": _INLINE_WEATHER_HEADS,
    "vehicles": _INLINE_VEHICLE_HEADS,
    "rules": _INLINE_RULE_HEADS,
}


_INLINE_PAT_CACHE: dict[tuple[str, ...], re.Pattern] = {}


def _expand_inline(stmts: list[tuple[int, str]], heads: tuple[str, ...]) -> list[tuple[int, str]]:
    """把同一行内的多条语句拆成多行（子语句行号沿用原行）。

    只按「空白 + 语句关键字」边界切分（词边界 \b 防误伤：
    close_road、x_car、nodeRoad 之类的复合词不会命中）。
    """
    if not heads:
        return stmts
    pat = _INLINE_PAT_CACHE.get(heads)
    if pat is None:
        pat = re.compile(r"\s+(?=(?:%s)\b)" % "|".join(heads))
        _INLINE_PAT_CACHE[heads] = pat
    out: list[tuple[int, str]] = []
    for lineno, ln in stmts:
        for p in pat.split(ln):
            p = p.strip()
            if p:
                out.append((lineno, p))
    return out


# v5 P0：车辆单行块（`车辆 { 20辆 A {…} 3辆 B {…} }`）的切分器。
# 只在「数字 + x（辆）+ 车型名」边界切分，跳过物理块内部数值——
# 否则 `_expand_inline` 按任意数字切会把 `{ 载重 1.6 }` 里的 1.6 切成
# 独立「车辆行」→ 缺车型告警 + physics 全部丢失（评审点出的断链）。
_INLINE_VEHICLE_ENTRY_RE = re.compile(r"\s+(?=\d+(?:\.\d+)?\s*[xX]\s+\S)")


# v6 P2.1 场景名保真（与 lark 侧同法，双解析器同语义）：中文归一化把含关键字短语
# +非汉字后缀的名字（「区域天气v6」→ weather_regions v6）拆散——翻译前按原文抓首个
# 带 { 的 scenario 行名字回填，用户写什么名字就保留什么名字。
_SCENARIO_NAME_RE = re.compile(r"^\s*(?:scenario|场景)\s+([^{]+?)\s*\{")


def _extract_scenario_name(text: str) -> str | None:
    """翻译前按原文抓场景名（首个带 { 的 scenario 行）。

    返回第一个匹配的名字；找不到（场景行缺失/英文写法不匹配）返回 None，
    调用方回退旧逻辑（取翻译后 head 余部）。
    """
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = _SCENARIO_NAME_RE.match(line)
        if m:
            return m.group(1).strip()
    return None


def _split_vehicle_inline(inner: str) -> list[str]:
    """把单个 `车辆 { … }` 块内的多条车辆条目按 `N辆 车型` 边界切开。

    只按「数字 x 车型」切（保留各自 { physics } 块不动）。
    无 x 的裸数字（物理块内 1.6/24 等）不切。
    """
    return [p.strip() for p in _INLINE_VEHICLE_ENTRY_RE.split(inner) if p.strip()]


def parse_tw(text: str) -> ScenarioIR:
    """.tw 文本 -> ScenarioIR（中文/英文均可），语法错误带行号"""
    ir = ScenarioIR(_new())
    d = ir.data

    # (行号, 行内容)：去注释 + 全角归一 + 中文归一化。
    # 行号用于报错/警告定位（对齐「报错带行号」的接口承诺）。
    lines: list[tuple[int, str]] = []
    # v6 P2.1 场景名保真（与 lark 侧同法）：中文归一化会污染含关键字短语+非汉字后缀的
    # 场景名（「场景 区域天气v6」→ scenario weather_regions v6，名字被拆）。翻译前
    # 抓原文名回填，行式/lark 双解析器名字一致且保留用户原文。
    raw_name = _extract_scenario_name(text)
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()          # 去注释
        if line:
            lines.append((lineno, normalize(_zh_to_en_line(line))))  # 全角归一 + 中文归一化

    # ---- 阶段 1：剥块（scenario/map/weather/vehicles/rules/routing/energy/weather_regions）----
    blocks: dict[str, list[tuple[int, str]]] = {"map": [], "weather": [], "vehicles": [], "rules": [],
                                                "routing": [], "energy": [], "weather_regions": [],
                                                "pedestrians": [], "events": []}
    meta_lines: list[tuple[int, str]] = []
    stack: list[str] = []
    cur: str | None = None
    name = ""

    for lineno, ln in lines:
        has_open = "{" in ln
        has_close = "}" in ln

        if has_open and not has_close:
            # 块头行（可能带内容）：如 "map {" 或 "map { node a (0,0)"
            head = ln[: ln.rfind("{")].strip()
            if head.startswith("scenario "):
                name = raw_name or head[len("scenario"):].strip()
                stack.append("meta")
                cur = "meta"
            else:
                key = head.split()[0] if head.split() else head
                if key in _BLOCK_KEYS:
                    stack.append(key)
                    cur = key
                else:
                    stack.append("?")
                    cur = None
            # { 之后的剩余内容与块头同行：按当前块收集
            rest = ln[ln.rfind("{") + 1:].strip()
            if rest:
                if cur == "meta":
                    meta_lines.append((lineno, rest))
                elif cur in blocks:
                    blocks[cur].append((lineno, rest))
            continue

        if has_open and has_close:
            # 内联块：剥括号，不改变块栈
            stmt = ln.replace("{", "").replace("}", "").strip()
            if stmt:
                head = stmt.split()[0]
                if head in _BLOCK_KEYS:
                    # 单行块（如 rules { when ... }）：先做内联多语句展开，
                    # 再逐条进入对应块解析（"map { node a node b }" 同行可全量解析）
                    if head == "vehicles":
                        # v5 P0 修复：车辆单行块不能用「任意数字开头」切分——
                        # `_expand_inline` 的 vehicle head 是 `\d`，会把物理块里
                        # 的 1.6/24 等数值切成独立「车辆行」，physics 全部丢失。
                        # 这里只在 `N x 车型` 边界切分（数字后必须跟 x），
                        # 且保留原始括号让 _parse_vehicle_stmt 提取物理块。
                        _inner = ln[ln.find("{") + 1: ln.rfind("}")].strip()
                        if _inner:
                            for _sub in _split_vehicle_inline(_inner):
                                _dispatch_stmt("vehicles", lineno, _sub, ir, d)
                    else:
                        inner = stmt[len(head):].strip()
                        if inner:
                            # v6 P1：routing/energy 无内联头部 → .get 兜底空元组
                            for _, sub in _expand_inline([(lineno, inner)],
                                                         _INLINE_HEADS_BY_BLOCK.get(head, ())):
                                _dispatch_stmt(head, lineno, sub, ir, d)
                elif cur in ("map", "weather", "rules") \
                        and head in _INLINE_HEADS_BY_BLOCK.get(cur, ()):
                    # 内联块内可能同行混写多条语句（如「节点 1 (0,0) 信号灯 1 {…}」）。
                    # 若在此立即 dispatch，剥离｛｝后的 head 是首条语句（node），
                    # 尾部 traffic_light 会被当参数吞并（无告警）。统一收集到对应块，
                    # 阶段2 按行序 _expand_inline 拆分 dispatch——节点先建、信号灯/规则
                    # 后挂靠，语义依赖成立，未知尾句也不会静默消失。
                    blocks[cur].append((lineno, stmt))
                elif cur == "vehicles":
                    # v5 P0 修复：车辆物理块 { 质量 … 质心高度 … } 必须整行交给
                    # _parse_vehicle_stmt（它自己找 {…} 提取 physics）。若在此剥括号，
                    # 物理块在进入行级解析器前就被丢弃——正是「字段存进去不生效」的前置断链。
                    _dispatch_stmt(cur, lineno, ln, ir, d)
                elif cur and stmt:
                    _dispatch_stmt(cur, lineno, stmt, ir, d)
            continue

        if ln.strip() == "}":
            # 块结束
            if stack:
                stack.pop()
            cur = stack[-1] if stack else None
            continue

        if has_close:
            # 只有 }，且不是纯 }：多行块的最后一行（语句 + }）
            stmt = ln[: ln.rfind("}")].strip()
            if cur and stmt:
                _dispatch_stmt(cur, lineno, stmt, ir, d)
            if stack:
                stack.pop()
            cur = stack[-1] if stack else None
            continue

        # 普通语句行
        if cur == "meta":
            meta_lines.append((lineno, ln))
        elif cur in blocks:
            blocks[cur].append((lineno, ln))
        # cur 为 None 或 '?' 的行忽略

    # ---- 阶段 2：执行语句 ----
    d["meta"]["name"] = name
    for lineno, ln in meta_lines:
        _dispatch_stmt("meta", lineno, ln, ir, d)
    for key in ("map", "weather", "vehicles", "rules", "routing", "energy",
                "weather_regions", "pedestrians", "events"):
        heads = _INLINE_HEADS_BY_BLOCK.get(key, ())
        for lineno, ln in _expand_inline(blocks[key], heads):
            _dispatch_stmt(key, lineno, ln, ir, d)
    return ir


def _dispatch_stmt(ctx: str, lineno: int, ln: str, ir: ScenarioIR, d: dict):
    """按上下文分发单条语句。

    - 语法级错误（_TwError）统一带「第N行」前缀抛出；
    - 语义级「无法识别/暂不支持」统一收集为 warnings（不中断解析），
      避免原先的静默丢弃——用户写了但没生效的规则再也不会无提示。
    """
    def _warn(msg: str):
        d.setdefault("warnings", []).append(f"第{lineno}行: {msg}")

    try:
        if ctx == "meta":
            _parse_meta(ln, d)
        elif ctx == "map":
            _parse_map_stmt(ln, d, _warn)
        elif ctx == "weather":
            _parse_weather_stmt(ln, d)
        elif ctx == "vehicles":
            _parse_vehicle_stmt(ln, d, _warn)
        elif ctx == "rules":
            _parse_rule_stmt(ln, d, _warn)
        elif ctx == "routing":
            # v6 P1：多代价路由块（路由 { 模式 最省 车辆 X 权重 {...} }）
            _parse_routing_stmt(ln, d, _warn)
        elif ctx == "energy":
            # v6 P2：能耗块（能耗 { 报告 开启 每段 }）
            _parse_energy_stmt(ln, d, _warn)
        elif ctx == "weather_regions":
            # v6 D1：区域天气块（区域天气 { 区域 名 { 多边形 [...] 天气 X 强度 N } }）
            _parse_weather_region_stmt(ln, d, _warn)
        elif ctx == "pedestrians":
            # v6 P2：行人块（行人 { 数量 30 密度 0.05 }）→ d["pedestrians"]
            _parse_pedestrians_stmt(ln, d, _warn)
        elif ctx == "events":
            # v6 P2：事件块（事件 { 施工 { 位置 A 时刻 600 车道 2 } }）
            # → d["events"] = [{type, location, time_sec, lanes, duration}]
            _parse_events_stmt(ln, d, _warn)
    except _TwError as e:
        raise _TwError(f"第{lineno}行: {e}") from None


class _TwError(Exception):
    pass


# ---------- 各块语句 ----------

def _parse_routing_stmt(ln: str, d: dict, _warn=None):
    """v6 P1：多代价路由块（路由 { 模式 最省 车辆 X 权重 {最省 0.8, 最安全 0.2} }）。

    写入 d["routing"] = {profile, vehicle_ref, weights, constraints}。
    行式语法与 Lark 块一致（英文 token：profile/vehicle/weights）。
    """
    _warn = _warn or (lambda msg: None)
    toks = ln.split()
    if not toks:
        return
    rd = d.setdefault("routing", {})
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in ("profile", "vehicle", "vehicles"):
            if i + 1 < len(toks):
                val = toks[i + 1]
                if t == "profile":
                    rd["profile"] = val
                else:
                    rd["vehicle_ref"] = val
                i += 2
                continue
        elif t == "weights":
            # 权重 {名 值, ...}（行内花括号已被剥块器移除，剩下 key-value 序列）
            j = i + 1
            w = {}
            while j + 1 < len(toks) and toks[j] != "}":
                k, vraw = toks[j], toks[j + 1]
                _vn = _to_num(vraw)
                if _vn is not None:
                    w[k] = float(_vn)
                    j += 2
                    continue
                j += 1
            if w:
                rd["weights"] = w
            i = j
            continue
        i += 1
    if not rd.get("profile") and not rd.get("vehicle_ref") and not rd.get("weights"):
        _warn(f"路由块未识别到有效字段（模式/车辆/权重）: {ln}")


def _parse_energy_stmt(ln: str, d: dict, _warn=None):
    """v6 P2：能耗块（能耗 { 报告 开启 每段 }）→ d["energy"] = {report, granularity}。"""
    _warn = _warn or (lambda msg: None)
    toks = ln.split()
    if not toks:
        return
    ed = d.setdefault("energy", {})
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "report" and i + 1 < len(toks):
            ed["report"] = toks[i + 1]
            i += 2
            continue
        if t in ("granularity", "per_segment"):
            ed["granularity"] = "per_segment"
            i += 1
            continue
        i += 1
    if not ed.get("report") and not ed.get("granularity"):
        _warn(f"能耗块未识别到有效字段（报告/粒度）: {ln}")


def _parse_pedestrians_stmt(ln: str, d: dict, _warn=None):
    """v6 P2：行人块（行人 { 数量 30 密度 0.05 } / 行人 数量 30 密度 0.05）。

    写入 d["pedestrians"] = {"count": 30, "density": 0.05}；
    缺省时引擎回退全局时段密度（不改变现有行为）。
    """
    pd = d.setdefault("pedestrians", {})
    toks = ln.split()
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "count" and i + 1 < len(toks):
            v = _to_num(toks[i + 1])
            if v is not None:
                pd["count"] = max(0, int(v))
            i += 2
            continue
        if t == "density" and i + 1 < len(toks):
            v = _to_num(toks[i + 1])
            if v is not None:
                pd["density"] = max(0.0, min(1.0, float(v)))
            i += 2
            continue
        i += 1
    if not pd:
        _warn(f"行人块未识别到有效字段（数量/密度）: {ln}")


# .tw 事件类型 → 引擎 SCENARIO_EVENTS 动作类型（与 main._trigger_scenario_event 消费一致）
_EVENT_TYPE_MAP = {
    "blasting": "road_closure",      # 爆破封路 → 全车道封闭
    "construction": "road_closure",  # 施工封路
    "rockfall": "lane_blockage",     # 落石占道 → 部分车道封闭
    "lane_blockage": "lane_blockage",
    "pedestrian_crossing": "pedestrian_crossing",  # 人员横穿 → 节点附近减速
    "crossing": "pedestrian_crossing",
}


def _parse_events_stmt(ln: str, d: dict, _warn=None):
    """v6 P2：事件块（事件 { 爆破 位置 坝区->骨料场 时刻 600 车道 2 时长 1800 }）。

    每条事件写入 d["events"] = [{
        event_id, type, edge_from/edge_to 或 node_id,
        trigger_time, duration, lanes, blockage_ratio(可选)
    }]——与 main._trigger_scenario_event（road_closure/lane_blockage/
    pedestrian_crossing）字段一一对应。缺省字段引擎兜底。
    """
    evs = d.setdefault("events", [])
    toks = ln.split()
    if not toks:
        return
    etype = _EVENT_TYPE_MAP.get(toks[0])
    if etype is None:
        _warn(f"事件块未识别事件类型 '{toks[0]}'（可选: 爆破/施工/落石/横穿）")
        return
    ev = {"event_id": f"tw_ev_{len(evs) + 1}", "type": etype}
    i = 1
    while i < len(toks):
        t = toks[i]
        if t == "location" and i + 1 < len(toks):
            loc = toks[i + 1]
            ab = _split_arrow(loc)
            if ab:
                ev["edge_from"], ev["edge_to"] = ab
            else:
                ev["node_id"] = loc
            i += 2
            continue
        if t == "time_sec" and i + 1 < len(toks):
            v = _to_num(toks[i + 1])
            if v is not None:
                ev["trigger_time"] = float(v)
            i += 2
            continue
        if t == "duration" and i + 1 < len(toks):
            v = _to_num(toks[i + 1])
            if v is not None:
                ev["duration"] = float(v)
            i += 2
            continue
        if t == "lanes" and i + 1 < len(toks):
            v = _to_num(toks[i + 1])
            if v is not None:
                ev["lanes"] = int(v)
                if etype == "lane_blockage":
                    ev["blockage_ratio"] = min(1.0, max(0.0, int(v) / 2.0))
            i += 2
            continue
        i += 1
    evs.append(ev)


def _parse_meta(ln: str, d: dict):
    toks = ln.split()
    if toks and toks[0] == "seed":
        v = _to_num(toks[1]) if len(toks) > 1 else None
        if v is not None:
            d["meta"]["seed"] = int(v)
    elif toks and toks[0] in ("runs", "run_repeat", "times"):
        # v6 P0：仿真次数（运行 N 次 / runs N）。次/次数已归一化为 runs/times，
        # 取首个数值即可（尾部 times 单位已入 toks）。
        v = next((_to_num(t) for t in toks[1:] if _to_num(t) is not None), None)
        if v is not None:
            d["meta"]["runs"] = max(1, int(v))
    elif toks and toks[0] == "vehicle_total":
        # v6 P0：车辆总数（可选，覆盖车辆块 count 之和；真正驱动引擎 n_vehicles）
        v = next((_to_num(t) for t in toks[1:] if _to_num(t) is not None), None)
        if v is not None:
            d["meta"]["n_vehicles"] = max(1, int(v))
    elif toks and toks[0] in ("av_ratio", "av_penetration"):
        # v6 P2：AV 渗透率（AV渗透率 0.6）
        v = next((_to_num(t) for t in toks[1:] if _to_num(t) is not None), None)
        if v is not None:
            d["meta"]["av_ratio"] = max(0.0, min(1.0, float(v)))
    elif toks and toks[0] == "time_slot":
        # v6 P2：时段（时段 晚高峰）
        if len(toks) > 1:
            d["meta"]["time_slot"] = " ".join(toks[1:])
    elif toks and toks[0] == "temperature":
        # v6 P2：温度（温度 23 / 23 摄氏度）
        v = next((_to_num(t) for t in toks[1:] if _to_num(t) is not None), None)
        if v is not None:
            d["meta"]["temperature"] = float(v)
    elif toks and toks[0] == "wind_speed":
        # v6 P2：风速（风速 4 米每秒）
        v = next((_to_num(t) for t in toks[1:] if _to_num(t) is not None), None)
        if v is not None:
            d["meta"]["wind_speed"] = float(v)
    elif toks and toks[0] == "wind_mode":
        # v6 P2：风场模式（风场 进阶 / basic）
        if len(toks) > 1:
            d["meta"]["wind_mode"] = toks[1]
    elif toks and toks[0] == "output_types":
        # v6 P2：输出类型（输出类型 摘要 验证指标 天气系统）
        _valid = {"summary", "validation_metrics", "weather_system", "mental_state",
                  "event_logs", "vehicle_logs", "violation_types", "job_stats",
                  "power_check"}
        _sel = [t for t in toks[1:] if t in _valid]
        if _sel:
            d["meta"]["output_types"] = _sel
    elif toks and toks[0] == "routing":
        # v3 预留：路由模式/车辆（P3 多代价路由时消费）
        d["routing"]["profile"] = toks[1] if len(toks) > 1 else ""
        # 「车辆」归一化为 vehicles（复数），英文写法 vehicle 也兼容
        if "vehicles" in toks:
            idx = toks.index("vehicles")
            if idx + 1 < len(toks):
                d["routing"]["vehicle_ref"] = " ".join(toks[idx + 1:])
        elif "vehicle" in toks:
            idx = toks.index("vehicle")
            if idx + 1 < len(toks):
                d["routing"]["vehicle_ref"] = " ".join(toks[idx + 1:])
    elif toks and toks[0] == "energy":
        # v3 预留：能耗报告开关（P2 能耗分析器时消费）
        # v6 P2 语义归一：report 值是动作词——「报告/开启/启用/on」→ on（启动能耗分析），
        # 「关闭/禁用/off」→ off；granularity=per_segment 由「每段/per_segment」词置位。
        _rw = toks[1] if len(toks) > 1 else ""
        if _rw in ("report", "on", "开启", "启用"):
            _rw = "on"
        elif _rw in ("off", "关闭", "禁用"):
            _rw = "off"
        d["energy"]["report"] = _rw
        if "per_segment" in toks:
            d["energy"]["granularity"] = "per_segment"


def _parse_map_stmt(ln: str, d: dict, _warn=None):
    toks = ln.split()
    if not toks:
        return
    head = toks[0]
    m = d["map"]

    if head == "node":
        node = {"id": toks[1] if len(toks) > 1 else "?"}
        # 坐标组 (x, y[, z]) 从整行提取（避免 split 拆开）；第三坐标 = v3 高程
        c = re.search(r"\(\s*([-\d.]+)\s*[,，]\s*([-\d.]+)\s*(?:[,，]\s*([-\d.]+)\s*)?\)", ln)
        if not c:
            raise _TwError(f"节点 '{node['id']}' 缺少数字坐标 (x, y)")
        node["x"], node["y"] = float(c.group(1)), float(c.group(2))
        if c.group(3) is not None:
            node["z"] = float(c.group(3))
        if "heading" in toks:
            idx = toks.index("heading")
            if idx + 1 < len(toks):
                v = _to_num(toks[idx + 1])
                if v is not None:
                    node["heading"] = v
        m["nodes"].append(node)

    elif head in ("road", "intersection", "ramp", "elevated", "tunnel", "bridge", "landscape"):
        edge: dict[str, Any] = {"kind": head}
        # 起点->终点 在第一个非关键字参数
        arg_start = 1
        if len(toks) > 1:
            ab = _split_arrow(toks[1])
            if ab:
                edge["from"], edge["to"] = ab
                arg_start = 2
            else:
                edge["id"] = toks[1]
                for t in toks[2:]:
                    ab = _split_arrow(t)
                    if ab:
                        edge["from"], edge["to"] = ab
                        break
        # key-value 参数
        # v3：surface 支持附带属性（破损/平整度/路宽/承载）→ 提升为字典；
        #     高程剖面 elevation_profile [(里程, 高程), ...] 单字段承载。
        i = arg_start
        surface_material = None
        surf_attrs: dict[str, Any] = {}
        # 单位噪声 token（数字+单位被拆出，如 路宽 6米 -> width 6 m），
        # 单独消耗一个位置，避免打乱后续 key-value 对位。
        unit_noise = {"m", "sec", "min", "km", "kmh", "x"}
        while i < len(toks) - 1:
            k, v = toks[i], _to_num(toks[i + 1])
            if k in unit_noise:
                i += 1
                continue
            if k == "lanes" and v is not None:
                edge["lanes"] = int(v)
            elif k == "speed" and v is not None:
                edge["speed_limit"] = v
            elif k == "curve" and v is not None:
                edge["curve_radius"] = v
            elif k == "slope" and v is not None:
                edge["slope"] = v
            elif k == "surface":
                surface_material = toks[i + 1]
            elif k == "damage" and v is not None:
                # 破损 20% -> 0.2（IR 0~1）。v6 P2 口径统一（与 lark 对齐）：
                # 显式 % 或裸数字 >1 按百分比 ÷100（破损 15 -> 0.15）；
                # 裸数字 <=1 视为 0~1 小数原值（破损 0.15 -> 0.15，不误除——
                # 旧口径恒 ÷100 会把显式小数误读成 0.15%）。
                d = float(v)
                if toks[i + 1].endswith("%") or d > 1.0:
                    d /= 100.0
                surf_attrs["damage"] = d
            elif k == "iri" and v is not None:
                surf_attrs["iri"] = v
            elif k == "width" and v is not None:
                surf_attrs["width"] = v
            elif k == "load_class":
                surf_attrs["load_class"] = toks[i + 1]
            elif k in ("mu", "mu_override") and v is not None:
                surf_attrs["mu_override"] = v   # v4 P1：显式附着系数（已归一为 mu_override）
            elif k in ("elevation_profile", "profile"):
                # 兼容 (0,45) / (0km,45m) / (0.8 km, 82 m) 三种写法；
                # 距离带 km 单位时换算为米（公里→×1000）
                pts = re.findall(
                    r"\(\s*([-\d.]+)\s*([a-zA-Z]*)\s*[,，]\s*([-\d.]+)\s*([a-zA-Z]*)\s*\)", ln)
                if pts:
                    prof = []
                    for a, ua, b, _ub in pts:
                        dist = float(a)
                        if ua.lower() in ("km", "公里"):
                            dist *= 1000.0
                        prof.append({"distance": dist, "z": float(b)})
                    edge["elevation_profile"] = prof
            i += 2
        if surface_material is not None and not surf_attrs:
            edge["surface"] = surface_material          # v2 写法：字符串材质
        elif surf_attrs or surface_material is not None:
            edge["surface"] = {"material": surface_material, **surf_attrs} if surface_material else surf_attrs
        if not edge.get("id"):
            edge["id"] = f"{edge.get('from','?')}_{edge.get('to','?')}"
        m["edges"].append(edge)

    elif head == "zone":
        zone: dict[str, Any] = {"id": toks[1] if len(toks) > 1 else "?"}
        i = 2
        while i < len(toks):
            t = toks[i]
            if t in ("speed_limit", "work_zone"):
                zone["type"] = t
            elif t == "at":
                i += 1
                ab = _split_arrow(toks[i]) if i < len(toks) else None
                if ab:
                    zone["edge"] = f"{ab[0]}->{ab[1]}"
            elif t == "km":
                # 兼容两种写法：「数值 km」（1.2公里→1.2 km）与「km 数值」
                v = _to_num(toks[i - 1]) if i > 0 else None
                if v is None and i + 1 < len(toks):
                    v = _to_num(toks[i + 1])
                if v is not None:
                    zone["km"] = v
            elif t == "to" and i + 1 < len(toks):
                v = _to_num(toks[i + 1])
                if v is not None:
                    zone["params"] = zone.get("params", {})
                    zone["params"]["limit_kmh"] = v
            elif t == "capacity" and i + 1 < len(toks):
                v = _to_num(toks[i + 1])
                if v is not None:
                    zone["params"] = zone.get("params", {})
                    zone["params"]["capacity"] = int(v)
            i += 1
        m["zones"].append(zone)

    elif head == "traffic_light":
        # S2 修复：信号灯配时 → 写入节点 traffic_light_config（与编辑器字段名一致，
        # CustomMapLoader 直接消费：red_duration / green_duration / yellow_duration / initial_state）
        nid = toks[1] if len(toks) > 1 else None
        cfg: dict[str, Any] = {}
        i = 2
        while i < len(toks):
            k = toks[i]
            v = _to_num(toks[i + 1]) if i + 1 < len(toks) else None
            if k in ("red_duration", "green_duration", "yellow_duration") and v is not None:
                cfg[k] = float(v)
            elif k == "initial_state" and i + 1 < len(toks):
                cfg["initial_state"] = toks[i + 1]
                i += 1
            elif k == "is_pedestrian":
                cfg["is_pedestrian"] = True
            i += 1
        if nid is None:
            _warn("信号灯语句缺少节点 id")
        else:
            node = next((n for n in m["nodes"] if str(n.get("id")) == str(nid)), None)
            if node is None:
                _warn(f"信号灯节点 '{nid}' 未定义")
            else:
                node["traffic_light_config"] = cfg

    else:
        # S2/S5 修复：未识别语句不再静默丢弃，明确告警（可排查而非消失）
        _warn(f"无法识别的地图语句（已忽略）: {ln}")


# P0-1 修复：英文天气词 -> config.WEATHER_TYPES 中文键（.tw 英文写法
# 与中文写法最终收敛到同一词表，WeatherSystem / 路由按中文键消费）。
_WEATHER_CN = {
    # 英文 -> config 中文键（.tw 英文写法收敛到同一词表）
    "clear": "晴", "sunny": "晴", "cloudy": "多云", "overcast": "多云",
    "drizzle": "小雨", "light_rain": "小雨", "rain": "小雨", "moderate_rain": "小雨",
    "heavy_rain": "大雨", "storm": "大雨", "rainstorm": "大雨",
    "thunderstorm": "雷雨", "fog": "雾", "mist": "雾", "night": "夜间",
    "snow": "雪", "sleet": "雪", "hail": "冰雹", "ice": "冰雹",
    # 中文别名不再收敛：ir.validate 的 WEATHER_TYPES 已放行 暴雨/团雾/结冰，
    # lark 保留中文原文——行式若再收敛成 大雨/雾 会与校验词表+lark 分歧。
    # 引擎剧本消费的是保留后的中文原名（WeatherSystem 按 config 中文键匹配）。
}


def _parse_weather_region_stmt(ln: str, d: dict, _warn=None):
    """v6 D1：区域天气块（区域天气 { 区域 雾区 { 多边形 [...] 天气 雾 强度 0.8 } }）。

    行式语法（归一化后英文 token）：
      zone 名 polygon [(x1,y1),(x2,y2),...] weather <天气> intensity <0~1>
    写入 d["weather_regions"] = [{id, polygon, weather, layer, intensity}]，
    与引擎 WeatherRegion.from_dict 的 schema 完全对齐。
    多边形正则接受 (x,y) / (x, y) / 中英文逗号。
    """
    _warn = _warn or (lambda msg: None)
    toks = ln.split()
    if not toks:
        return
    rid = None
    if toks[0] == "zone" and len(toks) > 1:
        rid = toks[1]
    # 多边形顶点 [(x,y), ...] 从整行提取（避免 split 拆开坐标）
    pts = re.findall(r"\(\s*([-\d.]+)\s*[,，]\s*([-\d.]+)\s*\)", ln)
    polygon = [(float(a), float(b)) for a, b in pts] if pts else []
    if not polygon:
        _warn(f"区域天气缺少多边形顶点（应为 [(x,y),(x,y),...]）: {ln}")
        return
    if len(polygon) < 3:
        _warn(f"区域天气多边形至少需要 3 个顶点（当前 {len(polygon)}）: {ln}")
        return
    # 天气种类：weather 之后的下一个 token（中文「雾/雨」直接保留，
    # 引擎 config.WEATHER_TYPES 中文键消费；英文名走别名归一）
    weather = None
    if "weather" in toks:
        i = toks.index("weather")
        if i + 1 < len(toks):
            weather = _WEATHER_CN.get(toks[i + 1], toks[i + 1])
    if not weather:
        _warn(f"区域天气缺少天气类型（天气 雾/大雨/...）: {ln}")
        return
    # 强度（intensity 后数值；缺省 1.0）
    intensity = 1.0
    if "intensity" in toks:
        i = toks.index("intensity")
        if i + 1 < len(toks):
            v = _to_num(toks[i + 1])
            if v is not None:
                intensity = max(0.0, min(1.0, float(v)))
    d.setdefault("weather_regions", []).append({
        "id": rid or f"wr_{len(d.get('weather_regions', [])) + 1}",
        "polygon": polygon,
        "weather": weather,
        "layer": 0,
        "intensity": intensity,
    })


def _parse_weather_stmt(ln: str, d: dict):
    """剧本行：'script 0-600 sec clear' / '0-10 min heavy_rain'（分钟自动换算），
    显式天气转移：'transition 1800 to rain'（中文：转移 1800 转向 暴雨）。"""
    toks = ln.split()
    if not toks:
        return
    # 显式天气转移（IR weather.transition 槽位补齐；SCRIPT 之外的第二通道）
    if toks[0] == "transition":
        if len(toks) < 3:
            raise _TwError(f"天气转移格式应为「transition 时间转向 天气」，当前: {ln}")
        at = _to_num(toks[1])
        if at is None:
            raise _TwError(f"天气转移缺少时间数值: {ln}")
        unit = next((t for t in toks[2:] if t in ("sec", "min")), "sec")
        at_sec = at * 60.0 if unit == "min" else at
        if at_sec < 0:
            raise _TwError(f"天气转移时间不能为负: {ln}")
        # to 之后的第一个非单位/非转向词 = 目标天气（跳过 sec/min/to）
        weather = None
        for t in toks[2:]:
            if t in ("sec", "min", "to"):
                continue
            weather = _WEATHER_CN.get(t, t)
            break
        if not weather:
            raise _TwError(f"天气转移缺少目标天气: {ln}")
        d["weather"]["transition"].append({"at_sec": at_sec, "to": weather})
        return
    # 「剧本」已在预处理归一化为 script；无 script 前缀的裸行也兼容（idx=0）
    idx = 1 if toks[0] == "script" else 0
    if idx >= len(toks):
        return
    m = re.match(r"([\d.]+)-([\d.]+)", toks[idx])
    if not m:
        raise _TwError(
            f"天气剧本时段格式应为「script 开始-结束(秒|分钟) 天气」，当前: {ln}")
    start, end = float(m.group(1)), float(m.group(2))
    # 单位换算：区间数字后紧跟 min 时按分钟换算为秒（默认秒）
    unit = next((t for t in toks[idx + 1:] if t in ("sec", "min")), "sec")
    if unit == "min":
        start, end = start * 60.0, end * 60.0
    if end <= start:
        raise _TwError(f"天气剧本时段结束须大于开始: {ln}")
    seg = {"start": start, "end": end, "type": ""}
    for t in toks[idx + 1:]:
        if t in ("sec", "min"):
            continue
        seg["type"] = _WEATHER_CN.get(t, t)   # 英文天气归一为 config 中文键
        break
    if not seg["type"]:
        raise _TwError(f"天气剧本缺少天气类型: {ln}")
    d["weather"]["script"].append(seg)


def _parse_vehicle_stmt(ln: str, d: dict, _warn=None):
    """'20 x dump_truck'（20辆 自卸卡车），可选 v4 物理属性块：
    '20 x dump_truck { mass 24 t brake_efficiency 0.8 }'（质量 24吨 制动效率 0.8）"""
    _warn = _warn or (lambda msg: None)
    toks = ln.split()
    if not toks:
        return
    count = _to_num(toks[0])
    if count is None:
        _warn(f"车辆行缺少数量（应为「数量 x 车型」）: {ln}")
        return
    c = int(count)
    if c < 1:
        _warn(f"车辆数量必须 >=1，当前: {c}")
        return

    # ---- v4 P1：行尾物理属性块 { 键 值 ... } ----
    physics = None
    pb = ln.find("{")
    pe = ln.rfind("}")
    if pb != -1 and pe != -1 and pe > pb:
        body = ln[pb + 1: pe].replace(",", " ")
        attrs = body.split()
        physics = {}
        i = 0
        while i < len(attrs):
            k = attrs[i]
            if k not in _VEHICLE_PHYSICS_KEYS:
                _warn(f"未知物理键 '{k}'（可选: 质量/最大总质量/载重/制动效率/风阻CdA/滚动阻力/质心高度/最大功率）: {ln}")
                i += 2
                continue
            if i + 1 >= len(attrs):
                _warn(f"物理键 '{k}' 缺少数值: {ln}")
                break
            v = _to_num(attrs[i + 1])
            if v is None:
                _warn(f"物理键 '{k}' 数值无效: {ln}")
                i += 2
                continue
            if k == "mass" or k == "max_gross_mass":
                # 支持吨（t）/千克（kg）单位后缀；无单位 = 千克
                unit = attrs[i + 2] if i + 2 < len(attrs) else None
                val = float(v)
                if unit == "t":
                    val *= 1000.0
                    i += 3
                elif unit == "kg":
                    i += 3
                else:
                    i += 2
                if k == "mass":
                    physics["mass"] = max(1.0, val)
                else:
                    physics["max_gross_mass"] = max(1.0, val)
            elif k == "load_factor":
                # v5 P0：放开到 0~1.5（水电站超载常态）；>1.0 标记 overload
                _raw_lf = float(v)
                if _raw_lf > 1.5:
                    _warn(f"载重 {_raw_lf} 超出 0~1.5（超载上限），已钳到 1.5")
                lf = max(0.0, min(1.5, _raw_lf))
                physics["load_factor"] = lf
                if lf > 1.0:
                    physics["overload"] = True
                i += 2
            elif k == "brake_efficiency":
                physics["brake_efficiency"] = min(1.0, max(0.0, float(v)))
                i += 2
            elif k in ("cda", "rolling_resistance", "cg_height_m", "max_power_kw"):
                physics[k] = float(v)
                i += 2
        if not physics:
            physics = None

    # 车型名：跳过 { 块（若有），取块前除数量与 "x" 外的首个 token
    body_before = ln[: pb if pb != -1 else len(ln)]
    rest = [t for t in body_before.split()[1:] if t != "x"]
    if not rest:
        _warn(f"车辆行缺少车型: {ln}")
        return
    entry = {"type": rest[0], "count": c}
    # v6 P1：品牌型号绑定（C 组）——「品牌 <型号ID>」归一化为 brand <id>，
    # 引擎 enable_brand_model 采样时只在该型号池中选（active_models）。
    if "brand" in rest:
        _bi = rest.index("brand")
        if _bi + 1 < len(rest):
            entry["brand_model"] = rest[_bi + 1]
    if physics:
        entry["physics"] = physics
    d["vehicles"].append(entry)


def _parse_rule_stmt(ln: str, d: dict, _warn=None):
    """'when time(600) close_road(A->B, 1800)' 或 'when crossing pedestrian(卡口)'

    _warn(msg)：无法识别的触发/动作时会告警（不再静默丢弃——
    文档承诺支持但未实现的「调度/天气联动/限速」等会明确提示）。
    """
    _warn = _warn or (lambda msg: None)
    calls = re.findall(r"([A-Za-z_]+)\s*\(([^)]*)\)", ln)
    if not calls:
        _warn(f"规则语句无法识别（应有 触发(参数) 动作(参数)）: {ln}")
        return

    def args_of(call) -> list[str]:
        return [a.strip() for a in call[1].split(",") if a.strip()]

    if "crossing" in ln:
        # 行人横穿：crossing 是触发器关键字，人员(位置) 是第一个调用
        loc_args = args_of(calls[0]) if calls else []
        loc = loc_args[0] if loc_args else ""
        trig = {"type": "crossing", "location": loc}
        if len(calls) >= 2:
            action = _build_action(calls[1][0], args_of(calls[1]))
            if action is None:
                _warn(f"动作 '{calls[1][0]}' 暂不支持（当前支持: close_road/accident/speed_limit/schedule）: {ln}")
                return
        else:
            action = {"type": "pedestrian", "location": loc}  # 隐式动作：产生行人
    else:
        if len(calls) < 2:
            _warn(f"规则需要「触发(参数) 动作(参数)」两个调用: {ln}")
            return
        trig = _build_trigger(calls[0][0], args_of(calls[0]))
        if trig is None:
            _warn(f"触发器 '{calls[0][0]}' 暂不支持（当前支持: time/random/crossing/weather_link/schedule）: {ln}")
            return
        action = _build_action(calls[1][0], args_of(calls[1]))
        if action is None:
            _warn(f"动作 '{calls[1][0]}' 暂不支持（当前支持: close_road/accident/speed_limit/schedule）: {ln}")
            return
    d["rules"].append({"id": f"rule_{len(d['rules'])+1}", "trigger": trig, "action": action})


def _build_trigger(name: str, args: list[str]) -> dict | None:
    if name == "time":
        v = _to_num(args[0]) if args else 0
        return {"type": "time", "at_sec": float(v or 0)}
    if name == "random":
        v = _to_num(args[0]) if args else 0.1
        return {"type": "random", "prob_per_min": float(v or 0.1)}
    if name == "crossing" or name == "pedestrian":
        return {"type": "crossing", "location": args[0] if args else ""}
    if name == "weather_link":
        # P1-1 修复：天气联动触发（与 lark_parser/ir 枚举一致）：
        # 天气切换/满足时触发动作，weather 参数已归一为 config 中文键
        return {"type": "weather_link", "weather": args[0] if args else ""}
    if name == "schedule":
        return {"type": "schedule", "args": list(args)}
    return None


def _build_action(name: str, args: list[str]) -> dict | None:
    if name == "close_road":
        edge = args[0] if args else ""
        duration = _to_num(args[1]) if len(args) > 1 else 0
        return {"type": "close_road", "edge": edge, "duration_sec": float(duration or 0)}
    if name == "accident":
        edge = args[0] if args else ""
        lanes = _to_num(args[1]) if len(args) > 1 else 1
        return {"type": "accident", "edge": edge, "lanes_blocked": int(lanes or 1)}
    if name == "speed":
        # S5 修复：限速动作（限速 归一化为 speed，IR 记录 speed_limit 语义）
        edge = args[0] if args else ""
        speed = _to_num(args[1]) if len(args) > 1 else None
        return {"type": "speed_limit", "edge": edge, "speed": float(speed or 0)}
    if name == "schedule":
        # P1-1 修复：调度动作（与 lark_parser/ir 枚举一致）
        return {"type": "schedule", "args": list(args)}
    return None


# ---------- 文件入口 ----------

_INCLUDE_RE = re.compile(r'^\s*(?:include|引用)\s+["\']?([^"\']+)["\']?\s*$', re.M)


def _expand_includes(path: str, _stack: list | None = None) -> str:
    """S1 修复：include 分文件（`引用 "xx.tw"` / `include "xx.tw"`）递归展开，
    支持大路网按区块拆文件。被包含文件应为块片段（地图/车辆/规则等块），
    与主文件拼接后由同一块解析器处理；循环引用直接报错而非死循环。"""
    if _stack is None:
        _stack = []
    path = os.path.abspath(path)
    if path in _stack:
        raise RuntimeError(f"include 循环引用: {' -> '.join(_stack + [path])}")
    _stack = _stack + [path]
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    def _repl(m):
        inc = m.group(1)
        base_dir = os.path.dirname(path)
        inc_path = os.path.abspath(os.path.join(base_dir, inc))
        return _expand_includes(inc_path, _stack)

    return _INCLUDE_RE.sub(_repl, text)


def parse_file(path: str) -> ScenarioIR:
    """S1 修复：支持 include 展开后解析（大路网可分文件维护）。"""
    return parse_tw(_expand_includes(path))

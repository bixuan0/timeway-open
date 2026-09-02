"""
lark_parser.py — 时途 .tw 进阶场景语言（Lark 正式文法版）
============================================================
设计原则（与 parser.py 同目标，但用正式 PEG 文法替代"行式启发式"）：
  - 中文归一化（复用 tokens.py）后交给 Lark 解析英文内部 token；
  - 文法即文档：加新关键字/新结构 = 加一条文法规则，不再"改启发式+踩坑"；
  - 进阶特性（变量 / 表达式 / 天气联动 / 复用）在"编译期"全部展开成现有
    ScenarioIR，下游引擎与校验器零改动。

支持（向后兼容 parser.py 的全部既有语法 + 进阶扩展）：
  - 变量：设 base_speed = 40
  - 表达式：限速 base_speed * 0.8   （算术 + 变量引用，仅限轻量算术）
  - 天气联动：当 天气=暴雨 道路 营地->坝区 限速 降到 20
  - 复用：引用 "base.tw" / 继承 "base.tw"  （include + 当前胜出合并）

解析入口：parse_tw_advanced(text) / parse_file_advanced(path)
"""
from __future__ import annotations

import os
import re
from typing import Any

from lark import Lark, Transformer, Tree, Token
from lark.exceptions import LarkError

from .ir import ScenarioIR, _new
from .tokens import TOKEN_MAP, normalize, translate_keywords

LARK_GRAMMAR_VERSION = "1.0"  # Lark 文法版（与 parser.py 行式版平行，互备降级）

# v6 P2：事件块类型映射（与 parser.py 行式版 _EVENT_TYPE_MAP 一致，双解析器同语义）
_EVENT_TYPE_LARK = {
    "blasting": "road_closure",          # 爆破 → 全车道封闭
    "construction": "road_closure",       # 施工 → 全车道封闭
    "rockfall": "lane_blockage",          # 落石 → 部分车道封闭
    "lane_blockage": "lane_blockage",
    "pedestrian_crossing": "pedestrian_crossing",
    "crossing": "pedestrian_crossing",
}

# ---------- 预处理（与 parser.py 同款中文归一化）----------

_UNIT_NAMES = {"米", "秒", "辆"}
_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)([%s])" % "".join(sorted(_UNIT_NAMES)))


def _zh_to_en_line(line: str) -> str:
    """中文关键字 -> 英文内部 token（带汉字边界，见 tokens.translate_keywords）"""
    out = translate_keywords(line)
    out = _UNIT_RE.sub(lambda m: f"{m.group(1)} {TOKEN_MAP[m.group(2)]} ", out)
    return out


# ---------- Lark 文法 ----------

_GRAMMAR = r"""
    start: scenario

    scenario: "scenario" NAME "{" body* "}"

    ?body: map_block
         | weather_block
         | vehicles_block
         | rules_block
         | routing_block
         | energy_block
         | pedestrians_block
         | events_block
         | weather_regions_block
         | meta_stmt
         | let_stmt
         | include_stmt
         | extends_stmt

    meta_stmt: "seed" expr | run_stmt | vehicle_total_stmt | param_stmt
          | routing_line | energy_line | output_types_stmt
    run_stmt: "runs" expr "times"?
    # v6：车辆总数——尾部单位「辆/x」可选（中文原文「辆」与归一化英文 x 均可，
    # 防「车辆总数 35 辆」带空格时归一化层跳过的场景）
    vehicle_total_stmt: "vehicle_total" expr ("x"|"辆")?
    # v6 P2：引擎参数 meta（B 组）——写 .tw 即配引擎（AV渗透率/时段/温度/风速/风场/输出类型）
    # P2 修复：param 拆成「键+单值」原子项——earley 贪婪的 param_tail* 会把相邻
    # 参数行合并（time_slot 被当 av_ratio 的尾巴、方法层 key 被后词覆盖），改为单值后
    # earley 被迫逐条拆分；output_types 是多词列表，独立成 output_types_stmt。
    param_stmt: PARAM_KEY param_single
    param_single: SIGNED_NUMBER param_unit? | NAME | WEATHER
    # 单位尾缀（摄氏度/米每秒/辆 → celsius/mps/x），吃下避免 earley 报多余 token
    param_unit: "celsius" | "mps" | "x"
    # v6 P2 修复：output_types 用命名枚举终端——否则 earley 会把 energy_report
    # 拆成 energy(字面量, 命中 energy_line) + _report(NAME)，歧义 resolve 错支丢词。
    output_types_stmt: "output_types" OUTPUT_KIND+
    OUTPUT_KIND: "summary" | "validation_metrics" | "weather_system" | "mental_state"
               | "event_logs" | "vehicle_logs" | "violation_types" | "job_stats"
               | "power_check"
    PARAM_KEY: "av_ratio"|"time_slot"|"temperature"|"wind_speed"|"wind_mode"
    # v6 P1 兼容：路由/能耗行式（向后兼容 v3 旧写法 `路由 最省 车辆 X`，无大括号）
    routing_line: "routing" routing_line_tail*
    # v6 P2 语义归一（与行式/api 桥接口径一致）：旧写法「路由 最省 车辆 X」里
    # 「最省」单独出现时 earley 原本把它当裸 NAME 丢弃（profile 空）——补动作词
    # 分支直接映射 profile，与行式「toks[0]=routing → profile=toks[1]」对齐。
    # v6 P2.1 修复（对齐块式 RFC_*/E_RPT 同款手法）：行式 tail 的关键字从匿名字面量
    # 改为命名终端——lark 默认丢弃匿名字面量 token（profile/report…），Transformer
    # 拿不到键名 → 「路由 最省 车辆 X」的 vehicle_ref、「能耗 报告 开启 每段」的
    # report/granularity 全空（与「routing_block 空转」同一根因，见上文注释）。
    ROUTE_KIND: "economy" | "fastest" | "comfortable" | "safest" | "custom"
    RL_PROFILE: "profile"
    RL_VEHICLE: "vehicle" | "vehicles"
    # tail 不再收裸 NAME：earley 的 tail* 会把后续 meta 行（能耗/路由…）当 NAME 吞进
    # 前一条行式——「路由 最省 车辆 X」后的「能耗 报告 每段」整行被并掉、能耗 block
    # 永不成形。行式旧写法的字段全部是关键字锚定的（profile/vehicle×N/动作词/报告/
    # 粒度），删掉裸分支后 earley 被迫在关键字边界切行，tail 语义归一才真正落位。
    routing_line_tail: RL_PROFILE NAME | ROUTE_KIND | RL_VEHICLE NAME
    energy_line: "energy" energy_line_tail*
    # v6 P2 语义归一（与行式/api 桥接口径一致）：
    #   - 裸「report」（无值）= 开启能耗分析（report 动作词默认 on）——earley 里只写
    #     “report” 单独出现时，旧 "report" NAME 分支无人可吃，词被丢；补裸分支收住。
    #   - granularity/per_segment → per_segment。
    EL_REPORT: "report"
    EL_GRAN: "granularity" | "per_segment"
    # 同 routing_line_tail：不裸收 NAME——否则「能耗 报告 每段」后的「路由…」行会被
    # 当 NAME 吞进能耗行，路由行永不成形（与 routing 侧同一根因，见上文注释）。
    energy_line_tail: EL_REPORT | EL_REPORT NAME | EL_GRAN NAME | EL_GRAN
    let_stmt: "let" NAME "=" expr

    map_block: "map" "{" map_stmt* "}"
    ?map_stmt: node_stmt | road_stmt | zone_stmt
    node_stmt: "node" (NAME | SIGNED_NUMBER) coord? heading?
    coord: "(" SIGNED_NUMBER "," SIGNED_NUMBER ("," SIGNED_NUMBER)? ")"
    heading: "heading" expr

    road_stmt: ROADKIND edge_post
    ?edge_post: edge_ref road_param*
              | (NAME | SIGNED_NUMBER) edge_ref road_param*
    edge_ref: (NAME | SIGNED_NUMBER) "->" (NAME | SIGNED_NUMBER)
    ?road_param: lanes_p | speed_p | curve_p | slope_p | surface_p
               | damage_p | iri_p | width_p | load_class_p | elevation_profile_p
    lanes_p: "lanes" expr
    speed_p: "speed" expr
    curve_p: "curve" expr
    slope_p: "slope" slope_val
    slope_val: SIGNED_NUMBER "%" | expr
    surface_p: "surface" SURFACE
    # v3 路面属性（破损/平整度/路宽/承载/高程剖面）：文法宽松接收，导出器按简化处理
     damage_p: "damage" expr PCT?
    PCT: "%"
    iri_p: "iri" expr
    width_p: "width" expr NAME?
    load_class_p: "load_class" NAME
    elevation_profile_p: "elevation_profile" "[" profile_point ("," profile_point)* "]"
    profile_point: "(" expr NAME? "," expr NAME? ")"

    # v6 路由 / 能耗块（深度融合：不再跳过，解析进 IR 并随地图 JSON 下发消费）
    # 关键字全部命名终端化：lark 默认丢弃匿名字面量 token（profile/report/count…），
    # Transformer 拿不到键名→整块空转。命名终端（如 PARAM_KEY）会被保留——
    # 这正是 param 能落位而 routing/energy 空转的根因。以下统一命名。
    routing_block: "routing" "{" routing_field* "}"
    RFC_PROFILE: "profile"
    RFC_VEHICLE: "vehicle" | "vehicles"
    RFC_WEIGHTS: "weights"
    routing_field: RFC_PROFILE NAME
                  | RFC_VEHICLE NAME
                  | RFC_WEIGHTS "{" routing_weight ((",")? routing_weight)* "}"
    routing_weight: NAME expr
    energy_block: "energy" "{" energy_field* "}"
    E_RPT: "report"
    E_GRAN: "granularity" | "per_segment"
    energy_field: E_RPT NAME | E_GRAN
    # v6 P2：行人块（行人 { 数量 30 密度 0.05 }）+ 事件块（事件 { 爆破 { ... } }）
    pedestrians_block: "pedestrians" "{" pedestrian_field* "}"
    PD_COUNT: "count"
    PD_DENSITY: "density"
    pedestrian_field: PD_COUNT expr | PD_DENSITY expr
    events_block: "events" "{" event_stmt* "}"
    event_stmt: EVENT_TYPE event_field*
    EV_LOC: "location"
    EV_TIME: "time_sec"
    EV_DUR: "duration"
    EV_LANES: "lanes"
    event_field: EV_LOC (edge_ref | NAME) | EV_TIME expr | EV_DUR expr | EV_LANES expr
    EVENT_TYPE: "blasting" | "construction" | "rockfall" | "lane_blockage" | "pedestrian_crossing" | "crossing"
    # v6 D1：区域天气块（区域天气 { 雾区 { 多边形 [...] 天气 雾 强度 0.8 } }）
    weather_regions_block: "weather_regions" "{" weather_region_stmt* "}"
    # v6 P2.1 修复：文档形态是「雾区 { 多边形 ... }」带花括号（测试 TW_FULL 与用户
    # .tw 同款），但原文法只有裸形态（雾区 多边形 ...）——earley 遇 { 直接拒绝。
    # 双形态兼容：带括号（结构化）与裸（旧简写）都收。
    weather_region_stmt: ("zone"? NAME) "{" wr_body* "}"
                      | ("zone"? NAME) wr_poly? wr_weather? wr_intensity?
    # ?wr_body（定义处内联）：wr_poly/wr_weather/wr_intensity 直接成为 stmt 子项——
    # 否则 transform 展平只解一层，wr_body 包着的多边形/天气/强度 Tree 全部丢失
    # （weather_regions 落空，见 transform 注释）。
    ?wr_body: wr_poly | wr_weather | wr_intensity
    WR_POLY_KW: "polygon"
    WR_WEA_KW: "weather"
    WR_INT_KW: "intensity"
    wr_poly: WR_POLY_KW "[" coord ("," coord)* "]"
    wr_weather: WR_WEA_KW WEATHER
    wr_intensity: WR_INT_KW expr

    zone_stmt: "zone" NAME "{" zone_body* "}"
    ?zone_body: speed_limit_body | work_zone_body
    speed_limit_body: "speed_limit" "at" edge_ref km_val? "to" expr
    work_zone_body: "work_zone" "at" edge_ref km_val? "capacity" expr
    km_val: SIGNED_NUMBER "km" | SIGNED_NUMBER

    weather_block: "weather" "{" weather_stmt* "}"
    weather_stmt: "script"? time_range WEATHER
                 | "transition" SIGNED_NUMBER "to" WEATHER
    time_range: SIGNED_NUMBER "-" SIGNED_NUMBER UNIT?

    vehicles_block: "vehicles" "{" vehicle_stmt* "}"
    vehicle_stmt: expr "x" NAME (BRAND_KW NAME)? phys_block?
    # v6 C 组：品牌绑定（品牌 <型号ID>）——brand 前缀固定在前、phys_block 尾随在后，
    # 单一可选后缀无重叠歧义（防 earley 把下一辆车行「5 x 水泥罐车」误判为 brand）。
    BRAND_KW: "brand"
    PHYS_KEY: "mass"|"max_gross_mass"|"load_factor"|"brake_efficiency"|"cda"|"rolling_resistance"|"max_power_kw"|"cg_height_m"
    phys_block: "{" phys_attr* "}"
    ?phys_attr: PHYS_KEY SIGNED_NUMBER phys_unit?   -> phys_pair
    phys_unit: "t"|"kg"

    rules_block: "rules" "{" rule_stmt* "}"
    rule_stmt: "when" trigger action?
    ?trigger: "time" "(" expr ")"        -> trig_time
            | "random" "(" expr ")"      -> trig_random
            | "crossing"                 -> trig_crossing
            | "weather" "=" WEATHER      -> trig_weather
    ?action: call
           | road_speed_override
    ACTION_NAME: "close_road"|"accident"|"schedule"|"pedestrian"
    call: ACTION_NAME "(" arglist? ")"
    arglist: arg ("," arg)*
    ?arg: edge_ref
        | SIGNED_NUMBER   -> number
        | NAME            -> raw
    road_speed_override: "road" edge_ref "speed" "to" expr

    include_stmt: "include" STRING
    extends_stmt: "extends" STRING

    ?expr: sum
    ?sum: product
        | sum "+" product   -> add
        | sum "-" product   -> sub
    ?product: atom
        | product "*" atom  -> mul
        | product "/" atom  -> div
    ?atom: SIGNED_NUMBER    -> number
         | NAME             -> var
         | "(" expr ")"

    NAME: /[一-鿿A-Za-z_][一-鿿A-Za-z0-9_]*/
    SIGNED_NUMBER: /[+-]?\d+(\.\d+)?/
    ROADKIND: "road"|"intersection"|"ramp"|"elevated"|"tunnel"|"bridge"|"landscape"
    SURFACE: "gravel"|"mud"|"steel"|"asphalt"|"碎石"|"泥结"|"钢板"|"沥青"
    WEATHER: "clear"|"light_rain"|"heavy_rain"|"fog"|"ice"|"snow"|"晴"|"小雨"|"大雨"|"暴雨"|"雾"|"团雾"|"冰雪"|"雪"|"结冰"|"雷雨"
    UNIT: "sec"|"min"
    STRING: /"[^"]*"/
    %import common.WS
    %ignore WS
    %ignore /\#[^\n]*/
"""

_PARSER = Lark(_GRAMMAR, parser="earley", maybe_placeholders=False,
              propagate_positions=True, ambiguity="resolve")


class TwError(Exception):
    pass


# ---------- 表达式求值（安全：仅算术 + 变量）----------

def _eval_expr(node: Tree, scope: dict) -> float:
    d = node.data
    if d in ("expr", "sum", "product", "atom"):
        return _eval_expr(node.children[0], scope)
    if d == "number":
        return float(node.children[0])
    if d == "var":
        n = str(node.children[0])
        if n not in scope:
            raise TwError(f"变量 '{n}' 未定义（进阶语法需先 '设 {n} = ...'）")
        return scope[n]
    if d in ("add", "sub", "mul", "div"):
        a = _eval_expr(node.children[0], scope)
        b = _eval_expr(node.children[1], scope)
        return {"add": a + b, "sub": a - b, "mul": a * b, "div": a / b}[d]
    raise TwError(f"无法求值的表达式节点: {d}")


# ---------- Transformer：文法 -> ScenarioIR ----------

_TRIGGER_TYPES = {"time", "random", "crossing", "weather", "weather_link"}
_ACTION_TYPES = {"close_road", "accident", "speed_limit", "pedestrian", "schedule"}


def _nums(items):
    out = []
    for x in items:
        if isinstance(x, (int, float)):
            out.append(x)
        elif isinstance(x, Token) and x.type == "SIGNED_NUMBER":
            out.append(float(x))
    return out


def _first_str(items):
    # lark 1.3 的 Token 是 str 子类：第一分支 isinstance(x, str) 会命中并
    # 把 Token 对象原样返回（下游拿到的是 Token 而非纯 str）。统一 str() 兜底。
    for x in items:
        if isinstance(x, str):
            return str(x)
    return ""


def _as_str(x):
    """参数可能是 str / Token / float，统一成字符串（位置型：封路边/人员位置）"""
    return str(x) if not isinstance(x, str) else x


class TwTransform(Transformer):
    def __init__(self, ir: ScenarioIR, scope: dict):
        super().__init__()
        self.ir = ir
        self.scope = scope

    # ---- 顶层（直接改 ir，返回 None）----
    def scenario(self, items):
        for x in items:
            if isinstance(x, Token) and x.type == "NAME":
                self.ir.data["meta"]["name"] = str(x)
        return None

    def map_block(self, items): return None
    def weather_block(self, items): return None
    def vehicles_block(self, items): return None
    def rules_block(self, items): return None
    def meta_stmt(self, items):
        # v6 P0：meta 语句（seed / runs / vehicle_total）分发
        for x in items:
            if isinstance(x, dict) and "kind" in x:
                _k = x["kind"]
                if _k == "runs" and x.get("value") is not None:
                    self.ir.data["meta"]["runs"] = max(1, int(x["value"]))
                elif _k == "vehicle_total" and x.get("value") is not None:
                    self.ir.data["meta"]["n_vehicles"] = max(1, int(x["value"]))
        ns = _nums(items)
        if ns and not any(isinstance(x, dict) and x.get("kind") in ("runs", "vehicle_total") for x in items):
            self.ir.data["meta"]["seed"] = int(ns[0])
        return None
    def run_stmt(self, items):
        ns = _nums(items)
        return {"kind": "runs", "value": int(ns[0]) if ns else None}
    def vehicle_total_stmt(self, items):
        ns = _nums(items)
        return {"kind": "vehicle_total", "value": int(ns[0]) if ns else None}
    def param_single(self, items):
        ns = _nums(items)
        s = _first_str(items)
        # 单值：数值 or 词（供 param_stmt 按 key 消费；避免多词合并吞相邻参数）
        return ns[0] if ns else (s or "")
    def param_stmt(self, items):
        # v6 P2：引擎参数 meta（B 组）——AV渗透率/时段/温度/风速/风场/输出类型
        # 单值化（P2 修复）：每条 param 只带 1 个值词，earley 无法再把「av_ratio 0.5
        # time_slot 晚高峰」合并成一条——相邻参数各成一条独立 param_stmt。
        key = None
        vals = []
        for x in items:
            if isinstance(x, Token) and str(x) in ("av_ratio", "time_slot", "temperature",
                                                   "wind_speed", "wind_mode"):
                key = str(x)
            else:
                vals.append(x)
        if not key:
            return None
        meta = self.ir.data["meta"]
        if key == "av_ratio":
            _v = _nums(vals)
            if _v:
                meta["av_ratio"] = max(0.0, min(1.0, float(_v[0])))
        elif key == "time_slot":
            _s = _first_str(vals)
            if _s:
                meta["time_slot"] = str(_s)
        elif key == "temperature":
            _v = _nums(vals)
            if _v:
                meta["temperature"] = float(_v[0])
        elif key == "wind_speed":
            _v = _nums(vals)
            if _v:
                meta["wind_speed"] = float(_v[0])
        elif key == "wind_mode":
            _s = _first_str(vals)
            if _s:
                meta["wind_mode"] = str(_s)
        return None
    def output_types_stmt(self, items):
        # v6 P2：输出类型（多词列表）独立规则——PARAM_KEY 不再含 output_types，
        # 避免相邻参数行里 output_types 的词被前一条 param 吞掉。
        _valid = {"summary", "validation_metrics", "weather_system", "mental_state",
                  "event_logs", "vehicle_logs", "violation_types", "job_stats",
                  "power_check"}
        _sel = [str(x) for x in items
                if isinstance(x, Token) and str(x) in _valid]
        if _sel:
            self.ir.data["meta"]["output_types"] = _sel
        return None
    def let_stmt(self, items): return None  # 预扫描已收集

    def include_stmt(self, items): return None
    def extends_stmt(self, items): return None

    # ---- 地图 ----
    def coord(self, items):
        ns = _nums(items)
        out = {"x": ns[0], "y": ns[1]}
        if len(ns) >= 3:
            out["z"] = ns[2]   # v3 三坐标 (x, y, z)，z 为高程
        return out
    def heading(self, items):
        ns = _nums(items)
        return {"heading": ns[0]} if ns else {}
    def node_stmt(self, items):
        nid, node = None, {}
        for x in items:
            if isinstance(x, Token) and x.type in ("NAME", "SIGNED_NUMBER") and nid is None:
                nid = str(x)
            elif isinstance(x, dict):
                node.update(x)
        node["id"] = nid or "?"
        self.ir.nodes.append(node)
        return None

    def road_stmt(self, items):
        kind = None
        edge = None
        eid = None
        params = {}
        flat = []
        for x in items:
            if isinstance(x, Tree) and x.data == "edge_post":
                flat.extend(x.children)
            else:
                flat.append(x)
        for x in flat:
            if isinstance(x, dict):
                params.update(x)
            elif isinstance(x, str) and "->" in x:
                edge = x
            elif isinstance(x, Token):
                if x.type == "ROADKIND":
                    kind = str(x)
                elif x.type == "NAME" and edge is None:
                    eid = str(x)
        if edge is None:
            return None
        # v6 P2 对齐（行式 v3 语义）：lark 的 damage/iri/width/load_class 从顶层并入
        # surface 字典 {material, damage, iri, width, load_class}——双解析器路面表达
        # 完全一致，api 下发与引擎消费按同一字典。
        _surf = params.get("surface")
        _mv = {k: params.pop(k) for k in ("damage", "iri", "width", "load_class")
               if k in params}
        if _mv:
            if not isinstance(_surf, dict):
                _surf = {"material": _surf} if _surf else {}
            _surf.update(_mv)
            params["surface"] = _surf
        from_, to_ = edge.split("->")
        e = {"kind": kind or "road", "from": from_, "to": to_,
             "id": eid or f"{from_}_{to_}"}
        e.update(params)
        self.ir.edges.append(e)
        return None

    def edge_ref(self, items):
        names = [str(t) for t in items
                 if isinstance(t, Token) and t.type in ("NAME", "SIGNED_NUMBER")]
        return "->".join(names)

    def arglist(self, items):
        out = []
        for x in items:
            if isinstance(x, list):
                out.extend(x)
            elif isinstance(x, Tree):
                if x.data == "arg":
                    out.extend(self.arglist(x.children))
                elif x.data == "edge_ref":
                    out.append(self.edge_ref(x.children))
                elif x.data == "raw":
                    out.append(str(x.children[0]))
                elif x.data == "number":
                    out.append(_nums(x.children)[0])
                else:
                    out.append(x)
            elif isinstance(x, Token):
                out.append(str(x))
            elif isinstance(x, (str, int, float)):
                out.append(x)
        return out

    def arg(self, items):
        # ?arg is inlined; return children for arglist to flatten, but we handle
        # edge_ref/number/raw via arglist's own recursion when called on the
        # wrapper. To keep it simple, return the processed value directly.
        if len(items) == 1:
            x = items[0]
            if isinstance(x, Tree):
                if x.data == "edge_ref":
                    return self.edge_ref(x.children)
                if x.data == "number":
                    return _nums(x.children)[0]
                if x.data == "raw":
                    return str(x.children[0])
            return x
        return list(items)

    def raw(self, items):
        return str(items[0])

    def lanes_p(self, items): return {"lanes": int(_nums(items)[0])}
    def speed_p(self, items): return {"speed_limit": _nums(items)[0]}
    def curve_p(self, items): return {"curve_radius": _nums(items)[0]}
    def slope_p(self, items): return {"slope": _nums(items)[0]}
    def slope_val(self, items):
        ns = _nums(items)
        return ns[0] if ns else 0.0
    def km_val(self, items):
        ns = _nums(items)
        return ns[0] if ns else 0.0
    def surface_p(self, items): return {"surface": _first_str(items)}
    # ---- v3 路面属性（宽容接收，存参供下游简化使用）----
    def damage_p(self, items):
        # v6 P2 口径对齐（与行式 parser 相同）：破损声明「15%」归一成 0.15（小数），
        # 否则 lark 的 surface.damage=15.0 与行式的 0.15 分歧，引擎损伤系数差 100×。
        ns = _nums(items)
        v = ns[0] if ns else 0.0
        if any(isinstance(x, Token) and x.type == "PCT" for x in items):
            v = v / 100.0
        elif v > 1.0:
            # 无 % 裸数字按百分比口径（决策落定，对齐行式 parser 恒 ÷100 语义）：
            # 「破损 15」→ 0.15——否则 lark surface.damage=15.0 被引擎 _clamp_damage
            # 钳成 1.0（μ×0.4 / 滚阻+50%），与行式 0.15（μ×0.91）差 100×。
            # 显式小数（破损 0.15 / 0.5，≤1.0）保持 0~1 原值，不被误除。
            v = v / 100.0
        return {"damage": v}
    def iri_p(self, items): return {"iri": _nums(items)[0] if _nums(items) else 0.0}
    def width_p(self, items): return {"width": _nums(items)[0] if _nums(items) else 0.0}
    def load_class_p(self, items):
        nm = _first_str([x for x in items if isinstance(x, (str, Token)) and not isinstance(x, Token) or isinstance(x, Token) and x.type == "NAME"])
        return {"load_class": nm or ""}
    def elevation_profile_p(self, items):
        pts = [x for x in items if isinstance(x, tuple)]
        return {"elevation_profile": [{"distance": p[0], "z": p[1]} for p in pts]}
    def profile_point(self, items):
        ns = _nums(items)
        return (ns[0], ns[1]) if len(ns) >= 2 else (0.0, 0.0)
    # ---- v6 路由/能耗块（深度整合：解析进 IR，随地图 JSON 下发供引擎消费）----
    def routing_block(self, items):
        rd = {"profile": "", "weights": {}, "vehicle_ref": "", "constraints": {}}
        for x in items:
            if isinstance(x, dict):
                if x.get("kind") == "routing_profile":
                    rd["profile"] = x.get("value") or rd["profile"]
                elif x.get("kind") == "routing_vehicle":
                    rd["vehicle_ref"] = x.get("value") or rd["vehicle_ref"]
                elif x.get("kind") == "routing_weights":
                    rd["weights"] = x.get("weights") or {}
        # 只落非空（避免空块覆盖复用基底的默认）
        if rd["profile"] or rd["weights"] or rd["vehicle_ref"]:
            self.ir.data["routing"] = rd
        return None
    def routing_field(self, items):
        # 关键字是命名终端（RFC_PROFILE/RFC_VEHICLE/RFC_WEIGHTS），Token 值即关键字——
        # earley 合并后相邻参数已被单值文法拆开，这里按令牌值匹配即可。
        tvals = [str(x) if isinstance(x, Token) else None for x in items]
        if "profile" in tvals:
            return {"kind": "routing_profile", "value": _first_str(items[tvals.index("profile") + 1:]) or ""}
        if "vehicle" in tvals or "vehicles" in tvals:
            i = tvals.index("vehicles") if "vehicles" in tvals else tvals.index("vehicle")
            return {"kind": "routing_vehicle", "value": _first_str(items[i + 1:]) or ""}
        # 权重：内联 routing_weight 子树被其方法变换为 (键, 值) 元组——
        # 直接收集 items 里的 tuple（earley 文法权重不再有独立 routing_weights 子规则）
        w = {}
        for x in items:
            if isinstance(x, tuple) and len(x) == 2:
                k, v = x
                if isinstance(k, Token):
                    k = str(k)
                if isinstance(k, str) and v is not None:
                    w[k] = v
        if w:
            return {"kind": "routing_weights", "weights": w}
        return None
    def routing_weights(self, items):
        w = {}
        for x in items:
            if isinstance(x, tuple) and len(x) == 2:
                w[x[0]] = x[1]
        return w
    def routing_weight(self, items):
        k = None
        for x in items:
            if isinstance(x, Token) and x.type == "NAME":
                k = str(x)
                break
        ns = _nums(items)
        return (k, ns[0]) if k and ns else None

    def energy_block(self, items):
        ed = {"report": "", "granularity": ""}
        for x in items:
            if isinstance(x, dict):
                if x.get("kind") == "energy_report":
                    ed["report"] = x.get("value") or ed["report"]
                elif x.get("kind") == "energy_granularity":
                    ed["granularity"] = x.get("value") or ed["granularity"]
        if ed["report"] or ed["granularity"]:
            self.ir.data["energy"] = ed
        return None
    def energy_field(self, items):
        # 关键字（report/granularity/per_segment）是字面量终端，按 token 值匹配
        tvals = [str(x) if isinstance(x, Token) else None for x in items]
        if "report" in tvals:
            return {"kind": "energy_report", "value": _first_str(items[tvals.index("report") + 1:]) or ""}
        if "granularity" in tvals:
            return {"kind": "energy_granularity", "value": _first_str(items[tvals.index("granularity") + 1:]) or ""}
        if "per_segment" in tvals:
            return {"kind": "energy_granularity", "value": "per_segment"}
        return None

    # ---- v6 P2：行人块 / 事件块（D2/E 组，与 parser.py 行式版对齐）----
    def pedestrians_block(self, items):
        pd = {"count": None, "density": None}
        for x in items:
            if isinstance(x, dict):
                if x.get("kind") == "ped_count" and x.get("value") is not None:
                    pd["count"] = int(x["value"])
                elif x.get("kind") == "ped_density" and x.get("value") is not None:
                    pd["density"] = float(x["value"])
        if pd["count"] is not None or pd["density"] is not None:
            self.ir.data["pedestrians"] = pd
        return None

    def pedestrian_field(self, items):
        tvals = [str(x) if isinstance(x, Token) else None for x in items]
        if "count" in tvals:
            v = _nums(items)
            return {"kind": "ped_count", "value": v[0] if v else None}
        if "density" in tvals:
            v = _nums(items)
            return {"kind": "ped_density", "value": v[0] if v else None}
        return None

    def events_block(self, items):
        evs = [x for x in items if isinstance(x, dict)]
        if evs:
            for i, e in enumerate(evs, 1):
                e.setdefault("event_id", f"tw_ev_{i}")
            self.ir.data.setdefault("events", []).extend(evs)
        return None

    # ---- v6 D1：区域天气块（语义与 parser.py 行式版一致）----
    def weather_regions_block(self, items):
        for x in items:
            if isinstance(x, dict):
                self.ir.data.setdefault("weather_regions", []).append(x)
        return None

    def weather_region_stmt(self, items):
        # 宽容式：coord 规则引用被 transform 成 {"x","y"} dict 直接放入 children
        # （earley）；polygon/weather/intensity 子树被 ?wr_body 内联成直接子项。
        # 逐层展平到 token/dict 层（还要解开带括号分组与匿名分组产出的 list——
        # 否则 NAME/WEATHER/polygon 藏在 list 里收不到）。
        def _flatten(xs):
            out = []
            for x in xs:
                if isinstance(x, Tree):
                    out.extend(_flatten(x.children))
                elif isinstance(x, list):
                    out.extend(_flatten(x))
                else:
                    out.append(x)
            return out

        flat = _flatten(items)
        rid = None
        polygon = []
        weather = None
        intensity = 1.0
        for x in flat:
            if isinstance(x, dict) and "x" in x and "y" in x:
                polygon.append((x["x"], x["y"]))
            elif isinstance(x, Token):
                if x.type == "WEATHER":
                    weather = str(x)
                elif x.type == "NUMBER" and intensity == 1.0:
                    pass  # 数值兜底不覆盖（强度关键字在后面单独取）
                elif x.type == "NAME" and rid is None:
                    rid = str(x)
        # 强度：定位 wr_intensity 子树（内联后是 items 直接子项之一）取首个数值（0~1 钳制）
        for x in items:
            if isinstance(x, Tree) and x.data == "wr_intensity":
                n = _nums(x.children)
                if n:
                    intensity = max(0.0, min(1.0, float(n[0])))
        if not polygon or not weather:
            return None  # 缺多边形/天气 → 忽略（与行式版告警语义一致；缺省不落点）
        return {"id": rid or f"wr_{len(self.ir.data.get('weather_regions', [])) + 1}",
                "polygon": polygon, "weather": weather, "layer": 0,
                "intensity": intensity}

    def event_stmt(self, items):
        """事件 {} 单条：类型 + 字段（位置/时刻/时长/车道）。"""
        tvals = [str(x) if isinstance(x, Token) else None for x in items]
        etype = next((t for t in tvals
                      if t in ("blasting", "construction", "rockfall",
                               "lane_blockage", "pedestrian_crossing", "crossing")), None)
        if etype is None:
            return None
        ev = {"type": _EVENT_TYPE_LARK.get(etype, "road_closure")}
        for x in items:
            if isinstance(x, dict):
                ev.update(x)
        return ev

    def event_field(self, items):
        # 返回平铺字段（无 kind 包装），event_stmt 直接 ev.update(x) 合并
        tvals = [str(x) if isinstance(x, Token) else None for x in items]
        if "location" in tvals:
            # 位置 后可跟 边引用（A->B）或 节点名；edge_ref/NAME 以子项形式出现
            for x in items:
                if isinstance(x, Tree) and x.data == "edge_ref":
                    ch = [str(c) if isinstance(c, Token) else c for c in x.children]
                    if len(ch) >= 3:  # from '->' to
                        return {"edge_from": str(ch[0]), "edge_to": str(ch[2])}
                elif isinstance(x, Token) and x.type == "NAME":
                    return {"node_id": str(x)}
            return None
        if "time_sec" in tvals:
            v = _nums(items)
            return {"trigger_time": v[0] if v else 0.0}
        if "duration" in tvals:
            v = _nums(items)
            return {"duration": v[0] if v else 60.0}
        if "lanes" in tvals:
            v = _nums(items)
            return {"lanes": int(v[0]) if v else 2}
        return None

    # ---- v6 P1 兼容：行式路由/能耗（向后兼容 v3 旧写法，无大括号）----
    def routing_line(self, items):
        # 与 routing_block 同一消费：profile/vehicle/vehicles
        rd = {"profile": "", "weights": {}, "vehicle_ref": "", "constraints": {}}
        for x in items:
            if isinstance(x, dict):
                if x.get("kind") == "routing_profile":
                    rd["profile"] = x.get("value") or rd["profile"]
                elif x.get("kind") == "routing_vehicle":
                    rd["vehicle_ref"] = x.get("value") or rd["vehicle_ref"]
        if rd["profile"] or rd["vehicle_ref"] or rd["weights"]:
            self.ir.data["routing"] = rd
        return None

    def routing_line_tail(self, items):
        # v6 P2.1：关键字已命名终端化（RL_PROFILE/RL_VEHICLE）——lark 默认丢弃
        # 匿名字面量，tail 里只剩 ROUTE_KIND/NAME，键分支全部落空（vehicle_ref 空、
        # profile 靠 ROUTE_KIND 兜底）。现在关键字以 Token 形式保留在 items 里，
        # 按 str 值匹配键、取键后首个非关键字 token 为值。
        tvals = [str(x) if isinstance(x, Token) else None for x in items]
        if "profile" in tvals:
            return {"kind": "routing_profile", "value": _first_str(items[tvals.index("profile") + 1:]) or ""}
        if "vehicle" in tvals or "vehicles" in tvals:
            i = tvals.index("vehicles") if "vehicles" in tvals else tvals.index("vehicle")
            return {"kind": "routing_vehicle", "value": _first_str(items[i + 1:]) or ""}
        # v6 P2 语义归一（与行式对齐）：裸路由动作词（economy/fastest/comfortable/safest/custom）
        # 单独出现 = 路由 profile——「路由 最省 车辆 X」的「最省」在此收住。
        if tvals and tvals[0] in ("economy", "fastest", "comfortable", "safest", "custom"):
            return {"kind": "routing_profile", "value": tvals[0]}
        return None

    def energy_line(self, items):
        ed = {"report": "", "granularity": ""}
        for x in items:
            if isinstance(x, dict):
                if x.get("kind") == "energy_report":
                    _rv = x.get("value") or ""
                    # v6 P2 语义归一（与行式 + api 桥接口径一致）：report 动作词，
                    # 「报告/开启/启用/on」→ on，「关闭/禁用/off」→ off。
                    if _rv in ("report", "on", "开启", "启用"):
                        _rv = "on"
                    elif _rv in ("off", "关闭", "禁用"):
                        _rv = "off"
                    ed["report"] = _rv
                elif x.get("kind") == "energy_granularity":
                    ed["granularity"] = x.get("value") or ed["granularity"]
                elif x.get("kind") == "energy_both":
                    # v6 P2 语义归一收尾：tail 的「report + 每段」合并返回（裸 report
                    # 默认 on）——此处拆回两个槽，否则「能耗 报告 每段」整行落空。
                    _vals = x.get("value") or []
                    if len(_vals) >= 2:
                        ed["report"] = _vals[0]
                        ed["granularity"] = _vals[1]
        if ed["report"] or ed["granularity"]:
            self.ir.data["energy"] = ed
        return None

    def energy_line_tail(self, items):
        tvals = [str(x) if isinstance(x, Token) else None for x in items]
        # v6 P2 语义归一：裸「report」（无值）= 开启能耗分析（report 动作词默认 on）；
        # 「report 每段/per_segment」→ report=on + granularity=per_segment（值归一到
        # granularity 槽，不再把 per_segment 当 report 值）。
        if "report" in tvals:
            _rest = tvals[tvals.index("report") + 1:]
            if _rest and _rest[0] == "per_segment":
                return {"kind": "energy_both", "value": ["on", "per_segment"]}
            return {"kind": "energy_report", "value": _rest[0] if _rest else "on"}
        if "granularity" in tvals:
            return {"kind": "energy_granularity", "value": _first_str(items[tvals.index("granularity") + 1:]) or ""}
        if "per_segment" in tvals:
            return {"kind": "energy_granularity", "value": "per_segment"}
        return None

    def zone_stmt(self, items):
        zid, ztype, edge, km, params = None, None, None, None, {}
        for x in items:
            if isinstance(x, dict):
                if x.get("type") == "speed_limit":
                    ztype, edge, km = "speed_limit", x["edge"], x.get("km")
                    params["limit_kmh"] = x["limit_kmh"]
                elif x.get("type") == "work_zone":
                    ztype, edge, km = "work_zone", x["edge"], x.get("km")
                    params["capacity"] = x["capacity"]
            elif isinstance(x, Token) and x.type == "NAME" and zid is None:
                zid = str(x)
        zone = {"id": zid or "?", "type": ztype, "edge": edge}
        if km is not None:
            zone["km"] = km
        zone["params"] = params
        self.ir.zones.append(zone)
        return None

    def speed_limit_body(self, items):
        edge = _first_str([x for x in items if isinstance(x, str) and "->" in x])
        ns = _nums(items)
        km = ns[0] if ns else None
        lim = ns[-1] if ns else None
        return {"type": "speed_limit", "edge": edge, "km": km, "limit_kmh": lim}
    def work_zone_body(self, items):
        edge = _first_str([x for x in items if isinstance(x, str) and "->" in x])
        ns = _nums(items)
        km = ns[0] if ns else None
        cap = ns[-1] if ns else None
        return {"type": "work_zone", "edge": edge, "km": km, "capacity": cap}

    # ---- 天气 ----
    def time_range(self, items):
        ns = _nums(items)
        unit = "sec"
        for x in items:
            if isinstance(x, Token) and x.type == "UNIT":
                unit = str(x)
        return (ns[0], ns[1], unit)
    def weather_stmt(self, items):
        rng, wtype = None, None
        for x in items:
            if isinstance(x, tuple):
                rng = x
            elif isinstance(x, Token) and x.type == "WEATHER":
                wtype = str(x)
        if rng is None:
            return None
        start, end, unit = rng
        if unit == "min":
            start, end = start * 60, end * 60
        if end <= start:
            self.ir.data.setdefault("warnings", []).append(
                f"天气剧本时段结束须大于开始: {start}-{end}")
            return None
        self.ir.data["weather"]["script"].append(
            {"start": start, "end": end, "type": wtype})
        return None

    # ---- 车辆 ----
    def vehicle_stmt(self, items):
        cnt, typ = None, None
        phys = {}
        brand = None
        flat = []
        for x in items:
            if isinstance(x, Tree) and x.data == "phys_block":
                flat.extend(x.children)
            else:
                flat.append(x)
        i = 0
        while i < len(flat):
            x = flat[i]
            if isinstance(x, (int, float)):
                if cnt is None:
                    cnt = x
            elif isinstance(x, Token) and str(x) == "brand":
                # v6 P1：品牌绑定（品牌 型号ID）——紧跟的 NAME 是型号 id
                if i + 1 < len(flat) and isinstance(flat[i + 1], Token) and flat[i + 1].type == "NAME":
                    brand = str(flat[i + 1])
                    i += 1
            elif isinstance(x, Token) and x.type == "NAME":
                if typ is None:
                    typ = str(x)
            elif isinstance(x, Tree) and x.data == "phys_pair":
                k = str(x.children[0])
                v = _nums(x.children[1:])[0]
                phys[k] = v
            i += 1
        if cnt and typ:
            entry = {"type": typ, "count": int(cnt)}
            if brand:
                entry["brand_model"] = brand
            if phys:
                entry["physics"] = phys
            self.ir.data["vehicles"].append(entry)
        return None

    def phys_pair(self, items):
        k = None
        val = None
        for x in items:
            if isinstance(x, Token) and x.type == "PHYS_KEY":
                k = str(x)
            elif isinstance(x, (int, float, Token)) and _nums([x]):
                val = _nums([x])[0]
        # v6 C 组对齐：mass/max_gross_mass 声明单位是「吨」，行式解析器在 IR 层即
        # ×1000 转 kg；lark 保持同口径，否则双解析器 IR 不一致 → api 下发车辆计划
        # 质量错 1000 倍（引擎 Vehicle.mass 单位是 kg）。
        if k in ("mass", "max_gross_mass") and val is not None:
            val = float(val) * 1000.0
        return Tree("phys_pair", [k, val]) if k else None

    # ---- 规则 ----
    def trig_time(self, items):
        return {"type": "time", "at_sec": _nums(items)[0]}
    def trig_random(self, items):
        return {"type": "random", "prob_per_min": _nums(items)[0]}
    def trig_crossing(self, items):
        return {"type": "crossing", "location": ""}
    def trig_weather(self, items):
        w = None
        for x in items:
            if isinstance(x, Token) and x.type == "WEATHER":
                w = str(x)
        return {"type": "weather_link", "weather": w}

    def call(self, items):
        name = None
        args = []
        for x in items:
            if isinstance(x, Token) and x.type == "ACTION_NAME":
                name = str(x)
            elif isinstance(x, list):
                args.extend(x)
            elif isinstance(x, (str, int, float)):
                args.append(x)
        return self._build_action(name, args)

    def road_speed_override(self, items):
        edge = _first_str([x for x in items if isinstance(x, str) and "->" in x])
        ns = _nums(items)
        lim = ns[-1] if ns else None
        return {"type": "speed_limit", "edge": edge, "limit_kmh": lim}

    def rule_stmt(self, items):
        trig, act = None, None
        for x in items:
            if isinstance(x, dict):
                t = x.get("type")
                if t in _TRIGGER_TYPES and trig is None:
                    trig = x
                elif t in _ACTION_TYPES and act is None:
                    act = x
        if trig is None:
            return None
        if trig["type"] == "crossing" and (act is None or act["type"] != "pedestrian"):
            act = {"type": "pedestrian", "location": trig.get("location", "")}
        if act is None:
            self.ir.data.setdefault("warnings", []).append("规则缺少动作，已跳过")
            return None
        self.ir.data["rules"].append({
            "id": f"rule_{len(self.ir.data['rules']) + 1}",
            "trigger": trig, "action": act})
        return None

    # ---- 表达式（编译期求值，返回 float）----
    def number(self, items): return float(items[0])
    def var(self, items):
        n = str(items[0])
        if n not in self.scope:
            raise TwError(f"变量 '{n}' 未定义（进阶语法需先 '设 {n} = ...'）")
        return self.scope[n]
    def add(self, items):
        ns = _nums(items); return ns[0] + ns[1]
    def sub(self, items):
        ns = _nums(items); return ns[0] - ns[1]
    def mul(self, items):
        ns = _nums(items); return ns[0] * ns[1]
    def div(self, items):
        ns = _nums(items); return ns[0] / ns[1]
    def sum(self, items): return items[0]
    def product(self, items): return items[0]
    def expr(self, items): return items[0]
    def atom(self, items): return items[0]
    def number(self, items): return float(items[0])
    def raw(self, items): return str(items[0])

    # ---- 动作构造 ----
    def _build_action(self, name, args):
        if name == "close_road":
            edge = _as_str(args[0]) if args else ""
            dur = args[1] if len(args) > 1 else 0
            return {"type": "close_road", "edge": edge, "duration_sec": float(dur or 0)}
        if name == "accident":
            edge = _as_str(args[0]) if args else ""
            lanes = args[1] if len(args) > 1 else 1
            return {"type": "accident", "edge": edge, "lanes_blocked": int(lanes or 1)}
        if name == "pedestrian":
            loc = _as_str(args[0]) if args else ""
            return {"type": "pedestrian", "location": loc}
        if name == "schedule":
            return {"type": "schedule", "args": [_as_str(a) for a in args]}
        if name == "speed_limit":
            edge = _as_str(args[0]) if args else ""
            lim = args[1] if len(args) > 1 else 0
            return {"type": "speed_limit", "edge": edge, "limit_kmh": float(lim or 0)}
        return {"type": name, "args": [_as_str(a) for a in args]}


# ---------- 复用（include / extends）合并 ----------

def _merge(base: ScenarioIR, over: ScenarioIR) -> ScenarioIR:
    """把 over 合并到 base 之上，over 胜出（id 冲突时覆盖，规则追加）"""
    import copy
    d = copy.deepcopy(base.data)

    def _by_id(lst):
        return {x.get("id"): x for x in lst}
    bn, on = _by_id(d["map"]["nodes"]), _by_id(over.data["map"]["nodes"])
    on.update(bn); d["map"]["nodes"] = list(on.values())
    be, oe = _by_id(d["map"]["edges"]), _by_id(over.data["map"]["edges"])
    oe.update(be); d["map"]["edges"] = list(oe.values())
    bz, oz = _by_id(d["map"]["zones"]), _by_id(over.data["map"]["zones"])
    oz.update(bz); d["map"]["zones"] = list(oz.values())

    if over.data["weather"]["script"]:
        d["weather"]["script"] = over.data["weather"]["script"]

    bv = {v["type"]: v for v in d["vehicles"]}
    for v in over.data["vehicles"]:
        bv[v["type"]] = v
    d["vehicles"] = list(bv.values())

    d["rules"] = d["rules"] + over.data["rules"]

    if over.data["meta"]["name"]:
        d["meta"]["name"] = over.data["meta"]["name"]
    if over.data["meta"]["seed"] is not None:
        d["meta"]["seed"] = over.data["meta"]["seed"]
    return ScenarioIR(d)


# ---------- 解析入口 ----------

def _collect_lets(tree: Tree, scope: dict):
    for node in tree.iter_subtrees():
        if node.data == "let_stmt":
            name, expr_node = None, None
            for c in node.children:
                if isinstance(c, Token) and c.type == "NAME" and name is None:
                    name = str(c)
                elif isinstance(c, Tree):
                    expr_node = c
            if name and expr_node:
                try:
                    scope[name] = _eval_expr(expr_node, scope)
                except TwError:
                    pass  # 校验阶段会再报


def _collect_includes(tree: Tree, base_dir: str, seen: set):
    bases = []
    for node in tree.iter_subtrees():
        if node.data in ("include_stmt", "extends_stmt"):
            path = None
            for c in node.children:
                if isinstance(c, Token) and c.type == "STRING":
                    path = str(c).strip('"')
            if not path:
                continue
            full = path if os.path.isabs(path) else os.path.join(base_dir, path)
            if full in seen or not os.path.exists(full):
                if not os.path.exists(full):
                    # 记录找不到的引用，但不阻断
                    pass
                continue
            seen.add(full)
            with open(full, encoding="utf-8") as f:
                bases.append(_parse_advanced(f.read(),
                                            os.path.dirname(full), seen))
    if not bases:
        return None
    combined = bases[0]
    for b in bases[1:]:
        combined = _merge(combined, b)
    return combined


_SCENARIO_NAME_RE = re.compile(
    r"^\s*(?:scenario|场景)\s+([^\s{]+)"  # 「场景 区域天气v6 {」→ 名字 = 首个非空 token
)


def _extract_scenario_name(text: str) -> str | None:
    """翻译前按原文抓场景名（translate_keywords 的汉字边界护不住「区域天气v6」这类
    关键字短语+非汉字后缀的名字——会把它拆成 weather_regions v6 两个 token，
    lark 的 scenario NAME 吃不下）。返回原文名字；找不到返回 None。"""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = _SCENARIO_NAME_RE.match(line)
        return m.group(1) if m else None
    return None


def _replace_scenario_name(src: str, name: str) -> str:
    """把已翻译 src 里 scenario 与 { 之间的片段整体替换为原文名字（单 token），
    之后 lark 的 scenario NAME 直接吃到原文。名字无污染时替换为自身，无害。"""
    return re.sub(r"(?m)(^\s*scenario\s+)[^{]+(\{)",
                  lambda m: f"{m.group(1)}{name}{m.group(2)}", src, count=1)


def _parse_advanced(text: str, base_dir: str, seen: set | None = None):
    seen = seen if seen is not None else set()
    _name = _extract_scenario_name(text)
    lines = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(normalize(_zh_to_en_line(line)))
    src = "\n".join(lines)
    if _name:
        src = _replace_scenario_name(src, _name)
    try:
        tree = _PARSER.parse(src)
    except LarkError as e:
        raise TwError(f".tw 进阶语法解析失败: {e}") from None

    base_ir = _collect_includes(tree, base_dir, seen)

    ir = ScenarioIR(_new())
    scope: dict = {}
    _collect_lets(tree, scope)
    tr = TwTransform(ir, scope)
    tr.transform(tree)

    if base_ir is not None:
        ir = _merge(base_ir, ir)  # 当前场景胜出
    return ir


def parse_tw_advanced(text: str, base_dir: str | None = None) -> ScenarioIR:
    return _parse_advanced(text, base_dir or os.getcwd())


def parse_file_advanced(path: str) -> ScenarioIR:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    base_dir = os.path.dirname(os.path.abspath(path))
    return _parse_advanced(text, base_dir)

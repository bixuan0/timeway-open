"""
ir.py — ScenarioIR 数据模型 + 校验器（语言无关，唯一事实源）
==============================================================
对齐 v2 规范 §2 的 Schema：
  meta:     {name, seed, desc}
  map:      {nodes[], edges[], zones[]}
  weather:  {script[], transition[]}   （script 优先，transition 可选）
  vehicles: [{type, count}]
  rules:    [{id, trigger, action}]
"""
from __future__ import annotations

import json
from typing import Any

SCHEMA_VERSION = "1.2"  # v4 P1：车辆声明 physics 块 + 边级 mu_override

# v5 P0：.tw 车辆物理声明白名单（IR 层放行 8 键，全部被引擎消费）：
#   mass/max_gross_mass/load_factor/brake_efficiency 为既有 4 键；
#   v5 新增 cda/rolling_resistance/max_power_kw 直接注入车辆动力学缓存，
#   新增 cg_height_m（质心高度）用于侧翻阈值（弯道横向限速取 μ·g 与 g·B/(2h) 较小值）。
_VEHICLE_PHYSICS_KEYS = {
    "mass", "max_gross_mass", "load_factor", "brake_efficiency",
    "cda", "rolling_resistance", "max_power_kw", "cg_height_m",
    "overload",   # v5 P0：派生标记（载重>1.0 时由 parser 写入，非输入键）
}
_VEHICLE_PHYSICS_UNLINKED = frozenset()  # v5：cda/rolling_resistance 已接入引擎，无未连接键

# ---- 合法取值枚举（校验用）----
SURFACE_TYPES = {"gravel", "mud", "steel", "asphalt", "碎石", "泥结", "钢板", "沥青"}
ROAD_KINDS = {"road", "intersection", "ramp", "elevated", "tunnel", "bridge", "landscape"}
ZONE_TYPES = {"speed_limit", "work_zone"}
# P0-1 修复：主导词表 = config.WEATHER_TYPES 中文 9 词（WeatherSystem/router 消费键），
# 英文别名仅在校验层放行（parser 会归一为中文键；手工构造 IR 时也避免误报）。
WEATHER_TYPES = {"晴", "多云", "小雨", "大雨", "雷雨", "雾", "夜间", "雪", "冰雹",
                 "clear", "sunny", "cloudy", "overcast", "drizzle", "light_rain",
                 "rain", "moderate_rain", "heavy_rain", "storm", "thunderstorm",
                 "fog", "mist", "night", "snow", "sleet", "hail", "ice",
                 "暴雨", "团雾", "结冰"}  # 中文别名（parser 归一为中文键前的兼容）
RULE_TRIGGERS = {"time", "random", "crossing", "schedule", "weather_link"}
RULE_ACTIONS = {"close_road", "accident", "speed_limit", "schedule", "pedestrian"}


def _new() -> dict:
    """空 IR 骨架"""
    return {
        "schema_version": SCHEMA_VERSION,
        # v6 P0：与引擎深度融合——meta.runs（仿真次数，多次运行聚合收敛；
        # 由 api/sim_worker 循环消费并聚合报告）、meta.n_vehicles（可选车辆总数覆盖，
        # 缺省取 vehicles 块 count 之和，真正驱动引擎车队规模）
        "meta": {"name": "", "seed": None, "desc": "", "runs": 1, "n_vehicles": None,
                  # v6 P2：引擎参数（B 组）——写 .tw 即配引擎，缺省 None 表示不覆盖 API 参数
                  "av_ratio": None, "time_slot": None, "temperature": None,
                  "wind_speed": None, "wind_mode": None, "output_types": None},
         "map": {"nodes": [], "edges": [], "zones": [], "elevation": None},
        "weather": {"script": [], "transition": []},
        "vehicles": [],
        "rules": [],
        # ---- v3 预留（P0 留口子；P2 能耗 / P3 多代价路由时消费）----
        "routing": {"profile": "", "weights": {}, "vehicle_ref": "", "constraints": {}},
        "energy": {"report": "", "granularity": ""},
        # ---- v6 P2：行人块（D2）/ 事件块（E）——引擎消费点已就绪（PedestrianSystem
        # density_mult、SCENARIO_EVENTS 注入），这里只补 IR 骨架让双解析器统一落位。----
        "pedestrians": {"count": None, "density": None},
        "events": [],
        # 解析期非致命问题（无法识别/暂不支持的语句），供上层诊断，
        # 不影响校验通过。默认空列表 = 解析无警告。
        "warnings": [],
    }


class ScenarioIR:
    """薄封装：持有 dict，提供便捷访问 + 校验"""

    def __init__(self, data: dict | None = None):
        self.data = data if data is not None else _new()

    # ---- 便捷访问 ----
    @property
    def name(self) -> str:
        return self.data.get("meta", {}).get("name", "")

    @property
    def nodes(self) -> list:
        return self.data.get("map", {}).get("nodes", [])

    @property
    def edges(self) -> list:
        return self.data.get("map", {}).get("edges", [])

    @property
    def zones(self) -> list:
        return self.data.get("map", {}).get("zones", [])

    @property
    def warnings(self) -> list:
        """解析期非致命警告（无法识别/暂不支持的语句），空列表 = 无警告"""
        return self.data.get("warnings", [])

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.data, ensure_ascii=False, indent=indent)

    def __repr__(self):
        return f"<ScenarioIR {self.name} nodes={len(self.nodes)} edges={len(self.edges)} zones={len(self.zones)} rules={len(self.data.get('rules', []))}>"


# ================= 校验器 =================

def validate_ir(ir: ScenarioIR | dict) -> list[str]:
    """返回警告/错误列表；空列表 = 通过

    v5 P1：兼容 IR 对象与裸字典两种输入（`validate_ir(ir.data)` 也合法）——
    避免调用侧手抖传 dict 时 AttributeError（坏输入不崩原则）。
    """
    issues: list[str] = []
    d = ir.data if hasattr(ir, "data") else ir

    # meta
    if not d.get("meta", {}).get("name"):
        issues.append("[meta] 场景缺少名称（场景 名字 {...}）")
    _runs = d.get("meta", {}).get("runs")
    if _runs is not None:
        if not isinstance(_runs, (int, float)) or int(_runs) < 1:
            issues.append("[meta] 仿真次数 runs 必须是 >=1 的整数（运行 N 次）")
    _nv = d.get("meta", {}).get("n_vehicles")
    if _nv is not None:
        if not isinstance(_nv, (int, float)) or int(_nv) < 1:
            issues.append("[meta] 车辆总数 n_vehicles 必须是 >=1 的整数（车辆总数 N 辆）")

    # 节点
    node_ids = set()
    for i, n in enumerate(d.get("map", {}).get("nodes", [])):
        if not n.get("id"):
            issues.append(f"[map.nodes#{i}] 节点缺少 id")
        else:
            node_ids.add(n["id"])
        if not isinstance(n.get("x"), (int, float)) or not isinstance(n.get("y"), (int, float)):
            issues.append(f"[map.nodes#{i}] 节点 {n.get('id')} 坐标必须是数字")
        if n.get("z") is not None and not isinstance(n.get("z"), (int, float)):
            issues.append(f"[map.nodes#{i}] 节点 {n.get('id')} 高程 z 必须是数字（v3 可选）")

    # 边：端点必须指向存在的节点
    for i, e in enumerate(d.get("map", {}).get("edges", [])):
        if e.get("from") not in node_ids:
            issues.append(f"[map.edges#{i}] 道路 {e.get('id')} 起点 '{e.get('from')}' 未定义节点")
        if e.get("to") not in node_ids:
            issues.append(f"[map.edges#{i}] 道路 {e.get('id')} 终点 '{e.get('to')}' 未定义节点")
        if e.get("lanes") is not None and e["lanes"] < 1:
            issues.append(f"[map.edges#{i}] 车道数必须 >=1")
        if e.get("kind") not in (None, *ROAD_KINDS):
            issues.append(f"[map.edges#{i}] 未知道路种类 '{e.get('kind')}'")
        # 路面：v2 字符串材质 或 v3 属性字典
        s = e.get("surface")
        if isinstance(s, str) and s and s not in SURFACE_TYPES:
            issues.append(f"[map.edges#{i}] 未知路面 '{s}'（可选: 碎石/泥结/钢板/沥青）")
        elif isinstance(s, dict):
            sm = s.get("material")
            if sm and sm not in SURFACE_TYPES:
                issues.append(f"[map.edges#{i}] 未知路面材质 '{sm}'（可选: 碎石/泥结/钢板/沥青）")
            if s.get("damage") is not None:
                try:
                    dmg = float(s["damage"])
                    if not 0 <= dmg <= 1:
                        issues.append(f"[map.edges#{i}] 破损等级 damage 须在 0~1")
                except (TypeError, ValueError):
                    issues.append(f"[map.edges#{i}] 破损等级 damage 必须是数字")
            for attr in ("iri", "width", "mu_override"):
                if s.get(attr) is not None and not isinstance(s.get(attr), (int, float)):
                    issues.append(f"[map.edges#{i}] 路面属性 {attr} 必须是数字")
        ep = e.get("elevation_profile")
        if ep is not None:
            if not isinstance(ep, list) or not ep:
                issues.append(f"[map.edges#{i}] 高程剖面 elevation_profile 必须是非空列表")
            else:
                for pt in ep:
                    if not isinstance(pt, dict) or not isinstance(pt.get("distance"), (int, float)) \
                            or not isinstance(pt.get("z"), (int, float)):
                        issues.append(f"[map.edges#{i}] 高程剖面点必须含数字 distance 与 z")
                        break

    # 区域
    for i, z in enumerate(d.get("map", {}).get("zones", [])):
        if z.get("type") not in ZONE_TYPES:
            issues.append(f"[map.zones#{i}] 未知区域类型 '{z.get('type')}'（可选: speed_limit/work_zone）")

    # 天气剧本
    for i, seg in enumerate(d.get("weather", {}).get("script", [])):
        if seg.get("start") is None or seg.get("end") is None:
            issues.append(f"[weather.script#{i}] 时段缺少 start/end")
        elif seg["end"] <= seg["start"]:
            issues.append(f"[weather.script#{i}] 时段 end 必须大于 start")
        if seg.get("type") not in WEATHER_TYPES:
            issues.append(f"[weather.script#{i}] 未知天气 '{seg.get('type')}'（可选: 晴/小雨/暴雨/团雾/结冰）")

    # 车辆
    for i, v in enumerate(d.get("vehicles", [])):
        if not v.get("type"):
            issues.append(f"[vehicles#{i}] 车型缺少 type")
        if not isinstance(v.get("count"), int) or v["count"] < 1:
            issues.append(f"[vehicles#{i}] 数量必须是 >=1 的整数")
        # v4 物理声明块：只允许已知键，数值须合法（越界在 parser 层已钳制，此处仅提示）
        _ph = v.get("physics")
        if _ph is not None:
            for _k in _ph:
                if _k not in _VEHICLE_PHYSICS_KEYS:
                    issues.append(f"[vehicles#{i}] 未知物理键 '{_k}'（可选: mass/max_gross_mass/load_factor/brake_efficiency/cda/rolling_resistance/max_power_kw/cg_height_m）")
                elif _k in _VEHICLE_PHYSICS_UNLINKED:
                    issues.append(f"[vehicles#{i}] 物理键 '{_k}' 已声明但引擎当前由品牌链覆盖，声明暂不生效")
            _m = _ph.get("mass")
            if _m is not None and not isinstance(_m, (int, float)) or (_m is not None and float(_m) <= 0):
                issues.append(f"[vehicles#{i}] 质量必须是正数")
            _mg = _ph.get("max_gross_mass")
            if _mg is not None and not isinstance(_mg, (int, float)) or (_mg is not None and float(_mg) <= 0):
                issues.append(f"[vehicles#{i}] 最大总质量必须是正数")
            _lf = _ph.get("load_factor")
            if _lf is not None and not (isinstance(_lf, (int, float)) and 0 <= _lf <= 1.5):
                issues.append(f"[vehicles#{i}] 载重必须是 0~1.5 的比例（>1.0 为超载）")
            elif _lf is not None and float(_lf) > 1.0:
                # v6 P0：超载是「已放开的合法场景」（v5 放开 0~1.5，水电站超载常态）——
                # 只提示不阻断，否则 .tw 场景写 载重 1.2 会被误拦，应用永远 400。
                _warn_list = d.setdefault("warnings", [])
                _warn_list.append(f"[vehicles#{i}] 载重 {_lf} > 1.0 → 标记超载 overload（侧翻/制动风险提高）")
            _be = _ph.get("brake_efficiency")
            if _be is not None and not (isinstance(_be, (int, float)) and 0 <= _be <= 1):
                issues.append(f"[vehicles#{i}] 制动效率必须是 0~1 的比例")
            _kp = _ph.get("max_power_kw")
            if _kp is not None and not (isinstance(_kp, (int, float)) and float(_kp) > 0):
                issues.append(f"[vehicles#{i}] 发动机功率 max_power_kw 必须是正数（kw）")
            _cg = _ph.get("cg_height_m")
            if _cg is not None and not (isinstance(_cg, (int, float)) and float(_cg) > 0):
                issues.append(f"[vehicles#{i}] 质心高度 cg_height_m 必须是正数（m）")
            _cda = _ph.get("cda")
            if _cda is not None and not (isinstance(_cda, (int, float)) and float(_cda) >= 0):
                issues.append(f"[vehicles#{i}] 风阻 CdA 必须 >=0（m²）")
            _rr = _ph.get("rolling_resistance")
            if _rr is not None and not (isinstance(_rr, (int, float)) and float(_rr) >= 0):
                issues.append(f"[vehicles#{i}] 滚动阻力系数必须 >=0")

    # 规则
    for i, r in enumerate(d.get("rules", [])):
        t = r.get("trigger", {})
        if t.get("type") not in RULE_TRIGGERS:
            issues.append(f"[rules#{i}] 未知触发类型 '{t.get('type')}'（可选: time/random/crossing/schedule/weather_link）")
        a = r.get("action", {})
        if a.get("type") not in RULE_ACTIONS:
            issues.append(f"[rules#{i}] 未知动作类型 '{a.get('type')}'（可选: close_road/accident/speed_limit/schedule）")

    # 路由（v3 可选；缺省空 = v2 贪心距离路由）
    r = d.get("routing") or {}
    if r.get("profile") and r["profile"] not in ("fastest", "economy", "comfortable", "safest", "custom"):
        issues.append(f"[routing] 未知路由模式 '{r.get('profile')}'（可选: fastest/economy/comfortable/safest/custom）")
    return issues


def fill_v3_defaults(ir: ScenarioIR) -> ScenarioIR:
    """v3 P0：给旧 v2 IR 自动补 v3 缺省值（下游可直接读 z / surface 字典）。

    - 节点 z 缺省补 0（平面地图）
    - 边 surface 若为 v2 字符串材质 → 提升为 {"material": s}
    - 已存在的 v3 字段不动（显式优先）
    - 返回同一 ir 对象（原地补齐）
    """
    m = ir.data.setdefault("map", {})
    for n in m.get("nodes", []):
        n.setdefault("z", 0.0)
    for e in m.get("edges", []):
        s = e.get("surface")
        if isinstance(s, str):
            e["surface"] = {"material": s}
    return ir

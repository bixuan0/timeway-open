# -*- coding: utf-8 -*-
"""
tw_to_xosc.py — 时途 .tw 场景语言 → ASAM OpenSCENARIO 1.2 导出器
================================================================

目标：把 .tw DSL 写出的微观交通场景，转换成业界标准 ASAM OpenSCENARIO
1.2（XML, .xosc）格式，并附带生成一份最小可用的 OpenDRIVE 1.7（.xodr）
路网文件，使场景可在 esmini / SUMO 等开源回放工具中加载验证。

为何是 1.2 而不是 2.x：
  ASAM OpenSCENARIO 1.x 是 XML 场景格式，被 esmini / SUMO / 商业回放器
  广泛支持；2.x（2026 年新增的 DSL 标准）与 .tw 同为"领域特定语言描述
  动态场景"的思路，但当前回放工具链以 1.x 为主。本导出器面向"可被工具
  加载验证"，故落地 1.2。

映射覆盖（详见 convert() 返回的 coverage 字典，并打印到 stdout）：
  ✅ 节点 / 道路几何（直线近似）→ OpenDRIVE 路网
  ✅ 车辆类型 + 物理参数（质量/功率/质心…）→ OSC Vehicle（含 BoundingBox/Performance）
  ✅ 天气剧本（晴/雨/雾…）→ OSC EnvironmentAction（按时间切换）
  ✅ 规则：时间触发封路/事故 → SpeedAction(0)；人员横穿 → Pedestrian 实体 + 行走
  ⚠️ 随机触发（随机(p)）→ 确定性时间触发（按 seed 采样），OSC 1.2 无原生概率触发
  ⚠️ 坡度/弯道/路面/高程剖面 → 路网几何简化（直线、限速取自 speed_limit），属性记录不展开
  ❌ 路由/能耗块 → 不在 OSC 场景表达范围（且 lark_parser 当前未支持，跳过并告警）

IP 安全：本导出器只消费 .tw 的"用户侧场景描述"（节点/边/车辆/规则），
不触及仿真引擎内核，可安全用于参赛材料展示。

用法：
  python tw_to_xosc.py input.tw [-o out.xosc] [--xodr out.xodr] [--no-xodr] [--validate]

依赖：lark（解析 .tw）、标准库 xml。
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime

# ---- 让脚本可独立运行（直接 python tw_to_xosc.py）----
# 必须以包形式导入 lark_parser（其内部用相对导入 from .ir / from .tokens）
_HERE = os.path.dirname(os.path.abspath(__file__))
_TIMEWAY = os.path.dirname(os.path.dirname(_HERE))  # .../TimeWay
if _TIMEWAY not in sys.path:
    sys.path.insert(0, _TIMEWAY)
from modules.dsl.lark_parser import parse_file_advanced, TwError  # noqa: E402
from modules.dsl.parser import parse_file as _parse_file_line  # noqa: E402

import xml.etree.ElementTree as ET  # noqa: E402

OSC_NS = "http://schemas.asam.net/OpenSCENARIO/1.2"
XR_NS = "http://schemas.asam.net/OpenDRIVE/1.7"
XODR_FILE = "{stem}.xodr"  # 占位，运行时替换

# 天气 → OSC Weather 参数（intensity 0~1，visualRange 米）
WEATHER_MAP = {
    "晴":    {"sun": 1.0, "rain": 0.0, "fog": 1000.0, "label": "clear"},
    "clear": {"sun": 1.0, "rain": 0.0, "fog": 1000.0, "label": "clear"},
    "多云":   {"sun": 0.5, "rain": 0.0, "fog": 800.0,  "label": "cloudy"},
    "小雨":   {"sun": 0.3, "rain": 0.3, "fog": 500.0,  "label": "light_rain"},
    "light_rain": {"sun": 0.3, "rain": 0.3, "fog": 500.0, "label": "light_rain"},
    "大雨":   {"sun": 0.1, "rain": 0.7, "fog": 300.0,  "label": "heavy_rain"},
    "heavy_rain": {"sun": 0.1, "rain": 0.7, "fog": 300.0, "label": "heavy_rain"},
    "暴雨":   {"sun": 0.0, "rain": 1.0, "fog": 150.0,  "label": "storm"},
    "storm":  {"sun": 0.0, "rain": 1.0, "fog": 150.0,  "label": "storm"},
    "雾":    {"sun": 0.4, "rain": 0.0, "fog": 80.0,   "label": "fog"},
    "团雾":   {"sun": 0.4, "rain": 0.0, "fog": 40.0,   "label": "fog_dense"},
    "结冰":   {"sun": 0.3, "rain": 0.0, "fog": 400.0,  "label": "ice", "friction": 0.3},
    "ice":    {"sun": 0.3, "rain": 0.0, "fog": 400.0,  "label": "ice", "friction": 0.3},
    "雪":    {"sun": 0.3, "rain": 0.0, "fog": 300.0,  "label": "snow", "friction": 0.4},
    "snow":   {"sun": 0.3, "rain": 0.0, "fog": 300.0,  "label": "snow", "friction": 0.4},
}

# 车型 → 尺寸（米），用于 BoundingBox 近似
VEH_DIMS = {
    "自卸卡车": (9.0, 2.6, 3.4), "电动自卸卡车": (9.0, 2.6, 3.4),
    "水泥罐车": (10.0, 2.5, 3.6), "通勤班车": (8.0, 2.5, 3.0),
    "轿车": (4.8, 1.9, 1.5), "卡车": (8.0, 2.5, 3.2),
}


def _q(tag: str) -> str:
    """OpenSCENARIO 带命名空间标签。"""
    return f"{{{OSC_NS}}}{tag}"


def _veh_dims(vtype: str):
    return VEH_DIMS.get(vtype, (5.0, 2.0, 1.8))


def _kmh_to_ms(kmh: float) -> float:
    return kmh * 1000.0 / 3600.0


def _node_by_id(ir, nid):
    for n in ir.data["map"]["nodes"]:
        if n.get("id") == nid:
            return n
    return None


def _edge_geom(ir):
    """返回每条边的几何：起点坐标、朝向(rad)、长度(米)。"""
    out = []
    for e in ir.data["map"]["edges"]:
        a = _node_by_id(ir, e.get("from"))
        b = _node_by_id(ir, e.get("to"))
        if not a or not b:
            continue
        dx = b["x"] - a["x"]
        dy = b["y"] - a["y"]
        length = math.hypot(dx, dy)
        hdg = math.atan2(dy, dx)
        out.append({"edge": e, "a": a, "b": b, "length": length, "hdg": hdg})
    return out


def _weather_params(wtype: str):
    return WEATHER_MAP.get(wtype, WEATHER_MAP["晴"])


# ============================ OpenSCENARIO 构建 ============================

def _build_entities(ir, geom):
    """展开车辆实例（每种车型按 count 展开为独立实体）+ 行人实体。"""
    entities = []  # {ref, kind:'vehicle'|'ped', type, vtype, start_edge, dims, phys}
    idx = 0
    # 选第一条边作为默认出生边
    spawn = geom[0] if geom else None
    for v in ir.data["vehicles"]:
        vtype = v.get("type", "车辆")
        cnt = min(int(v.get("count", 1)), 60)
        dims = _veh_dims(vtype)
        for k in range(cnt):
            idx += 1
            entities.append({
                "ref": f"E{idx}", "kind": "vehicle", "vtype": vtype,
                "dims": dims, "phys": v.get("physics", {}),
                "spawn": spawn, "sub": k,
            })
    # 行人：从 横穿/人员 规则提取
    for r in ir.data.get("rules", []):
        act = r.get("action", {})
        if act.get("type") in ("pedestrian",) or r.get("trigger", {}).get("type") == "crossing":
            loc = act.get("location") or r.get("trigger", {}).get("location", "")
            idx += 1
            entities.append({
                "ref": f"P{idx}", "kind": "ped", "vtype": f"行人_{loc or 'generic'}",
                "dims": (0.5, 0.5, 1.7), "phys": {},
                "spawn": spawn, "sub": 0, "loc": loc,
            })
    return entities


def _spawn_pose(ent):
    """计算实体出生位姿（世界坐标 x,y,h），沿出生边方向轻微错位。"""
    sp = ent.get("spawn")
    if not sp:
        return {"x": 0.0, "y": 0.0, "z": 0.0, "h": 0.0}
    a = sp["a"]
    hdg = sp["hdg"]
    # 垂直方向错位，避免重叠
    lateral = (ent["sub"] % 10) * 1.5 - 7.5
    off_fwd = (ent["sub"] // 10) * 5.0
    ca, sa = math.cos(hdg + math.pi / 2), math.sin(hdg + math.pi / 2)
    x = a["x"] + math.cos(hdg) * off_fwd + ca * lateral
    y = a["y"] + math.sin(hdg) * off_fwd + sa * lateral
    z = a.get("z", 0.0)
    return {"x": x, "y": y, "z": z, "h": hdg}


def _initial_speed(ent):
    sp = ent.get("spawn")
    if sp and sp["edge"].get("speed_limit") is not None:
        return _kmh_to_ms(float(sp["edge"]["speed_limit"])) * 0.8
    return 8.0  # 默认 8 m/s


def _make_vehicle_element(ent):
    name = ent["vtype"]
    L, W, H = ent["dims"]
    phys = ent["phys"]
    v = ET.Element(_q("Vehicle"), {"name": name})
    # BoundingBox
    bb = ET.SubElement(v, _q("BoundingBox"))
    _center = ET.SubElement(bb, _q("Center"))
    ET.SubElement(_center, _q("Dimensions"),
                  {"width": f"{W:.2f}", "length": f"{L:.2f}", "height": f"{H:.2f}"})
    ET.SubElement(_center, _q("Offset"),
                  {"x": f"{L/2:.2f}", "y": "0.00", "z": f"{H/2:.2f}"})
    # Performance
    perf = ET.SubElement(v, _q("Performance"),
                         {"maxSpeed": "55.55", "maxAcceleration": "8.0",
                          "maxDeceleration": "10.0"})
    # 若物理块有功率，用于近似 maxSpeed（功率→极速粗略换算）
    if phys.get("max_power_kw"):
        try:
            p = float(phys["max_power_kw"]) * 1000.0
            # v_top ≈ (2P/(rho*CdA))^(1/3) 简化
            cda = float(phys.get("cda", 4.5))
            vtop = (2 * p / (1.2 * cda)) ** (1 / 3)
            perf.set("maxSpeed", f"{min(vtop, 60.0):.2f}")
        except (TypeError, ValueError):
            pass
    # Axles
    ax = ET.SubElement(v, _q("Axles"))
    ET.SubElement(ax, _q("FrontAxle"),
                  {"maxSteering": "0.5", "wheelDiameter": "1.0",
                   "trackWidth": f"{W:.2f}", "positionX": f"{L*0.4:.2f}", "positionZ": "0.5"})
    ET.SubElement(ax, _q("RearAxle"),
                  {"maxSteering": "0.0", "wheelDiameter": "1.0",
                   "trackWidth": f"{W:.2f}", "positionX": f"{-L*0.4:.2f}", "positionZ": "0.5"})
    # Properties（保留原始物理参数，供下游工具读取）
    props = ET.SubElement(v, _q("Properties"))
    for k, val in phys.items():
        ET.SubElement(props, _q("Property"),
                      {"name": f"tw.{k}", "value": str(val)})
    ET.SubElement(props, _q("Property"),
                  {"name": "tw.source", "value": "TimeWay .tw DSL export"})
    return v


def _make_pedestrian_element(ent):
    p = ET.SubElement  # noqa
    ped = ET.Element(_q("Pedestrian"), {"name": ent["vtype"], "mass": "70.0"})
    bb = ET.SubElement(ped, _q("BoundingBox"))
    _c = ET.SubElement(bb, _q("Center"))
    ET.SubElement(_c, _q("Dimensions"), {"width": "0.50", "length": "0.50", "height": "1.70"})
    ET.SubElement(_c, _q("Offset"), {"x": "0.00", "y": "0.00", "z": "0.85"})
    ped.append(ET.Element(_q("PedestrianController")))
    props = ET.SubElement(ped, _q("Properties"))
    ET.SubElement(props, _q("Property"), {"name": "tw.source", "value": "TimeWay .tw DSL export"})
    return ped


def _teleport_action(x, y, z, h):
    pa = ET.Element(_q("PrivateAction"))
    ta = ET.SubElement(pa, _q("TeleportAction"))
    pos = ET.SubElement(ta, _q("Position"))
    ET.SubElement(pos, _q("WorldPosition"),
                  {"x": f"{x:.2f}", "y": f"{y:.2f}", "z": f"{z:.2f}", "h": f"{h:.4f}"})
    return pa


def _speed_action(target_ms, dyn_time=5.0):
    pa = ET.Element(_q("PrivateAction"))
    la = ET.SubElement(pa, _q("LongitudinalAction"))
    sa = ET.SubElement(la, _q("SpeedAction"))
    ET.SubElement(sa, _q("SpeedActionDynamics"),
                  {"dynamicsDimension": "time", "value": f"{dyn_time:.1f}",
                   "dynamicsShape": "linear"})
    st = ET.SubElement(sa, _q("SpeedTarget"))
    ET.SubElement(st, _q("AbsoluteTargetSpeed"), {"value": f"{target_ms:.2f}"})
    return pa


def _sim_time_condition(value, rule="greaterThan"):
    cond = ET.Element(_q("Condition"), {"name": f"t{value:.0f}", "delay": "0", "priority": "overwrite"})
    bv = ET.SubElement(cond, _q("ByValueCondition"))
    ET.SubElement(bv, _q("SimulationTimeCondition"),
                  {"value": f"{value:.1f}", "rule": rule})
    return cond


def _start_trigger(value, rule="greaterThan"):
    st = ET.Element(_q("StartTrigger"))
    cg = ET.SubElement(st, _q("ConditionGroup"))
    cg.append(_sim_time_condition(value, rule))
    return st


def _environment_action(wtype: str, friction=1.0):
    ga = ET.Element(_q("GlobalAction"))
    ea = ET.SubElement(ga, _q("EnvironmentAction"))
    env = ET.SubElement(ea, _q("Environment"))
    w = ET.SubElement(env, _q("Weather"), {"cloudState": "free"})
    wp = _weather_params(wtype)
    ET.SubElement(w, _q("Sun"), {"intensity": f"{wp['sun']:.2f}",
                                 "azimuth": "0.00", "elevation": "0.00"})
    ET.SubElement(w, _q("Fog"), {"visualRange": f"{wp['fog']:.1f}"})
    ET.SubElement(w, _q("Precipitation"),
                  {"precipitationType": "rain", "intensity": f"{wp['rain']:.2f}"})
    ET.SubElement(env, _q("RoadCondition"),
                  {"frictionScaleFactor": f"{wp.get('friction', friction):.2f}"})
    return ga


def parse_tw_degraded(path: str):
    """解析 .tw：优先进阶（Lark）语法；失败时降级到行式解析器。

    v3 的路由/能耗预留块、三坐标节点、路面属性字典等进阶语法 Lark 文法
    暂未覆盖——降级到行式解析器（容忍未知块）后仍可完成导出，
    覆盖信息记录在返回的 coverage["warnings"] 里。
    返回 (ir, coverage_warnings)。
    """
    try:
        return parse_file_advanced(path), []
    except TwError as e:
        warn = (f"进阶语法解析失败，已降级到行式解析器"
                f"（v3 预留块按需跳过）: {str(e).splitlines()[0]}")
        return _parse_file_line(path), [warn]


def export_scenario(path: str, out_dir: str | None = None):
    """API 友好入口：.tw 文件 → {name, xosc, xodr, coverage[, xosc_path, xodr_path]}。

    out_dir 为 None 时只在内存中生成文本不落盘；否则同时写出
    <stem>.xosc / <stem>.xodr 并返回路径（显式目录，避免污染场景库）。
    """
    ir, extra_warnings = parse_tw_degraded(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    xodr_name = f"{stem}.xodr"
    osc, xodr, cov = convert(ir, xodr_name)
    cov["warnings"] = extra_warnings + cov["warnings"]

    result = {
        "name": ir.name,
        "xosc": _serialize_osc(osc),
        "xodr": _serialize_xodr(xodr),
        "coverage": cov,
    }
    if out_dir is not None:
        os.makedirs(out_dir, exist_ok=True)
        xosc_path = os.path.join(out_dir, f"{stem}.xosc")
        xodr_path = os.path.join(out_dir, f"{stem}.xodr")
        with open(xosc_path, "w", encoding="utf-8") as f:
            f.write(result["xosc"])
        with open(xodr_path, "w", encoding="utf-8") as f:
            f.write(result["xodr"])
        result["xosc_path"] = xosc_path
        result["xodr_path"] = xodr_path
        # 同步落盘车辆/行人目录（CatalogFile 必须与主 .xosc 同级目录下）
        cats = cov.get("_catalogs") or {}
        if cats.get("vehicle") is not None:
            vdir = os.path.join(out_dir, "VehicleCatalogs")
            os.makedirs(vdir, exist_ok=True)
            vcat_path = os.path.join(vdir, f"{stem}_vehicles.xosc")
            with open(vcat_path, "w", encoding="utf-8") as f:
                f.write(_serialize_osc(cats["vehicle"]))
            result["vehicle_catalog_path"] = vcat_path
        if cats.get("pedestrian") is not None:
            pdir = os.path.join(out_dir, "PedestrianCatalogs")
            os.makedirs(pdir, exist_ok=True)
            pcat_path = os.path.join(pdir, f"{stem}_pedestrians.xosc")
            with open(pcat_path, "w", encoding="utf-8") as f:
                f.write(_serialize_osc(cats["pedestrian"]))
            result["pedestrian_catalog_path"] = pcat_path
    return result


def _serialize_osc(osc_root) -> str:
    import io
    _indent(osc_root)
    ET.register_namespace("", OSC_NS)
    buf = io.StringIO()
    ET.ElementTree(osc_root).write(buf, encoding="unicode",
                                    xml_declaration=True)
    return buf.getvalue() + "\n"


def _serialize_xodr(xodr_root) -> str:
    import io
    _indent(xodr_root)
    buf = io.StringIO()
    ET.ElementTree(xodr_root).write(buf, encoding="unicode",
                                    xml_declaration=True)
    return buf.getvalue() + "\n"


def _build_catalogs(entities):
    """构造 Vehicle/Pedestrian 目录元素（每种车型/行人类型仅一份定义）。

    供主 .xosc 的 <CatalogReference> 引用，避免 25 辆车生成 25 份
    完全相同的 <Vehicle> 定义（esmini/SUMO 均支持该 Catalog 机制）。
    返回值 (vehicle_catalog_el, pedestrian_catalog_el)，均为 ET 根元素。
    """
    veh_by_type, ped_by_type = {}, {}
    for e in entities:
        target = veh_by_type if e["kind"] == "vehicle" else ped_by_type
        target.setdefault(e["vtype"], e)
    today = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    vcat = ET.Element(_q("OpenSCENARIO"))
    ET.SubElement(vcat, _q("FileHeader"),
                  {"revMajor": "1", "revMinor": "0", "date": today,
                   "description": "TimeWay 车辆目录（车型定义唯一，供场景 CatalogReference 引用）",
                   "author": "时途 TimeWay DSL Exporter"})
    ve = ET.SubElement(vcat, _q("Catalog"), {"name": "VehicleCatalog"})
    for e in veh_by_type.values():
        ve.append(_make_vehicle_element(e))

    pcat = ET.Element(_q("OpenSCENARIO"))
    ET.SubElement(pcat, _q("FileHeader"),
                  {"revMajor": "1", "revMinor": "0", "date": today,
                   "description": "TimeWay 行人目录（行人类型定义唯一）",
                   "author": "时途 TimeWay DSL Exporter"})
    pe = ET.SubElement(pcat, _q("Catalog"), {"name": "PedestrianCatalog"})
    for e in ped_by_type.values():
        pe.append(_make_pedestrian_element(e))
    return vcat, pcat


def convert(ir, xodr_filename: str):
    """把 ScenarioIR 转成 (osc_root, xodr_root, coverage)。

    coverage 额外携带 "_catalogs": {"vehicle": root, "pedestrian": root}，
    供调用方写盘时一并输出（每车型/行人类型仅一份定义）。
    """
    coverage = {
        "supported": ["nodes", "edges(直线近似)", "vehicles+物理", "weather剧本",
                      "规则:时间触发封路/事故", "规则:人员横穿"],
        "approximated": ["随机触发→确定性时间", "坡度/弯道/路面/高程→路网简化"],
        "deferred": ["路由/能耗块", "信号/红绿灯", "弯道真实曲率几何", "OpenDRIVE路网互联"],
        "warnings": [],
    }
    geom = _edge_geom(ir)
    if not geom:
        coverage["warnings"].append("无有效道路几何（节点/边缺失），实体将生成在原点")
    entities = _build_entities(ir, geom)
    vcat_root, pcat_root = _build_catalogs(entities)
    coverage["_catalogs"] = {"vehicle": vcat_root, "pedestrian": pcat_root}
    veh_refs = [e["ref"] for e in entities if e["kind"] == "vehicle"]

    # ---- FileHeader ----
    # 命名空间由 _q() 标签自带，ET 序列化时自动生成 xmlns，勿手动再加（会重复属性）
    osc = ET.Element(_q("OpenSCENARIO"))
    today = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    scen_name = ir.name or "TimeWayScenario"
    ET.SubElement(osc, _q("FileHeader"),
                  {"revMajor": "1", "revMinor": "2", "date": today,
                   "description": f"Exported from TimeWay .tw DSL: {scen_name}",
                   "author": "时途 TimeWay DSL Exporter"})

    # ---- RoadNetwork ----
    rn = ET.SubElement(osc, _q("RoadNetwork"))
    lf = ET.SubElement(rn, _q("LogicFile"))
    lf.set("filepath", xodr_filename)

    # ---- CatalogLocations（车辆/行人目录：每车型定义仅一份，供所有实例引用）----
    # OSC 1.2 标准：实体定义经 Catalog 去重复用，避免 25 辆车生成 25 份
    # 完全相同的 <Vehicle> 定义（esmini/SUMO 均支持）。
    cl = ET.SubElement(osc, _q("CatalogLocations"))
    vcat = ET.SubElement(cl, _q("VehicleCatalog"))
    ET.SubElement(vcat, _q("Directory"), {"path": "VehicleCatalogs"})
    pcat = ET.SubElement(cl, _q("PedestrianCatalog"))
    ET.SubElement(pcat, _q("Directory"), {"path": "PedestrianCatalogs"})

    # ---- Entities ----
    # 每个实例一个 ScenarioObject（唯一 ref），车型定义通过 CatalogReference
    # 引用 VehicleCatalogs 中的唯一条目；Init 的 Private entityRef 一一对应。
    ents = ET.SubElement(osc, _q("Entities"))
    for e in entities:
        so = ET.SubElement(ents, _q("ScenarioObject"), {"name": e["ref"]})
        cat_ref = ET.SubElement(so, _q("CatalogReference"))
        if e["kind"] == "vehicle":
            cat_ref.set("catalogName", "VehicleCatalog")
            cat_ref.set("entryName", e["vtype"])
        else:
            cat_ref.set("catalogName", "PedestrianCatalog")
            cat_ref.set("entryName", e["vtype"])

    # ---- Storyboard ----
    sb = ET.SubElement(osc, _q("Storyboard"))
    init = ET.SubElement(sb, _q("Init"))
    init_actions = ET.SubElement(init, _q("Actions"))
    for e in entities:
        priv = ET.SubElement(init_actions, _q("Private"), {"entityRef": e["ref"]})
        pose = _spawn_pose(e)
        priv.append(_teleport_action(pose["x"], pose["y"], pose["z"], pose["h"]))
        priv.append(_speed_action(_initial_speed(e)))

    story = ET.SubElement(sb, _q("Story"), {"name": f"{scen_name}_story"})
    act = ET.SubElement(story, _q("Act"), {"name": "Act1"})

    # ---- Maneuver: 规则事件 ----
    man = ET.SubElement(act, _q("Maneuver"), {"name": "Rules"})
    for i, r in enumerate(ir.data.get("rules", []), 1):
        trig = r.get("trigger", {})
        actn = r.get("action", {})
        ttype = trig.get("type")
        atype = actn.get("type")
        ev = ET.SubElement(man, _q("Event"),
                           {"name": f"rule_{i}_{atype}", "priority": "parallel"})
        action = ET.SubElement(ev, _q("Action"))
        # 触发时间：时间触发用其秒；随机用 seed 采样；横穿默认 0
        if ttype == "time":
            tval = float(trig.get("at_sec", 0))
        elif ttype == "random":
            seed = (ir.data.get("meta", {}).get("seed") or 42)
            tval = (seed * (i + 1) % 1200) + 30.0  # 确定性近似
            coverage["warnings"].append(
                f"规则#{i} 随机触发近似为 t={tval:.0f}s（OSC 1.2 无原生概率触发）")
        elif ttype == "crossing":
            tval = 0.0
        else:
            tval = 0.0
        # 动作语义
        if atype in ("close_road", "accident"):
            # 路网事件 → 全部车辆减速至停止（封路/事故占用）
            for ref in veh_refs:
                priv = ET.SubElement(action, _q("Private"), {"entityRef": ref})
                priv.append(_speed_action(0.0, dyn_time=2.0))
        elif atype == "pedestrian" or ttype == "crossing":
            # 行人横穿：找到对应行人实体
            ped = next((e for e in entities if e["kind"] == "ped" and
                        e.get("loc") == actn.get("location", "")), None)
            if ped is None:
                ped = next((e for e in entities if e["kind"] == "ped"), None)
            if ped is not None:
                priv = ET.SubElement(action, _q("Private"), {"entityRef": ped["ref"]})
                priv.append(_speed_action(1.5, dyn_time=1.0))
        ev.append(_start_trigger(tval))

    # ---- Maneuver: 天气环境 ----
    wman = ET.SubElement(act, _q("Maneuver"), {"name": "Weather"})
    for i, seg in enumerate(ir.data.get("weather", {}).get("script", []), 1):
        wtype = seg.get("type", "晴")
        tstart = float(seg.get("start", 0))
        ev = ET.SubElement(wman, _q("Event"),
                           {"name": f"weather_{i}_{wtype}", "priority": "parallel"})
        action = ET.SubElement(ev, _q("Action"))
        gact = ET.SubElement(action, _q("GlobalAction"))
        gact.append(_environment_action(wtype))
        ev.append(_start_trigger(tstart))

    # 初始天气（t=0）若脚本首段非 0，补一段
    wscript = ir.data.get("weather", {}).get("script", [])
    if wscript and float(wscript[0].get("start", 0)) > 0:
        ev = ET.SubElement(wman, _q("Event"),
                           {"name": "weather_init", "priority": "parallel"})
        action = ET.SubElement(ev, _q("Action"))
        gact = ET.SubElement(action, _q("GlobalAction"))
        gact.append(_environment_action(wscript[0].get("type", "晴")))
        ev.append(_start_trigger(0.0))

    # ---- StopTrigger ----
    end_t = 3600.0
    if wscript:
        try:
            end_t = max(float(s.get("end", 0)) for s in wscript)
        except (TypeError, ValueError):
            pass
    stp = ET.SubElement(sb, _q("StopTrigger"))
    cg = ET.SubElement(stp, _q("ConditionGroup"))
    cg.append(_sim_time_condition(end_t, rule="greaterThan"))

    # ---- 生成 xodr ----
    xodr = _build_xodr(ir, geom)

    return osc, xodr, coverage


def _build_xodr(ir, geom):
    """最小可用 OpenDRIVE 1.7：每条边一条直线道路（不互联）。"""
    xr = ET.Element("OpenDRIVE", {"xmlns": XR_NS})
    hdr = ET.SubElement(xr, "header",
                        {"revMajor": "1", "revMinor": "7",
                         "name": ir.name or "TimeWay", "version": "1.0",
                         "date": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                         "north": "0.0", "south": "0.0",
                         "east": "0.0", "west": "0.0"})
    for i, g in enumerate(geom, 1):
        e = g["edge"]
        a, b = g["a"], g["b"]
        length = g["length"]
        hdg = g["hdg"]
        lanes = max(1, int(e.get("lanes", 1)))
        road = ET.SubElement(xr, "road",
                             {"id": str(i), "name": f"{e.get('from')}->{e.get('to')}",
                              "length": f"{length:.2f}", "junction": "-1"})
        link = ET.SubElement(road, "link")
        # 简化：不互联（WorldPosition 出生不需要路网链接）
        link.append(ET.Element("link")) if False else None
        pv = ET.SubElement(road, "planView")
        geo = ET.SubElement(pv, "geometry",
                            {"s": "0.00", "x": f"{a['x']:.2f}",
                             "y": f"{a['y']:.2f}", "hdg": f"{hdg:.4f}",
                             "length": f"{length:.2f}"})
        geo.append(ET.Element("line"))
        lanes_el = ET.SubElement(road, "lanes")
        sec = ET.SubElement(lanes_el, "laneSection", {"s": "0.00"})
        center = ET.SubElement(sec, "center")
        cl = ET.SubElement(center, "lane", {"id": "0", "type": "driving", "level": "false"})
        cl.append(ET.Element("link"))
        ET.SubElement(cl, "width", {"sOffset": "0.00", "a": "0.15", "b": "0.0", "c": "0.0", "d": "0.0"})
        right = ET.SubElement(sec, "right")
        spd = e.get("speed_limit")
        for li in range(1, lanes + 1):
            lane = ET.SubElement(right, "lane",
                                 {"id": str(-li), "type": "driving", "level": "false"})
            lane.append(ET.Element("link"))
            ET.SubElement(lane, "width",
                          {"sOffset": "0.00", "a": "3.50", "b": "0.0", "c": "0.0", "d": "0.0"})
            if spd is not None:
                ET.SubElement(lane, "speed", {"sOffset": "0.00", "max": f"{float(spd):.1f}"})
    return xr


def _indent(elem, level=0):
    """就地美化缩进。"""
    pad = "  "
    children = list(elem)
    if children:
        elem.text = "\n" + pad * (level + 1)
        for child in children:
            _indent(child, level + 1)
        child = children[-1]
        child.tail = "\n" + pad * level
    else:
        elem.tail = "\n" + pad * level


def write_osc(osc_root, path):
    _indent(osc_root)
    tree = ET.ElementTree(osc_root)
    # 注册命名空间前缀
    ET.register_namespace("", OSC_NS)
    tree.write(path, encoding="utf-8", xml_declaration=True)


def write_xodr(xodr_root, path):
    _indent(xodr_root)
    tree = ET.ElementTree(xodr_root)
    tree.write(path, encoding="utf-8", xml_declaration=True)


def validate(path):
    """良构性校验（标准库 xml 解析）。"""
    try:
        ET.parse(path)
        return True, "XML 良构 OK"
    except ET.ParseError as e:
        return False, f"XML 解析失败: {e}"


def main(argv=None):
    ap = argparse.ArgumentParser(description="时途 .tw → OpenSCENARIO 1.2 导出器")
    ap.add_argument("input", help="输入 .tw 场景文件")
    ap.add_argument("-o", "--output", help="输出 .xosc 路径（默认同目录同名）")
    ap.add_argument("--xodr", help="输出 .xodr 路径")
    ap.add_argument("--no-xodr", action="store_true", help="不生成 xodr")
    ap.add_argument("--validate", action="store_true", help="生成后做 XML 良构校验")
    ap.add_argument("--report", action="store_true",
                    help="导出后自动跑一次仿真并生成 HTML 安全指标报告（一条龙）")
    ap.add_argument("--report-duration", type=float, default=600.0,
                    help="--report 时仿真时长（秒），默认 600")
    args = ap.parse_args(argv)

    try:
        ir, degrade_warnings = parse_tw_degraded(args.input)
    except Exception as e:
        print(f"[错误] .tw 解析失败: {e}", file=sys.stderr)
        return 2

    stem = os.path.splitext(os.path.basename(args.input))[0]
    out_dir = os.path.dirname(os.path.abspath(args.input))
    xosc_path = args.output or os.path.join(out_dir, f"{stem}.xosc")
    xodr_path = args.xodr or os.path.join(out_dir, f"{stem}.xodr")

    osc, xodr, cov = convert(ir, os.path.basename(xodr_path))
    cov["warnings"] = degrade_warnings + cov["warnings"]
    write_osc(osc, xosc_path)
    # 同步落盘目录文件（VehicleCatalogs / PedestrianCatalogs），
    # 主 .xosc 的 CatalogReference 才能被 esmini/SUMO 解析到车型/行人定义。
    for cat_key, sub_dir in (("vehicle", "VehicleCatalogs"),
                             ("pedestrian", "PedestrianCatalogs")):
        root = (cov.get("_catalogs") or {}).get(cat_key)
        if root is None:
            continue
        cat_dir = os.path.join(os.path.dirname(xosc_path), sub_dir)
        os.makedirs(cat_dir, exist_ok=True)
        cat_path = os.path.join(cat_dir, f"{stem}_{cat_key}.xosc")
        write_osc(root, cat_path)
    if not args.no_xodr:
        write_xodr(xodr, xodr_path)

    print(f"[OK] 场景: {ir.name}")
    print(f"  实体数: {len(ir.data['vehicles'])} 车型 / "
          f"{len([e for e in ir.data.get('rules',[]) if e.get('action',{}).get('type')=='pedestrian'])} 行人规则")
    print(f"  导出: {xosc_path}")
    if not args.no_xodr:
        print(f"  路网: {xodr_path}")
    print("[覆盖] 支持:", ", ".join(cov["supported"]))
    print("[覆盖] 近似:", ", ".join(cov["approximated"]))
    print("[覆盖] 暂缓:", ", ".join(cov["deferred"]))
    for w in cov["warnings"]:
        print("  [!]", w)

    if args.validate:
        ok, msg = validate(xosc_path)
        print(f"[校验] {xosc_path}: {msg}")
        if not args.no_xodr:
            ok2, msg2 = validate(xodr_path)
            print(f"[校验] {xodr_path}: {msg2}")

    if args.report:
        # 一条龙：.tw → 仿真 → HTML 安全报告（复用 safety_report 模块）
        print("\n[报告] 运行仿真并生成安全指标 HTML 报告...")
        try:
            from modules.safety_report import make_report
            from main import run_scenario
            meta = ir.data.get("meta", {})
            veh_n = sum(int(v.get("count", 1)) for v in ir.data.get("vehicles", []))
            wscript = ir.data.get("weather", {}).get("script", [])
            weather = wscript[0].get("type", "晴") if wscript else "晴"
            report = run_scenario(
                scenario_name=ir.name or "safe_report_run",
                seed=meta.get("seed") or 42,
                duration=args.report_duration,
                time_slot="平峰",
                weather=weather,
                n_vehicles=veh_n or 27,
                output_dir=out_dir,
                verbose=False,
                warmup_duration=30.0,
                min_spacing=40.0,
            )
            html_path = make_report(out_dir)
            print(f"[报告] 已生成: {html_path}")
        except Exception as e:
            print(f"[报告] 生成失败: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

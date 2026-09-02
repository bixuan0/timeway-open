"""
tokens.py — 中英 1:1 关键字映射 + 全角符号归一化
==================================================
中文支持 = 一张映射表：parser 先把中文 token 归一化成英文内部 token，
之后逻辑与英文版完全共用。新增"换皮语言"只需扩一张表。

对齐 v2 规范（docs/scenario_dsl_architecture_20260821.md §2.1）。
"""
from __future__ import annotations

import re

# ---- 中英 1:1 映射（中文 -> 英文内部 token）----
TOKEN_MAP: dict[str, str] = {
    # 场景结构
    "场景": "scenario", "种子": "seed",
    "运行": "runs", "次数": "runs", "次": "times",
    "车辆总数": "vehicle_total", "车流": "vehicle_total",
    "地图": "map", "节点": "node", "朝向": "heading",
    # 道路
    "道路": "road", "车道": "lanes", "限速": "speed", "弯道": "curve",
    "坡度": "slope", "路面": "surface",
    "交叉口": "intersection", "匝道": "ramp", "高架": "elevated",
    "隧道": "tunnel", "桥梁": "bridge", "景观": "landscape",
    # 区域
    "区域": "zone", "限速区": "speed_limit", "装卸区": "work_zone",
    "位于": "at", "公里": "km", "降到": "to", "容量": "capacity",
    # 天气（P0-1 修复：天气名不翻英文——config.WEATHER_PARAMS/WeatherSystem/
    # 路由消费的都是 config.WEATHER_TYPES 中文键（晴/多云/小雨/大雨/雷雨/雾/
    # 夜间/雪/冰雹），翻成英文会 KEY_ERR 或校验失败；中文原文透传到 parser，
    # parser 再做「英文天气 -> 中文键」归一，两条路都收敛到同一词表）。
    "天气": "weather", "剧本": "script", "转移": "transition",
    "转向": "to", "转为": "to",
    # v6 D1：区域天气块（区域天气 { 区域 名 { 多边形 [...] 天气 X 强度 N } }）
    "区域天气": "weather_regions", "多边形": "polygon", "强度": "intensity",
    # 车辆
    "车辆": "vehicles", "辆": "x",
    # 规则
    "规则": "rules", "当": "when",
    "时间": "time", "随机": "random",
    "封路": "close_road", "事故": "accident", "横穿": "crossing",
    "人员": "pedestrian", "调度": "schedule", "天气联动": "weather_link",
    # 进阶语法（复用 / 变量）
    "引用": "include", "继承": "extends", "设": "let", "变量": "let",
    # ---- 物理属性（v4 子项目1）：车辆物理键 + 路面附着 ----
    "质量": "mass", "最大总质量": "max_gross_mass",
    "风阻CdA": "cda", "滚动阻力": "rolling_resistance",
    "制动效率": "brake_efficiency", "载重": "load_factor",
    # v5 P0：质心高度（侧翻阈值）与发动机功率（上坡力平衡）
    "质心高度": "cg_height_m", "发动机功率": "max_power_kw", "最大功率": "max_power_kw",
    "附着系数": "mu_override",
    "吨": "t", "千克": "kg",
    # ---- 信号灯（S2 修复）：配时/相位/行人过街 ----
    "信号灯": "traffic_light", "红绿灯": "traffic_light",
    "红灯": "red_duration", "绿灯": "green_duration", "黄灯": "yellow_duration",
    "初始状态": "initial_state", "行人过街": "is_pedestrian",
    # ---- v3 预留（P0：schema 留口子，能耗/路由待 P2/P3 实现）----
    "高程": "elevation", "高程剖面": "elevation_profile", "剖面": "profile",
    "破损": "damage", "平整度": "iri", "路宽": "width", "承载": "load_class", "摩擦系数": "mu",
    "路由": "routing", "能耗": "energy", "权重": "weights",  # v6 路由块权重字面量
    "模式": "profile", "最省": "economy", "最快": "fastest", "最舒适": "comfortable", "最安全": "safest",
    "报告": "report", "每段": "per_segment",
    "开启": "on", "启用": "on", "打开": "on", "关闭": "off", "禁用": "off",
    # ---- v6 P2：引擎参数 meta 语法（B 组；写 .tw 即配引擎）----
    "AV渗透率": "av_ratio", "AV渗透": "av_ratio", "渗透率": "av_ratio",
    "时段": "time_slot", "时间段": "time_slot",
    "温度": "temperature", "摄氏度": "celsius", "度": "celsius",
    "风速": "wind_speed", "米每秒": "mps",
    "风场": "wind_mode", "风场模式": "wind_mode",  # v6 P2 修复：整体词先于单字，避免「模式」边界挡掉
    "进阶": "advanced", "基础": "basic",
    "输出类型": "output_types", "输出": "output_types",
    "品牌": "brand", "型号": "model_id", "车型型号": "model_id",
    "摘要": "summary", "验证指标": "validation_metrics", "天气系统": "weather_system",
    "心智状态": "mental_state", "事件日志": "event_logs", "车辆日志": "vehicle_logs",
    "违章类型": "violation_types", "职业统计": "job_stats", "能耗报告": "power_check",  # v6 P2：无字面量前缀——earley 会把 energy_* 拆成 energy(字面量)+NAME，
    # 而 power_check 无 power 字面量，整体匹配 OUTPUT_KIND/NAME，不可拆
    # 单位/杂项
    "秒": "sec", "分钟": "min", "公里": "km", "公里每小时": "kmh", "米": "m",
    "类型": "type", "数量": "count", "开始": "start", "结束": "end",
    # ---- v6 P2：D2 行人块 / E 事件块（补齐"只差语法"的死语法缺口）----
    "行人": "pedestrians",                    # 行人 { 数量 30 密度 0.05 }
    "密度": "density",
    "事件": "events",                          # 事件 { 爆破 { 位置 A 时刻 600 车道 2 } }
    "施工": "construction", "爆破": "blasting", "落石": "rockfall",
    "位置": "location", "时刻": "time_sec", "时长": "duration", "影响车道": "lanes",
    "长度": "length", "高度": "height", "半径": "radius",
}

# 反向：英文 -> 中文（用于报错提示 / decompile 演示）
REVERSE_MAP: dict[str, str] = {v: k for k, v in TOKEN_MAP.items()}

# 结构关键字（不参与数值/标识符解析，仅作语句起始判定）
SCENARIO_KEYWORDS = {"scenario", "map", "weather", "vehicles", "rules"}
NODE_KEYS = {"node", "heading"}
ROAD_KEYS = {"road", "lanes", "speed", "curve", "slope", "surface"}
ZONE_KEYS = {"zone", "speed_limit", "work_zone", "at", "km", "to", "capacity"}
WEATHER_KEYS = {"weather", "script", "transition"}
VEHICLE_KEYS = {"vehicles"}
RULE_KEYS = {"rules", "when", "time", "random", "close_road", "accident",
             "crossing", "pedestrian", "schedule", "weather_link"}

# 全角 -> 半角（中文输入法自动全角，必须归一化）
_FULLWIDTH = {ord(c): ord(c) - 0xFEE0 for c in "０１２３４５６７８９（）－，％．"}

# 关键中文标点 -> 英文标点
_PUNCT = {"（": "(", "）": ")", "，": ",", "、": ",", "；": ";",
          "：": ":", "％": "%", "．": ".", "。": "."}


def normalize(text: str) -> str:
    """全角 -> 半角 + 中文标点 -> 英文标点（保留中文汉字/英文/数字）"""
    out = []
    for ch in text:
        if ch in _PUNCT:
            out.append(_PUNCT[ch])
        else:
            out.append(ch.translate(_FULLWIDTH))
    return "".join(out)


# 关键字两侧不得是汉字：防止把标识符里嵌的子串误当关键字
# （例：场景名「水电站能耗路由」中的「能耗/路由」不得替换）。
_CJK = r'[\u4e00-\u9fff]'

# 数字后缀单位（由各解析器的「数字+单位」规则处理，不参与关键字替换）
_UNIT_NAMES = {"米", "秒", "辆"}


def translate_keywords(line: str) -> str:
    """中文关键字 -> 英文内部 token（带汉字边界检查，两侧加空格）。

    只替换两侧都不是汉字的关键字出现；嵌在中文标识符内部的子串跳过。
    单位字（米/秒/辆等）不在此处理，由各解析器的数字后缀规则负责。
    """
    for zh, en in sorted(TOKEN_MAP.items(), key=lambda kv: -len(kv[0])):
        if zh in _UNIT_NAMES:
            continue
        pat = re.compile(rf"(?<!{_CJK}){re.escape(zh)}(?!{_CJK})")
        line = pat.sub(f" {en} ", line)
    return line


def zh_to_en(token: str) -> str | None:
    """中文关键字 -> 英文内部 token；非关键字返回 None"""
    return TOKEN_MAP.get(token.strip())


def en_to_zh(token: str) -> str:
    """英文内部 token -> 中文（用于报错提示友好化）"""
    return REVERSE_MAP.get(token, token)

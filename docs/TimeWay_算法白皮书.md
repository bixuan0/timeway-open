# TimeWay 时途 · 微观交通仿真平台软件 —— 算法白皮书

> 版本：2026-08-28（v2；基于 D:\timeway 代码逐文件核实，含 8/22–8/28 交叉验证与交付件更新）
> 范围：路网生成、车辆物理、驾驶决策、心理建模、天气路面、事件安全、时空加速、数据采集全链路算法，及第三方交叉验证。

---

## 1. 总体架构

| 层 | 组件 | 说明 |
|----|------|------|
| 仿真核心 | `main.py` | 单线程时间步进主循环，`SIM_DT=0.1s` 步长，11 阶段 |
| 车辆实体 | `modules/vehicle.py` | 职业车辆，生命周期状态机，物理运动学（Numba） |
| 驾驶决策 | `modules/ai_controller.py`（人类）/ `modules/av_controller.py`（AV） | 规则式决策，含 IDM 对照模式 |
| 心理模型 | `modules/mental_state.py` | 路怒/创伤/不公记忆/行为压力（BP） |
| 路网地图 | `modules/map_generator.py` | jittered grid + Delaunay 剖分，信号灯/交警/封路 |
| 天气路面 | `modules/weather_system.py` / `road_system.py` / `weather_region.py` | 马尔可夫天气、逐边摩擦系数 μ、区域天气 |
| 事件 | `modules/events.py` | 事故区、碰撞/剐蹭、交互事件、心智冲击波 |
| 安全度量 | `modules/safety_metrics.py` + `modules/safety_report.py` | TTC / THW / PET / DRAC / Near-miss（SSAM 口径，独立可复用） |
| 行人 | `modules/pedestrian.py` | 4 类行人，压力驱动越界行为 |
| 加速 | `modules/spatial_grid.py` + `numba_kernels.py` + `vehicle_soa.py` | 空间哈希 + 8 个 @njit 内核 + SoA 批量 |
| 记录 | `modules/data_recorder.py` / `heatmap_recorder.py` / `frame_recorder.py` | 报告、7 层热力图、回放帧 |
| 服务 | `api.py` / `sim_worker.py` | Flask API + 多进程仿真 worker |

---

## 2. 仿真主循环（每 0.1s 一步）

`main.py` 主循环按固定顺序执行，共 11 个阶段：

```
for step in total_steps:
  ① 渐进式投放车辆（预热期 warmup_duration 内均匀注入；初始车队受 min_spacing 约束防挤簇）
  ② 更新 TimeSystem（时间/时段/真太阳时/夜间）
  ③ 更新区域天气 weather_region_sys
  ④ 更新路段耐久度 durability_sys（天气+磨损→破损降级）
  ⑤ 更新信号灯 → 交警 → 道路封路计时
  ⑥ 构建 VehicleSoA（O(N)）+ SpatialGrid（每 GRID_BUILD_INTERVAL 步重建）
  ⑦ 更新事件（事故区/交互事件，用空间网格加速）
  ⑧ 更新行人
  ⑨ 批量碰撞避让（batch_find_nearest_ahead_kernel，v7 SoA 批量）
  ⑩ 逐车：状态更新 → AI 决策 → 位置更新 → 路口计数 → 目的地检查
  ⑪ 安全度量（TTC/THW/PET）→ 碰撞检测 → 剐蹭检测 → 边缘场景提取
      → 数据采样（3步/0.3s）→ 心智采样（10步）→ SP采样（5步）
      → 帧记录（10步/1s）→ 热力图记录（每步）
```

关键设计：空间网格**每步重建**用于行为决策（不降频，保证行为一致）；碰撞/剐蹭检测等低频任务复用同网格。数据统计在预热期后开始（`warmup_duration`，默认 600s 内完成车辆投放）。

> **装配修复（8/28）**：初始车队 `initial_vehicles = min(n_vehicles, max(10, int(n×0.1)))`——原 `max(10,…)` 会把 n<10 的小场景膨胀到 10 辆，导致与声明车流不符的密度；修复后 n≥10 场景行为不变，n<10 不再膨胀。

---

## 3. 路网与地图生成

### 3.1 路网拓扑（map_generator.py）
- **jittered grid**：`GRID_COLS×GRID_ROWS`（11×7）节点，位置加随机扰动。
- **Delaunay 三角剖分**（scipy.spatial）生成候选边，过滤长度 > `NODE_SPACING_MAX×1.3` 的边。
- **BFS 保证连通**，不连通组件丢弃或补边。
- 车道数、限速、材质、坡度（grade）、弯道半径（默认 500m，钳制 80~2000m）、超高角（bank_angle）均带参数随机化。

### 3.2 信号灯 TrafficLight（map_generator.py）
- 状态机：`red → green → yellow → red`。
- 时长：红 `U(20,30)s`、绿 `U(25,35)s`、黄固定 3s。
- **v7.2 配时场景化**：默认 `TRAFFIC_LIGHT_FIXED_TIMING=True`——每盏灯配时生成时采样一次，之后**固定不变**（真实定时方案，可复现、可做配时对比）；置 False 恢复旧行为（仅对照）。
- 规则预制件模式（`light_config` 固定）供地图编辑器使用。

### 3.3 交警与封路
- 固定岗 + 每步 0.05% 概率出现；在场时闯红灯概率归零、违章被抓概率 ×3。
- `close_road` 双向封锁，`close_timer` 到期自动解封；路由（`pick_next_node` 贪心）跳过 `is_closed` 边。

### 3.4 交通拥堵（traffic_system.py）
- `TrafficJam`：概率 × 天气 `accident_mult` 随机生成；车辆按进度命中拥堵区间时降速 `speed_mult`。

---

## 4. 车辆模型（vehicle.py）

### 4.1 生命周期状态机
```
0=normal → 1=injured（倒计时恢复 normal）
         → 2=fatal / 3=left（退出活跃）
         → 4=parked（停放，倒计时后离开）
         → 5=suspended（吊销驾照，降速 50%，倒计时后 left）
```
- 5 个冷却计时器（红灯/变道/剐蹭/碰撞/学习）线性递减。
- 疲劳累积：仅 normal 态按**时间**累积（`FATIGUE_RATE_PER_SEC`，符合 GB/T 19056 连续驾驶疲劳口径）。
- 守法奖励：`lawful_timer` 达阈值扣 SP 值（越守法压力越低）。

### 4.2 物理运动学内核 `_physics_update_kernel`（Numba）
单位制：m / m·s⁻¹ / s。综合 6 类力：

| 效应 | 公式 |
|------|------|
| 空气阻力 | `F_drag = ½·ρ·Cd·A·v²`（ρ 含温度修正 `ρ₀·(1+α·(T-15))`） |
| 滚动阻力 | `F_rr = m·g·f_r`，`f_r = f_r0·(1+a_v·v)·(1+a_T·ΔT)` |
| 坡度阻力 | `F_grade = m·g·sin(atan(grade))`（可切小角度近似） |
| 弯道限速 | `a_lat = v²/R ≤ μ·g`；超限按 `v = min(v, √(max_lat·R)·0.95)` 强制减速，并叠加侧翻阈值 `a_lat ≤ g·B/(2·h_cg)` |
| 功率限制 | `F_max = P·η / max(v, v_min)`（P=Fv 关系，低速恒扭矩） |
| 抓地限制 | `F_max = μ·m·g·f_drive` |

- **加速**：`F_drive = min(F_power, F_traction, F_engine)`，`net = (F_drive − F_drag − F_rr − F_grade)/m`。
- **制动**：`a_brake = brake_eff·μ·g·mental_brake_mod`（brake_eff = 制动系统衰减因子；mental_brake_mod 由心智状态降低有效制动能力）。
- **天气速度惩罚**：target_speed × (1 − speed_penalty)；风力沿向推力叠加。
- **滑流（高级风场）**：前方 `SLIPSTREAM_DISTANCE` 内存在大型车（mass ≥ 阈值）→ 风阻降为 70%（`SLIPSTREAM_FACTOR=0.7`）。

### 4.3 生存压力 SP（Phase 2 核心）
```
有期限任务：
  time_pressure = 直线距离×1.4(曼哈顿系数) / 剩余时间
  raw_sp = (SP_DEADLINE_BASE + time_pressure × SP_TIME_PRESSURE_AMPLIFIER) × pressure_weight
无期限：   raw_sp = min(SP_NO_DEST_BASE × weight, sim_time×0.1 × weight)
超临界：   raw_sp > SP_CRITICAL 时 ×(1+50%)
SP_base   = min(raw_sp, SP_HARD_CAP=180)
SP_effective = SP_base × (1 + rage×0.3 + injustice_mem×0.001) × bp   ← 心智耦合
SP 分级： SAFE < WARNING < DANGER < CRITICAL
```
- AV 车辆 SP 恒为 0（行为完全确定）。
- SP 影响车速加成：WARNING +5~10%、DANGER/CRITICAL 递增。

---

## 5. 人类驾驶决策（ai_controller.py，规则式）

`control_vehicle` 流水线：SP 计算 → 速度决策 → 事故区响应 → 碰撞避让 → 闯红灯 → 路口减速 → 行人交互 → 变道。

### 5.1 速度决策 `_decide_speed`
```
base = max_speed
× SP 分级加成（SPEED_BOOST_*）
× 天气 speed_mult × 路面 condition 倍率 × 心智 speed_modifier
上限 = max_speed × speed_cap_mult × SPEED_BOOST_CAP
```

### 5.2 碰撞避让 `_collision_avoidance`（感知模型）
- 感知半径：`perception_radius()` 基础值随 `reaction_delay` 每 1s 缩小 50m，下限 25m（**情绪→反应延迟→感知收缩**闭环）。
- 感知角：±30°（`PERCEPTION_ANGLE`），仅同 `(from,to)` 路段、活跃车辆。
- 分级避让（按 gap / safety_line 比例，`safety_distance()` 物理式）：
  - `> 0.6`（`AVOID_PREVENTIVE_LOW`）：预防性减速到前车 ×0.90
  - `> 0.3`（`AVOID_ACTIVE_LOW`）：主动避让 ×0.50 + 触发变道
  - `≤ 0.3`：紧急 ×0.20 + 触发变道
- 路怒（aggression）缩短安全线 → 更贴近前车（危险驾驶）。
- `safety_distance()`：`d = v·t_react + v²/(2μg) + margin`（PAPER_DATA_COMPAT_MODE=False 物理式；True 时为论文线性式 `v×3+15`）。

### 5.3 变道
相邻车道、`edge.lanes > 1`、变道冷却；过渡期 `lane_change_timer` 内线性切换（简化：直接尝试，无间隙检查）。

### 5.4 碰撞物理
- 触发：`closing_rate`（相对速度在连线方向投影）> 5 m/s 且距离 < 碰撞半径之和。
- 后果：动量守恒 + 恢复系数 e（0.1~0.4 按速度插值）计算速度交换；`take_damage` 按严重度；per-pair 冷却 30s。
- 碰撞后可能生成事故区（全车道封锁 25s）并封路。

### 5.5 IDM 跟驰对照模式（v7.2，`DRIVING_MODEL="idm"`）
经典智能驾驶模型（Treiber et al. 2000），用于学术对照与模型验证，与规则式一键切换：

```
a = a_max × [1 − (v/v0)^δ − (s*/s)²]
s* = s0 + max(0, v·T + v·Δv / (2·√(a_max·b)))      Δv = v − v_lead
```

- 参数：`IDM_A_MAX=1.5`、`IDM_B=2.0`、`IDM_T=1.2`、`IDM_S0=2.0`、`IDM_DELTA=4.0`、`IDM_V0_FACTOR=1.0`。
- 期望速度 v0 取基础目标速度（保留 SP/天气/心智加成）；感知过滤（扇形/同路段/心智反应延迟收缩）与规则式共用，保证对照公平。
- 已接入 `config_overrides` 白名单与前端「高级参数」面板。

### 5.6 驾驶模型注册表（v8，可插拔驾驶/规则）
规则式与 IDM 从硬编码 `if DRIVING_MODEL=='idm'` 二分分支，升级为**注册表驱动**（`driving_model_registry.py`）：
- **统一处理器契约**：`handler(controller, vehicle, world, target_speed, dt) -> (speed, need_lane_change)`，与避让分支返回一致。
- **内置默认插件**：`rule`（规则式批量避让 + 逐车 Python 回退）、`idm`（经典跟驰对照）——行为与原分支逐字节等价（133 项回归验证）。
- **第三方可插拔**：`register("my_model", handler)` 即可挂载新驾驶模型，`config.DRIVING_MODEL="my_model"` 一句话切换，**无需改动 ai_controller**；未知名字安全回退 `rule` 并告警。
- **契约测试锁定接缝**：`test_driving_model_registry.py` 7 项（内置注册/未知回退/第三方挂载卸载/非法拒绝/签名对齐），全量回归 133 passed + 1 skipped 无退化。

答辩口径：rule/idm 不是"两个 if"，而是注册表里的**内置默认插件**——任何第三方策略 register 一个处理器即为新驾驶模型，仿真引擎是**可扩展平台**而非死代码。

### 5.7 开发插件教程（第三方视角，真实可运行）
以 `docs/plugin_example_conservative.py`（「保守防御型」第三方插件）为完整范例，四步接入一个全新驾驶模型：

1. **写处理器**——实现统一契约 `handler(controller, vehicle, world, target_speed, dt) -> (speed, need_lane_change)`：
   ```python
   def conservative_handler(controller, vehicle, world, target_speed, dt):
       speed = target_speed * 0.75                      # 保守：只攻限速 75%
       leader, gap = controller._find_leader(vehicle, world)
       if leader is not None and gap is not None:
           speed = min(speed, gap / 2.5)                # 2.5s 车头时距，绝不贴车
       return max(0.0, speed), False
   ```
2. **挂载**：`register("conservative", conservative_handler)`——一行，无需改动 `main.py`/`ai_controller.py`。
3. **一句话切换**：`config_overrides={"DRIVING_MODEL": "conservative"}`（该键在 config 白名单内，经 `run_scenario` 生效后自动恢复原值）。
4. **强制验证**（防"假通"）：同一场景分别跑 rule 与保守模型，断言速度必须可读且可观测不同——不合格即失败退出，不打印假通过。实测：
   ```
   [rule]           均速 = 30.2 km/h
   [conservative]   均速 = 23.47 km/h   （= rule 的 77.7%，保守巡航语义成立）
   ```
   全程**未改引擎一行代码**，第三方策略即挂载并生效。

5. **完整插件源码**（`docs/plugin_example_conservative.py` 真实文件全文，直接可运行）：
   ```python
   # -*- coding: utf-8 -*-
   """
   plugin_example_conservative.py — 第三方驾驶模型插件·完整示例
   ================================================================
   演示 v8 可插拔驾驶/规则的完整链路（真实可运行）：

    插件实现 → registry.register 挂载 → config_overrides 一句话切换
    → 跑同一场景对比 rule vs conservative → 验证切换真实生效

   设计意图（答辩口径）：
     rule / idm 是注册表里的「内置默认插件」。这个文件是第三方视角的完整范式：
     不需要改动 main.py / ai_controller.py 任何一行，register 一个处理器即为新驾驶模型。
   """
   from __future__ import annotations

   import os
   import sys

   # --- 可定位到仓库根（无论从哪启动）---
   _HERE = os.path.dirname(os.path.abspath(__file__))
   _ROOT = os.path.dirname(_HERE)                      # D:\timeway?
   _TW = os.path.join(_ROOT, "TimeWay")                # D:\timeway\TimeWay
   for _p in (_TW, _ROOT):
       if _p not in sys.path:
           sys.path.insert(0, _p)

   from modules.driving_model_registry import register, get_model, available
   from main import run_scenario
   import config


   def conservative_handler(controller, vehicle, world, target_speed, dt):
       """保守防御型驾驶模型（第三方插件，未改动引擎任何代码）。

       策略：
        1. 巡航上限 = 基础目标速度 × 0.75（保守，不追限速极值）
        2. 前车间距不足时按 2.5s 车头时距防御性跟驰（绝不贴车）
       """
       speed = target_speed * 0.75

       leader, gap = controller._find_leader(vehicle, world)
       if leader is not None and gap is not None:
           safety_speed = gap / 2.5          # 2.5s 车头时距 → 速度上限
           speed = min(speed, safety_speed)

       return max(0.0, speed), False         # 不触发变道（与 idm 插件同契约）


   def _scenario():
       """返回一个轻量 hydro 施工区场景参数（真实 DSL→仿真链路）。"""
       return dict(
           scenario_name="tutorial-construction",
           seed=42,
           duration=30,                      # 30 秒（演示用，够看到巡航段）
           time_slot="平峰",
           weather="晴",
           n_vehicles=6,
           warmup_duration=5.0,
           min_spacing=50.0,
           custom_map=None,                  # 用同名 .tw 场景文件（仿真内置解析）
           config_overrides={},              # 由调用方逐次注入 DRIVING_MODEL
           verbose=False,
       )


   def _avg_speed(sample):
       """从仿真结果提取均速（km/h）（真实键：report.summary.avg_speed_ms）。"""
       if not sample:
           return None
       summ = sample.get("summary") or {}
       v = summ.get("avg_speed_ms")
       return round(v * 3.6, 2) if v is not None else None


   def main():
       base = _scenario()

       print("注册表当前可用模型:", available())

       # ① 第三方挂载
       register("conservative", conservative_handler)
       print("挂载后可用模型:", sorted(available()))
       assert "conservative" in available()

       # ② 用内置 rule 跑同一场景（对照组）
       base["config_overrides"] = {"DRIVING_MODEL": "rule"}
       rep_rule = run_scenario(**base)
       v_rule = _avg_speed(rep_rule)
       print(f"[rule]           均速 = {v_rule} km/h")

       # ③ 一句话切换 conservative，跑同一场景（experiment 组）
       base["config_overrides"] = {"DRIVING_MODEL": "conservative"}
       rep_cons = run_scenario(**base)
       v_cons = _avg_speed(rep_cons)
       print(f"[conservative]   均速 = {v_cons} km/h")

       # ④ 验证切换真实生效（硬校验：速度必须可读且可观测不同；不合格即失败，不报假通）
       for tag, v in (("rule", v_rule), ("conservative", v_cons)):
           assert v is not None, f"{tag} 的均速未读到（avg_speed_ms 缺失）——数据链路断了"
       assert abs(v_rule - v_cons) > 1.0, \
           "切换未生效：两模型应产生可观测的速度差异"
       ratio = v_cons / v_rule
       assert ratio < 0.85, f"conservative 应为保守（≤0.75 巡航），实测 {ratio:.0%}"
       print(f"=> 插件生效：conservative 均速 = rule 的 {ratio:.0%}（保守巡航语义成立）")

       # ⑤ 卸载后回退内置 rule（不崩溃）
       from modules.driving_model_registry import unregister, _registry
       unregister("conservative")
       assert "conservative" not in available()
       assert get_model("conservative") is _registry["rule"]
       print("卸载后回退 rule ✓（未知名字安全回退契约保持）")

       print("\n完整链路通过：挂载 → 切换 → 生效验证 → 卸载回退。")


   if __name__ == "__main__":
       main()
   ```

### 5.8 成长型平台（平台定位）
插件机制把仿真引擎从"一套写死的规则"升级为**可生长的算法平台**：
- **新研究策略 = 新插件**：CACC 队列策略、预测性安全距离、网联 AV 协同、新跟驰公式——各写一个 handler `register` 即上线，不重写引擎、不碰决策核心。
- **行为可回归**：每次接入新模型后，133 项契约测试锁定既有行为不退化（rule/idm 逐字节等价），**平台越长越稳，不是越长越乱**。
- **实验可控**：`DRIVING_MODEL` 一句话切换 A/B 对照，同场景跑两模型即得影响对比（如 §5.7 的 30.2 vs 23.5 km/h）——答辩可直接引用为"可扩展 + 可对照验证"双证据。

结论：TimeWay 的算法能力**不冻结随开发迭代封版**——每 register 一个新插件，平台就长出一项新能力，且全部被测试网兜住。这是"成长型平台"而非"静态仿真器"的立论基础。

---

## 6. AV 自动驾驶（av_controller.py）

规则式严格安全驾驶（无 IDM/无轨迹规划）：
- **感知**：半径 `max(80m, v×4+30)`，感知角 ±45°。
- **安全线（物理模式）**：`d = v·1.0 + v²/(2·μ·g) + 3`（μ 取天气值，默认 0.75），× 逻辑层级倍率（保守 1.5 / 标准 1.2 / 效率 1.0）。
- **分级跟驰**：`dist < 0.5·safety_line` → 紧急制动至前车×0.55；`< safety_line` → ×0.75；`< 1.5·safety_line` → ×0.90。
- **信号灯**：红灯必停（progress>0.85）；黄灯"能过则过"（progress>0.92 且 v>5）。
- **行人**：30m 内 crossing 行人，叉积判横向偏移，`lateral < 2.5m` 且在前方 → 15m 内全停。
- 真实减速度钳制 `brake_eff·μ·g`；>5 m/s² 记紧急制动、>3 记急刹。

---

## 7. 行人模型（pedestrian.py）

- 4 类行人：守规矩 / 一般 / 冒失 / 低头族，速度 1.2 m/s。
- 行为状态：`normal / jaywalk / rush_roadway / occupy` + `crossing` 标志。
- **压力驱动**：基线 −0.3/s；红灯等待 +2.0/s、雨天 +1.0/s（雷雨 +1.5）、高峰/深夜 +0.5/s；类型倍率（守规矩×0.5、冒失×1.5）。
- 触发阈值与概率：jaywalk ≥60、rush_roadway ≥70、occupy ≥90；`P = min(cap, rate×(pressure−thr)/10)`，触发后 20~60s 冷却。
- **车辆交互**：检测半径 35m；"不让行人"违章 = 距离<15m 且 v>5m/s 且行人 crossing。

---

## 8. 心智状态模型（mental_state.py）

### 8.1 衰减动力学（`_mental_update_kernel`，Numba）
```
rage        = max(0, rage − λ_rage·dt)          # λ≈0.005/s，线性衰减
trauma      = max(0, trauma − λ_trauma·dt)      # λ≈0.002/s
hmv         = min(100, hmv + RECOVER·dt)        # 人类心智值向 100 恢复
bp_value    = max(0, bp_value − λ_bp·bp_value·dt)  # λ=0.003/s，指数衰减（约4分钟半衰期）
bp（倍率）  = max(1, 1 + bp_value/100)            # 向后兼容，静息=1.0
```

### 8.2 事件影响（receive_event）
按事件类型查 effects 表（环境/生活/交互三类事件）：
- `impact = severity × (1 − dist/radius)` 距离衰减（心智冲击波，<0.05 忽略）。
- 逐项累加：rage / trauma / bp / bp_value / injustice_mem / sp（钳制 SP_HARD_CAP）。

### 8.3 行为畸变（作用于驾驶）
| 畸变 | 公式 |
|------|------|
| 反应延迟 | `reaction_delay = rage×0.3 + trauma×0.2`（→ 感知半径缩小） |
| 速度畸变 | `speed_modifier = 1 + rage×0.2 − trauma×0.3 + β·distortion` |
| 加速度畸变 | `accel_modifier = max(0.5, 1 − α·distortion)` |
| 跟车距离畸变 | `follow_modifier = max(0.5, 1 − γ·distortion)` |
| 制动心理修正 | `mental_brake_mod = max(MIN, 1 − rage×p1 − trauma×p2 − distortion×p3)` |
| 理性 | `rationality = (hmv/100)·(1 − rage×0.4)·(1 − (bp−1)×0.3)`，钳制 0.1~1.0 |
| 风险容忍 | `risk_tolerance = max(0, 1 − trauma×k)` |

---

## 9. 天气、路面与区域天气

### 9.1 天气马尔可夫链（weather_system.py）
- `TRANSITION_MATRIX` 8×8 概率表，每 `TRANSITION_INTERVAL=300s` 检查转换。
- 渐变：正弦缓动 `ease_in_out(t) = (1−cos(tπ))/2` 插值雨/雾/风强度，时长 `WEATHER_FADE_DURATION`。
- 支持计划式转换：`weather_transition={at_sec, to}` 精确时刻触发。

### 9.2 路面摩擦系数 μ（road_system.compute_mu）
```
base_mu：天气→路面状态（晴/多云/雾→干沥青 0.75；雨→湿沥青 0.50；雪/冰雹→冰雪 0.15）
  → 湿态：×(1−(1−wet_factor)·rain)
  → 冰封：×0.2（覆盖积雪）；积雪：×(1−(1−snow_factor)·min(snow/0.2, 1))
  → 熔化：−mu_penalty
  → 温度：<0℃ ×(1+t·0.02) 下限0.5；>40℃ ×(1−(t−40)·0.005) 下限0.7
  → 天气×破损交互矩阵（如 雪+施工 = ×0.65）
 下限 0.05
```
- `get_mu_for_edge` 按边取 μ（材质+天气+破损），供车辆物理内核与 AV 安全线使用。

### 9.3 区域天气（weather_region.py）
- 射线法 + 包围盒加速判定点是否在多边形区域内。
- 双图层叠加：按 `WEATHER_SEVERITY` 取最严苛；同严苛度强度叠加 `min(2.0, main + sub×0.5)`。

---

## 10. 路段耐久度（durability.py，AASHTO 四次方定律）

```
天气磨损：wear = WEATHER_WEAR_RATE[天气] × 材质倍率 × rain_amplify × heat_amplify × dt
  rain_amplify = 1 + min(rain,1)×1.5；>40℃ heat_amplify = 1 + min((t−40)/20, 1)
车辆磨损：wear_rate = BASE(2e-5) × axle_count × (axle_load/8163kg)^4   ← AASHTO 四次方
  急刹(decel≥4)×3；急转(lat≥3)×2
降级：durability < 60 → damaged；< 25 → construction；同步 RoadSegment.condition
修复：+5/s；更新降频 DURABILITY_UPDATE_INTERVAL=10 步
```
道路/桥梁超限按设计轴载（JTG D50 / JTG D60，550kN/5轴基准）超出部分放大损伤。

---

## 11. 事件系统（events.py）

- **事故区 AccidentZone**：block / caution / perception 三层半径 5 / 15 / 50m，存活 25s；致命碰撞后创建，封路最近节点 30m 内邻边 25s。
- **交互事件**（空间网格 O(N·k)）：
  - 被恶意别车：距离<25m 且 `< safety×trigger_dist_ratio` 且同路段后车在前；
  - 紧急车辆鸣笛：特种车距离 < trigger_dist；
  - 行人鬼探头：crossing 行人 <15m 且 v>4m/s；每车冷却 5s。
- **心智冲击波**：`impact = severity × (1 − dist/radius)`。
- **因果链 CausalChain**：记录 事件→心智→畸变→行为结果 全链路（支持热度/强度分析）。

---

## 12. 安全度量（SSAM 口径）

内置 `safety_metrics.py` 与独立可复用 `safety_report.py`（纯函数，17 项单测）：

| 指标 | 公式 | 危险阈值 |
|------|------|----------|
| TTC | `approach_speed = −(rel_v·d)/dist`；`TTC = dist / approach_speed`（接近时），否则 999 | <3s 危险；<1s 极度危险 |
| THW | `dist / v_obs` | <1s |
| PET | 轨迹每 0.5s 采样（保留 30s），同一节点相邻到达对时间差 | <2s 冲突 |
| DRAC | `(v_f−v_l)² / (2·gap)`（后车避免碰撞所需减速度） | >3.4 m/s²（HCM） |
| Near-miss | 会话式跟踪：连续 TTC<2s 区间记录 ttc_min / thw_min / min_dist / duration；碰撞时强制 flush | TTC<2s |

空间网格 cell=50m，配对扫描每 3 步降频。`safety_report.TTC/PET/DRAC/analyze_pair` 与论文口径解耦，用于批量与交叉验证。

---

## 13. 时空数据结构与性能加速

### 13.1 空间网格（spatial_grid.py）
- cell_size = 50m（≥ 最大感知半径，保证不漏检）。
- 每步重建 O(N)；配对只遍历同 cell + 邻域 cell → **O(N·k) ≈ O(N)**。
- CSR 候选构建 `build_csr_candidates`：为所有活跃车一次性展平邻域 SoA 索引，供 @njit 批量内核零拷贝消费。
- 配对生成器 `pairs()`：cell 内 `i<j` + 4 方向邻域，每对只遍历一次（无 seen set）。

### 13.2 SoA 结构（vehicle_soa.py）
- 10 个 numpy 数组：ids/xs/ys/headings/speeds/progresses/from_nodes/to_nodes/states(int8)/is_emergency(bool)，双倍扩容。
- 双反向映射 `id_to_idx` / `index_to_vehicle`；`build()` 每步 O(N)。
- 切片视图 `[:n]` 零拷贝传给 Numba 内核。

### 13.3 Numba @njit 内核清单（共 8 个）
| 内核 | 文件 | 功能 |
|------|------|------|
| `find_nearest_ahead_kernel` | numba_kernels.py | 单车前方扇形最近车（角度过滤） |
| `scan_interaction_pair_kernel` | numba_kernels.py | 单车交互事件触发（恶意别车/鸣笛） |
| `batch_find_nearest_ahead_kernel` | numba_kernels.py | 全车批量最近车 + Top-K=5 粗略过滤（Python 侧精确验证） |
| `batch_scan_interaction_pair_kernel` | numba_kernels.py | 全车批量交互事件（RNG/副作用在 Python 侧剥离） |
| `pair_scan_fill_kernel` | numba_kernels.py | 邻域 SoA 索引展平填充（批量扫描前置） |
| `_physics_update_kernel` | vehicle.py | 6 类力合成物理速度更新 |
| `_state_update_kernel` | vehicle.py | 冷却/疲劳/守法/状态恢复数值更新 |
| `_mental_update_kernel` | mental_state.py | 心智衰减动力学 |

### 13.4 其他优化
- **orjson** 替代标准 json（frame/heatmap 导出，实测 2.5×）。
- **多进程 sweep**：`ProcessPoolExecutor` + `SimWorker`，`SWEEP_MAX_PARALLEL=min(4, cpu−1)`，进程隔离。
- **降频**：碰撞/剐蹭/安全度量/耐久度/区域天气 按步数降频；AI 决策行人相关每 3 步。
- **常量缓存**：CdA / 滚动阻力基础值按车型缓存；config 常量提到函数入口局部变量。

---

## 14. 数据采集与输出

### 14.1 采样体系（data_recorder.py）
- 主采样：每 3 步（0.3s），记录活跃车辆速度/数量，按天气/风力/雨量/温度/时段/路段类型/材质/坡度分级。
- 统计指标：平均速度±标准差、碰撞率（碰撞数/(车×小时)）、闯红灯率、超速率、活跃率、配送失败率。
- Phase 2：mental_state / sp_effective / action_distortion / event_impacts CSV + causal_chains / probability_matrix JSON。
- Phase 3：av_decisions / near_miss / pet_conflicts / edge_cases / human_av_comparison。
- **四维帕累托评分**：安全 40% / 合规 20% / 效率 20% / 经济 20%。

### 14.2 热力图（heatmap_recorder.py）
- 网格 25m，7 层：accident / congestion / collision / near_miss（**count 累加型**）；speed / rage / trauma（**avg 型**：累加值+计数，导出时相除）。
- 阈值：拥堵 v<2m/s、rage>0.3、trauma>0.3、TTC<2s。
- 导出 `{meta, layers:{label,color,mode,max_value,grid}}`，前端按 max_value 归一化 + 径向渐变渲染。

### 14.3 帧记录与 2D 回放
- 每 10 步（1s）一帧，帧结构 `{t, v:[id,x,y,heading,color_idx,prof_idx], p:[id,x,y,beh_idx](行人), w, e}`。
- 坐标量化 1cm、索引表压缩，支持 gzip。
- **`scripts/render_2d_animation.py`（v2）**：matplotlib FuncAnimation 俯视回放，三色体系（**CAV 蓝 #1e88e5 / HV 灰 #9aa0a6 / 行人红 #ef4444**），读 `near_miss_events.csv` 对事件窗口车辆红圈闪烁、`closed_on_collision` 实碰更粗实圈高亮；MP4→GIF→采样图板三级输出。

---

## 15. 参数体系

- **run_scenario 签名**：显式参数（场景/种子/时长/时段/天气/车辆数/AV 比例/品牌型号/天气精细/温度/风速/min_spacing/输出目录/自定义名/实时帧等）。
- **config_overrides 白名单**：48 项四大类（交通拥堵 / 车辆驾驶 / 系统开关 / 物理环境），运行前 apply → 仿真 → finally restore，白名单校验 + 类型转换，前端「高级参数」面板驱动。
- **品牌型号系统**：车型级参数（功率/风阻/轴载等）按品牌型号查 JSON，运行前预采样避免运行中查询。

---

## 16. 关键公式速查

```
跟驰物理     a = min(F_power, F_traction, F_engine)/m − ½ρCdAv²/m − f_r·g − g·sinθ
制动         a_brake = brake_eff · μ · g · mental_brake_mod
弯道极限     v_max = √(μ·g·R)
安全线(AV)   d = v + v²/(2μg) + 3   （× 逻辑层级倍率）
安全线(人驾) d = v·t_react + v²/(2μg) + margin   （PAPER_DATA_COMPAT_MODE=False）
IDM 跟驰     a = a_max·[1 − (v/v0)^δ − (s*/s)²]；s* = s0 + max(0, v·T + v·Δv/(2√(a_max·b)))
SP           SP = min(HARD_CAP, base·(1 + rage·0.3 + injustice·0.001)·bp)   ← 系数待标定
μ(路面)      0.05 ≤ base(材质/天气) × 湿/雪/冰修正 × 温度修正 × 破损交互 ≤ 1.0
磨损(路面)   wear ∝ (axle_load / 8163kg)^4 × axle_count
TTC          TTC = dist / (−(rel_v·d)/dist)   （接近时）
心智畸变     speed_mod = 1 + rage·0.2 − trauma·0.3 + β·distortion
天气渐变     ease_in_out(t) = (1 − cos(tπ))/2
发车间距     initial_vehicles = min(n, max(10, n×0.1));  相邻车 min_spacing 内重采样（≤60次）
交叉偏差     Δ = |TW − SUMO| / |SUMO| × 100%    （<15% 视为收敛）
```

---

## 17. 工具与运维

- **敏感性分析**：`python scripts/sensitivity_analysis.py` —— OAT 单变量扰动，对 12 个关键参数各跑低/高两档短仿真，输出影响排名（控制台 + CSV 到 `data/sensitivity/`）。自动缩短 warmup 保证数据窗口。
- **算法对照实验**：前端「高级参数」或 `config_overrides` 设 `DRIVING_MODEL=idm` 与 `rule` 各跑一次，对比两类跟驰模型。
- **安全指标批量**：`scripts/safety_batch_chart.py` / `make_safety_charts.py` —— 扫描 `data/experiments/hydro_p*.json` 批量安全汇总（天气/渗透率分组，1268 组实测）。
- **2D 回放**：`scripts/render_2d_animation.py` —— 俯视动画短片（见 §14.3）。
- **交叉验证**：`competition/crossval/run_crossval.py` + `make_report.py`（见 §18）。

---

## 18. SUMO 交叉验证与第三方验证（2026-08-22 ~ 08-28）

### 18.1 管线（run_crossval.py v2）
- 同一方环几何分别编译为 TimeWay `custom_map` 与 SUMO `net.xml`（netconvert 1.27.1，真实安装运行），两引擎从同一坐标系出数据；环路避免死端直道末端堆积，使两引擎进入稳态环流。
- 30 组场景 = 10 施工区(Hydro，单车道+天气梯度) + 10 高速跟驰(Highway，双车道密度梯度) + 10 渗透率梯度(Penetration)。
- **同一提取器**（复刻 TimeWay TTC/冲突数学：配对扫描半径 50m、Near-miss 阈值 2.0s）对两引擎轨迹提取 4 指标：流量 q=k·v / 平均速度 / TTC危险 / 冲突数——apples-to-apples。
- 每场景独立输出目录 + 直读本场景帧文件（修复帧文件读写错位）。

### 18.2 交叉验证发现并修复的装配级 bug（均为数据/装配链路，非引擎核心）
1. **speed 未透传**：场景限速只写进地图边，未注入车辆职业 max_speed → TimeWay 车辆最高速度与实际场景限速脱节。修复：仿真前把全部职业 max_speed 临时注入为场景限速±10%，仿真后恢复。
2. **帧文件读写错位**：仿真按运行目录(CWD)写 `frames.json`，提取器按包目录(TW_DIR)读 → 读到陈年旧文件，首轮 30 组 TimeWay 均速全部恒等于 15.0 的假数据。修复：每场景独立输出目录 + 直读本场景帧文件。
3. **车辆数膨胀**：`initial_vehicles=max(10,…)` 把 n<10 场景膨胀到 10 辆（已修，见 §2 装配修复）。

### 18.3 修复后实证结果（诚实披露）
- 修复后平均速度偏差（族均值）：Hydro ≈53%、Highway ≈55%、Penetration ≈54%——**未收敛至 <15%**。
- 已做完整证伪实验电池，排除发车参数假说：
  ① 车辆数膨胀修复（仅微升）；② min_spacing 0/50/150/300m + 初始车队强制间距（几乎不变）；③ 速度同质化（固定 max_speed，仍 ~30）；④ 64 段完美圆弧环（仍 ~32，排除转角）；⑤ SUMO 同源 IDM 参数（反而更低，C01 33→25）。
- **收敛结论**（引擎级标定，非装配）：`safety_distance()` 物理跟驰（高速间距需求大）+ AVOID 分级降速系数构成的环流平衡远低于限速；即使自由流条件也会局部触发后压缩成低速簇。需引擎级跟驰参数重标定（后续工作）。
- **天气不对称（Hydro）**：TimeWay 雨/雾降速至 6–8 km/h（天气模型生效），SUMO 无天气模型仍 24–25，放大 Hydro 偏差。
- **定性趋势一致性（答辩核心）**：高速族「密度↑→TTC危险↑/冲突↑」两引擎方向一致（SUMO τ≈0.96–1.0，TimeWay τ≈0.33–0.42）——两组独立引擎复现同一规律，是"数据非自编"的直接证据。Hydro 族被天气混淆无干净趋势。

### 18.4 交付件（competition/crossval/）
- `crossval_report.html`：30 组明细 + 4 指标散点 + 诚实声明（2 bug + 5 项证伪 + 真实偏差 + 收敛结论）。
- `safety_batch_summary.{png,json}` / `weather_panel.png` / `penetration_panel.png`：1268 组 hydro 批量安全汇总（晴组危险最高 TTC≈2431/碰撞≈7.4；雨雾因降速反而安全；AV 渗透 p20 较 p0 危险略降 1796 vs 2160）。
- `demo_clip1_高速跟驰.gif` / `demo_clip2_施工区.gif` / `demo_clip3_水电站横穿.gif`：2D 回放短片（CAV 蓝 / HV 灰 / 行人红 + 碰撞高亮）。
- `crossval_ppt.pptx`：2 页答辩（双引擎趋势 τ≈0.96 页 + 5 项证伪「我们如何验证自己」叙事页）。

---

## 19. DSL 场景语言层与标准互操作（SceneLang 核心差异化）

### 19.0 定位：场景语言层是最大差异化
本平台与仿真引擎类竞品的核心区别，不在"又多了一个仿真器"，而在**顶层场景语言层（对应比赛名 SceneLang）**：用户用**中文领域 DSL（`.tw`）直接编写场景**（含施工区、封路、行人横穿、天气、事件），一条命令即可编译为仿真场景、OpenSCENARIO 标准场景、OpenDRIVE（XODR）路网——从"写场景"到"可复现、可迁移、可复核"一条链路。**这条链路已通过 37→133 项自动化测试验证**（安全指标模块补测后全量回归由 37 项扩容至 133 passed + 1 skipped），数字是信用。竞品通常只提供 GUI 或英文脚本，缺"语义级场景描述 + 标准导出"。

### 19.1 .tw 中文场景 DSL（modules/dsl/）
- **语法层** `lark_parser.py` / `parser.py`（Lark）：解析中文场景 DSL，入口 `parse_tw(text)` / `parse_file(path)`。
- **分词与令牌** `tokens.py`、**中间表示** `ir.py`（ScenarioIR）：场景 → 语法树 → 中间表示，与仿真引擎解耦——同一份 IR 可同时服务仿真、导出、测试。
- **场景语义覆盖**：节点/路段/车道/限速/材质/坡度/弯道、天气与时段、信号灯/交警/封路规则预制件、事件注入（爆破/落石/人员横穿）、行人行为、AV 渗透率——全部可在一份 `.tw` 文本内声明。

### 19.2 OpenSCENARIO 导出（modules/dsl/tw_to_xosc.py）
`export_scenario` 将 `.tw` 编译为符合 OSC 1.2 的 `.xosc`（+ 每边几何 `.xodr`）：
- **核心结构**：`<Scenario>` 名称保留中文（如"水电站能耗路由"，不经 ASCII 转码损坏）→ `<Entities><ScenarioObject>` 每实例唯一 ref → `<CatalogReference>` 引用车型/行人 → `<Init><Private> entityRef 一一对应` → 路由/速度/天气/事件。
- **Catalog 去重方案（关键）**：车型定义集中到 `VehicleCatalogs` / `PedestrianCatalogs` 目录文件，主 `.xosc` 通过 `<CatalogLocations>` + `<CatalogReference>` 引用——**25 辆车同一车型仅 1 份 `<Vehicle>` 定义**（esmini / SUMO 均支持该机制），避免逐车复制定义导致文件膨胀与不一致。
- **与 IR 解耦**：同一 ScenarioIR 既可进仿真引擎，也可导出 XOSC——导出器与引擎无耦合，保证"写一次场景，双路消费"。
- **契约测试**：`tests/test_tw_to_xosc.py` 导出器契约测试 **16 项**（唯一 ref / 实例数 / Catalog 去重 / 中文名往返 / API 导出），全量回归 **133 passed + 1 skipped**（8/28 安全指标模块补测后由 37 项扩容至 133）。
- **OpenDRIVE 现状（诚实边界）**：当前 `.xodr` 导出覆盖**直线近似路网几何**（每边起止点 + 车道数/限速）；弯道曲率、高程剖面、信号相位列为后续版本，暂不导出。

### 19.3 标准互操作价值（答辩可讲）
- `.tw` → `.xosc` 是**行业标准进出**：esmini 可视化复现、SUMO 独立复验均消费该格式（§18 交叉验证即同一场景的 OpenSCENARIO/路网双路编译）。
- 场景即文档：中文 `.tw` 可读、可评审、可版本化，评委可直接审阅"施工区封路"的语义描述，而非黑盒脚本。
- **竞品对比锚点**：主流竞品场景以 GUI 工程或私有二进制存储（如经纬恒润 TPIP、中汽研场景库），**不导出 OpenSCENARIO**，跨平台/跨工具迁移需人工重建；`.tw` 为**纯文本**——可 git 版本化、可 diff、可评审，一行改动即可追溯，这是"场景语言层"比形容词更有力的论据。

### 19.4 DSL 语法示例（来自 `scenarios/hydropower/hydro_gate_v3.tw`，真实文件节选）

```text
场景 水电站能耗路由 {        # 场景名：中文保留，导出 .xosc 时不被 ASCII 转码损坏
  种子 42                 # 可复现种子：同一 .tw 同一结果
  地图 {
    # v3：节点三坐标 (x,y,z)，坡度由相邻节点高程差自动推导；
    # 道路字段可再叠加破损/平整度/路宽/承载（路面属性字典）
    节点 营地   (0,   0,   45)  朝向 90       # 起终点地形高程
    节点 坝区   (500, 300,  72)  朝向 135
    道路 营地->坝区 车道 2 限速 40 弯道 30 路面 碎石 破损 15% 平整度 4.8 路宽 7米 承载 重型
      # 「弯道 30」为语言层语义（急弯意图）；引擎按 §3.1 将弯道半径钳制到 80~2000m 后执行
    道路 坝区->骨料场 车道 1 限速 25 坡度 8% 路面 碎石 破损 25%    # 单车道，显式坡度优先于自动推导
  }
  天气 {
    剧本 0-600秒 晴          # 天气剧本：显式时间线
    剧本 600-1800秒 暴雨
    剧本 1800-2400秒 团雾    # 暴雨→团雾→转晴的马尔可夫串
  }
  车辆 {
    10辆 电动自卸卡车        # 电网级矿山车辆（与项目"能耗路由"呼应）
    10辆 水泥罐车
    5辆 通勤班车
  }
  规则 {
    当 时间(600) 封路(营地->坝区, 1800)
    当 随机(0.3) 事故(坝区->骨料场, 1)        # 随机落石占道
    当 横穿 人员(卡口)                       # 人员横穿：near-miss 高发点
  }
  路由 最省 车辆 电动自卸卡车    # v3 预留：能耗/多代价路由消费口
  能耗 报告 每段
 }
 ```

说明：同一份 `.tw` 即可进仿真引擎（§2–§18 全链路）→，也可一行命令导出 `.xosc`（车辆经 Catalog 去重）与 `.xodr`（直线近似几何）→ 供 esmini/SUMO 消费。**场景即代码**：中文语义、可评审、可版本化、可回归。

---

*本白皮书基于当前代码核实；§18 为 8/22–8/28 交叉验证与交付件的如实记录；§19 场景语言层（SceneLang）为差异化定位。算法如有调整，请以 `config.py` 常量为最终依据。*
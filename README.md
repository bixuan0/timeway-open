# TimeWay（时途）— 混行安全微观交通仿真平台 · 开放核心版

> 基于动态驾驶员心智状态的微观交通仿真平台。本仓库为 **Open Core（开放核心）** 的公开层。

TimeWay 在微观交通仿真中引入了**驾驶员动态心智状态建模**（路怒、创伤记忆、行为压力等），
构建「事件触发 → 心智演化 → 动作畸变 → 物理行为」四层耦合架构，支持 M1000 级（1000 辆车）
高并发仿真，并提供 OpenDRIVE 1.7 / OpenSCENARIO 1.2 标准场景导出。

## 本仓库包含什么（开放层）

| 模块 | 说明 |
|---|---|
| `docs/TimeWay_算法白皮书.md` | 算法与方法论白皮书（仿真架构、确定性验证、SUMO 交叉验证） |
| `docs/md2html_whitepaper.py` | 白皮书 Markdown → HTML 转换工具 |
| `dsl/` | `.tw` 场景描述语言（DSL）解析器：词法、语法、IR、OpenSCENARIO 转换 |
| `demos/osc_demo/` | 标准场景示例（OpenDRIVE `.xodr` / OpenSCENARIO `.xosc`，含水电站门禁场景） |
| `demos/crash_edge_demo/` | 「崩溃边缘」交互式可视化 demo |
| `examples/` | `.tw` 场景示例（水电站施工交通安全场景） |

## 本仓库不包含什么（私有 / 商业层）

以下构成 TimeWay 的核心竞争力与知识产权，**不在本公开仓库中**，需通过商业许可获取：

- 核心仿真引擎（`numba_kernels` / `vehicle_soa` / `spatial_grid` 等 Numba JIT 批量内核）
- 驾驶员心理演化模型与耦合逻辑（`mental_state` / `env_coupling` / `rule_engine`）
- 七层安全热力图算法、安全度量与评估体系
- 企业级功能：大规模批处理、横向课题定制（如水电站施工交通安全 MVP）、API 与 Web 服务层

## 许可证

- 本公开层以 **GPL-3.0** 发布（详见 `LICENSE`）。
- 衍生作品须同样以 GPL-3.0 开源；你可自由学习、修改、再分发。
- **商业 / 企业使用**（闭源集成、规模化部署、定制开发、教学包授权）需另行签署商业许可，
  与 GPL 开源许可不冲突（双许可模式）。

## 快速开始

1. 阅读 `docs/TimeWay_算法白皮书.md` 了解方法论与确定性验证结果。
2. 查看 `dsl/` 了解 `.tw` 场景语言，参考 `examples/*.tw` 编写自己的场景。
3. 打开 `demos/crash_edge_demo/index.html` 体验交互式可视化。
4. 用任意兼容 OpenDRIVE / OpenSCENARIO 的工具加载 `demos/osc_demo/` 中的标准场景。

## 商业授权与联系

如需场地授权、教学包、企业定制或大规模部署，请通过赛事/项目官方渠道联系 TimeWay 团队，
洽谈商业许可（Freemium：基础层开源，高级/企业层付费）。

## 免责声明

本仓库仅供教学与科研用途演示。仿真结果不应直接用于真实交通工程设计决策。

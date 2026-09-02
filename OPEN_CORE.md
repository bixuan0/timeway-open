# 开放核心（Open Core）边界说明

本文档界定 TimeWay 的**开放层**与**商业层**的精确边界，供使用者、贡献者与商业客户参照。

## 开放层（本仓库）

> 授权双轨：**文档 CC BY 4.0 · 代码 GPL-3.0**。

### 文档与规范（CC BY 4.0）
- `docs/TimeWay_算法白皮书.md` — 方法论白皮书（架构、确定性、SUMO 交叉验证 §18、DSL §19）
- `docs/CC-BY-4.0.txt` — 白皮书授权文件（Creative Commons Attribution 4.0 International）
- `docs/md2html_whitepaper.py` — 白皮书转换工具

  文档内容以 **CC BY 4.0** 授权，转载 / 改编 / 商用均须注明出处（TimeWay Team · 玄尊）。

### 场景语言（DSL）
- `dsl/__init__.py`
- `dsl/tokens.py` — 词法记号定义
- `dsl/ir.py` — 中间表示
- `dsl/parser.py` / `dsl/lark_parser.py` — 语法解析（Lark）
- `dsl/tw_to_xosc.py` — `.tw` → OpenSCENARIO 转换

### 演示与示例
- `demos/crash_edge_demo/` — 交互式可视化（自包含 HTML）
- `demos/osc_demo/` — 标准 OpenDRIVE / OpenSCENARIO 场景文件
- `examples/*.tw` — 场景语言示例

> 代码（DSL 解析器、demo 等）以 **GPL-3.0** 授权，可自由使用、修改、再分发，衍生代码作品须保持 GPL-3.0。

## 商业层（不在本仓库，需商业许可）

| 类别 | 内容 |
|---|---|
| 仿真内核 | Numba JIT 批量内核、SoA 数据布局、空间网格索引 |
| 心智模型 | 驾驶员心理演化、事件耦合、规则引擎 |
| 安全分析 | 七层热力图、安全度量、评估与报告 |
| 工程能力 | 大规模批处理、横向课题定制（水电站等）、API / Web 服务 |
| 数据集 | NGSIM 校准数据、实验结果仓库 |

商业层以**双许可**提供：在 GPL-3.0 开源层之上，企业客户通过商业许可获得
闭源集成、规模化部署与定制支持的权利。

## 获取企业版

通过赛事/项目官方渠道联系 TimeWay 团队，洽谈商业许可（Freemium 模式）。

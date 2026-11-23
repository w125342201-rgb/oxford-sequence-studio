# 历史文档归档（L3，冻结）

这里存放 1.6.0–1.7.7 期间的原始报告与测试输出，以及**此前其他 AI agent 开发时留下的记录**（`.workbuddy/memory/`）。**文件内容未做任何改写**，仅移动位置，以保留当时的证据与措辞。

> **这些文件不是当前行为，也不是需求来源。** 当前规定见 [开发规范](../开发规范.md)；旧说法是否仍有效，查规范 §10 冲突裁定表。

## 1. 归档说明

1. **未经许可不得修改本目录任何文件。** 需要补充说明时，追加到本文件末尾的"后续说明"。
2. 本目录文件正文中的相对路径**以仓库根目录为基准**（例如 `audit/regression_1.7.6.txt`、`original_sources_sha256.json`），不是相对本目录。
3. 归档文档之间互相引用的 `*.md` 已随目录整体移动，链接仍然有效。
4. 根目录 [README_使用说明.md](../../README_使用说明.md) 中指向它们的链接已更新为 `docs/archive/…`。
5. 这些是**历史快照**：测试计数、默认值、行为描述只对**对应版本**成立。
6. 被删除的功能（SR830、DC、I–V、气压扫描）在旧文档里仍有描述，那是历史状态，不是待恢复项（规范 §1.3）。
7. **旧文档里的"待办 / 建议 / 下一步"不构成需求。** 已有一次教训：据此重提了已否决的 Delta pre-flight（规范 §8 R-001）。

## 2. 报告索引

| 文件 | 版本 | 类型 | 内容 | 现规范裁定 |
|---|---|---|---|---|
| [CHANGELOG_1.7.7.md](CHANGELOG_1.7.7.md) | 1.7.7 | 变更 | 默认工作点上移到设备上限侧；四条使用边界 | §9.6 / C-011…C-013 |
| [CHANGELOG_1.7.6.md](CHANGELOG_1.7.6.md) | 1.7.6 | 变更 | 人工接受环境值、环境通道分级、计数语义 | §3.6 / §3.7 |
| [CHANGELOG_1.7.5.md](CHANGELOG_1.7.5.md) | 1.7.5 | 变更 | 磁场判据、角度恢复、关源三查、**Delta 配置复用（已撤销 W-002）** | §8.3 / C-003 |
| [CHANGELOG_1.7.4.md](CHANGELOG_1.7.4.md) | 1.7.4 | 变更 | 审计 D1–D11；CSV 48 列（历史值） | §6.2 / C-004 |
| [CHANGELOG_1.7.3.md](CHANGELOG_1.7.3.md) | 1.7.3 | 变更 | 采集可靠性与数据追溯 | §5.5 |
| [DEVICE_SELECTION_1.7.2.md](DEVICE_SELECTION_1.7.2.md) | 1.7.2 | 专项 | 设备选择不同步的核查与修复 | §9.3 / D-007 |
| [RANGE_REVIEW_1.7.1.md](RANGE_REVIEW_1.7.1.md) | 1.7.1 | 专项 | 4462 六档量程官方核对；±42.4 V 例外 | §3.5 / D-009 |
| [HARMONIC_SELECTION_1.7.0.md](HARMONIC_SELECTION_1.7.0.md) | 1.7.0 | 专项 | 1f+2f / 1f+3f；3f 符号推导 | §3.3 / D-011 |
| [PHASE_DEFAULTS_1.6.9.md](PHASE_DEFAULTS_1.6.9.md) | 1.6.9 | 专项 | marker=90°、1f=0°、2f=−90° 的由来与迁移 | §3.4 / D-010 |
| [REVIEW_6221_4462_1.6.8.md](REVIEW_6221_4462_1.6.8.md) | 1.6.8 | 审计 | 6221+4462 外部示例复核；低频读取等待 | §9.1 |
| [AUDIT_4462_20260924.md](AUDIT_4462_20260924.md) | 1.6.7 | 审计 | 4462 数字锁相深度复核（最完整的算法文档） | §3.3 |
| [CHANGELOG_1.6.7.md](CHANGELOG_1.6.7.md) | 1.6.7 | 变更 | 短窗泄漏、频点取整、实际采样率 | §3.3 |
| [INSTRUMENT_AUDIT.md](INSTRUMENT_AUDIT.md) | 1.6.0 | 审计 | 与原 GUI 逐项对照；**"保留除数 2"（已作废 C-001）** | §3.2 / C-001 |
| [VALIDATION.md](VALIDATION.md) | 1.6.0 / 1.6.4 | 验证 | 早期验证记录（52–73 项时代） | §7.3 / C-007 |

## 3. 历史测试输出（evidence/）

原在仓库根目录的 7 份早期测试结果，已移入 [evidence/](evidence/)：

| 文件 | 时代 |
|---|---|
| [test_results.txt](evidence/test_results.txt) | 1.6.0 前后（52 项时代） |
| [test_results_field_retry.txt](evidence/test_results_field_retry.txt) | 磁场重试用例 |
| [test_results_four_channels_harmonics.txt](evidence/test_results_four_channels_harmonics.txt) | 四通道谐波 |
| [test_results_front_panel_wait.txt](evidence/test_results_front_panel_wait.txt) | 前面板等待 |
| [test_results_pxi_channels.txt](evidence/test_results_pxi_channels.txt) | PXI 通道映射 |
| [test_results_unlimited_wait.txt](evidence/test_results_unlimited_wait.txt) | 无限等待 |
| [test_results_xy_phase.txt](evidence/test_results_xy_phase.txt) | X/Y 相位 |

现行回归记录仍在仓库 `audit/regression_<版本>.txt`（**未移动**，因为历史报告以仓库根为基准引用它们）。

## 4. 后续说明

### 2026-11-11 文档体系重构
- 本目录由 `docs/changelog/` 更名为 `docs/archive/`，纳入 L3 归档层。
- 根目录 7 份 `test_results*.txt` 移入 `evidence/`。
- 新增 [开发规范](../开发规范.md) 作为 L0 唯一权威；旧文档的每一处过时说法已在规范 §10 登记编号（C-001…C-025）。
- 归档文件本身**未改动**，仅 `docs/archive/README.md`（本文件）被重写以反映新路径与裁定索引。

### 2026-11-07 Delta 流程回退（事后记录）
- 事件与处置见 [开发规范 §8.3（W-001/W-002）](../开发规范.md) 与 [reference/11](../reference/11-版本历史.md) §5。
- 结论：Delta 执行流程保持 1017 版，**不加入任何逐点串口校验或设置复用**。

## 5. 已取消的计划（plans/）

| 文件 | 日期 | 状态 | 结论 |
|---|---|---|---|
| [PLAN-20261111-offset-cancel.md](plans/PLAN-20261111-offset-cancel.md) | 2026-11-11 | **已取消，未实施** | 参考/窗口 180° 翻转在数学上无消偏效果；真反流需实机验证 6221 负幅值。用户决定暂不开发，见规范 §8.4 H-001 |

## 6. 其他 agent 的开发记录（`.workbuddy/memory/`）

这是此前其他 AI agent 在本项目上工作时留下的记忆文件，2026-11-11 由用户整理到本目录。**它们是历史证据（L3），不是需求来源，也不代表当前行为。**

| 文件 | 内容 | 注意 |
|---|---|---|
| [memory/2026-11-01.md](.workbuddy/memory/2026-11-01.md) | 1.7.7 默认工作点调整的决策过程（0.237 T/min、0.01 T、0.1 A、105 V、0.105 A） | 与规范 D-012 一致 |

使用规则：

1. 其中的"待办 / 建议 / 下一步"**一律不构成需求**（规范 §0.3 第 3 条）；
2. 与规范或当前代码冲突时，以规范/代码为准，并在规范 §10 登记；
3. 文件本身不再修改；有新结论写在规范 §8 台账，不要回写这些记忆。

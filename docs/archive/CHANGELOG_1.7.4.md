# Oxford Sequence Studio 1.7.4

日期：2026-10-21。基于已交付 1.7.3，落实代码审计复核后的修改。

## 用户明确保留的行为

源顺从、过温、关键设置回读不一致等源异常，继续按原逻辑使整轮运行失败并执行清理。没有把这些错误转换为单电极 NULL。6221/2182A 驱动文件与 1.7.3 逐字节相同；新增回归测试验证顺从事件仍触发整轮失败。

普通无效读数或采集超时仍按原规则记 NULL 后继续，不引入连续无效点熔断，不自动永久跳过电极。关源与矩阵切换前的硬件确认保留，不采用软件“已经关闭”缓存替代实际确认。

## 审计项修复对应

| 项 | 1.7.4 行为 |
| --- | --- |
| D1 点列量化 | 生成器严格校验起点、终点和步长均符合现有点列精度，非法输入报错；不再静默取整。默认包含终点，末段可短于步长；取消“包含终点”后严格等间距。升扫、降扫都不越界。 |
| D2 等待提示 | RTOS 提示正常励磁及按设定速率估算的剩余时间；PAUS 提示检查暂停原因；HOLD/CLMP 未到位提示检查设定；RTOZ 提示正在回零；未知状态不擅自重发运动命令。容差内等待稳定不要求操作前面板。 |
| D3 | 按用户决定保留源异常停整轮，普通无效读数继续。 |
| D4 环境归属 | 保留每点一行、多电极分列格式，新增 routeN_ 独立环境、采集时间和状态列。公共环境列明确属于第一个电极。 |
| D5 VTI/气压 | CSV 和日志恢复工具均导出 vti_k、pressure_mbar。 |
| D6 计数 | 容器不再产生虚构操作。分别显示执行操作数、采集次数、CSV 行数；run.json 同时记录 planned_workload。 |
| D7 超时文案 | Delta 标为单次采集超时，说明该次 NULL 后继续；环境标为等待提示阈值；PXI 隐藏未使用的 Delta timeout_s 字段。保留原配置键名。 |
| D8 恢复重试 | gate 统一用 retry_io 恢复测量；通信超时重试，停止可退出，非可重试错误仍抛出。 |
| D9 主动停止 | 已完成电压仍保存，跳过环境回读时标 stopped，不再与环境通信失败 unavailable 混同。 |
| D10 路由冲突 | 开启逐点电极表时，拒绝启用显式 route/channels 节点。被禁用的节点不构成冲突。 |
| D11 滚轮 | 每个应用只注册一组全局处理器，窗口销毁移除登记，避免累积；子控件上的滚动仍有效。 |

附加整理：删除未使用的 step 状态和重复条件、避免创建重复电流列表；固定模式和固定四通道用只读标签展示；电压列元数据改为显式键值构造；硬件环境常规轮询共用一秒节流缓存，采集前后必要的新鲜回读不受此限制；PXI 模拟噪声采用跨点生成器，支持调用方传入已设种子的 Generator。

为保持旧方案兼容，delta_divisor=1 和固定 daq_channels 的序列化字段保留，不将其当作新硬件设置。历史迁移处理保留。重复关源的跨层防护不作为纯性能冗余删除。

## CSV 列与分析规则

旧电压列名保留；新增列会使列号移动，请始终按列名读取。

通用新增列：vti_k、pressure_mbar、environment_read_utc、environment_timing、measurement_error、logical_point、route_index。4462 四通道、双谐波的标准 CSV 现在为 **48 列**。

逐点电极表中，每组电极增加 14 列，以 route1_、route2_……为前缀：

- time_utc、acquisition_start_utc、acquisition_end_utc、environment_read_utc；
- temperature_k、vti_k、field_t、pressure_mbar、angle_deg、current_a；
- measurement_status、measurement_error、environment_status、environment_error。

这些前缀按运行时 point_routes 的顺序确定。run.json 的每个电压列元数据包含 detail_prefix，例如对应 route2_。重复电极接线生成的不同电压列仍使用独立前缀。

公共 temperature_k/field_t/vti_k/pressure_mbar/angle_deg 和 environment_read_utc 取首组电极，environment_timing 标为 first_route_after_point；公共 acquisition_start_utc 至 acquisition_end_utc 表示本轮从首组开始到最后一个已完成电极结束的区间。环境回读在各自采集后进行，不是同步采样，也不保证回读的多个环境量在完全相同时刻取得。

measurement_status 的表级含义：

- valid：全部电极均有有效电压；
- partial_invalid：部分电极无效，其余有效；
- invalid：全部已完成且都无效；
- incomplete：本轮电极尚未全部完成（例如中途停止或失败）。

环境状态逐电极取 ok / unavailable / stopped；表级混合状态为 partial_unavailable。分析时按电极的电压、测量状态和环境状态筛选，不因表级 partial_invalid 就丢弃其他电极的数据。NULL 表示已尝试但无效；空白电压表示尚未完成该电极。缺失环境的有效电压仍可保留，但不能当作已知温度/磁场的测量点。

恢复命令仍为：

```bash
python audit/recover_points.py "运行目录/events.jsonl" "recovered.csv"
```

恢复输出为逐次采集的长表，包含逻辑点和路由索引；不覆盖原表，不将环境空值伪造为有效回读。

## 工作量与上限的含义

执行操作数统计环境目标设定、等待、路由、每次采集，不等于底层 SCPI 命令数，也不是预计机时。repeat 容器自身不计操作；sweep 的每个目标仍需一次真实环境操作；逐点电极表每个电极包含一次路由和一次采集。

示例 3 温度 × 5 磁场 × 1 次采集：33 个操作、15 次采集、15 行 CSV。两层各重复 300 次的单次采集：90,000 个操作，可以通过默认 100,000 上限。若每个点还有环境设定或电极切换，操作数仍可能超过上限；界面会准确说明，不把 90,000 次采集谎称为 90,000 个总操作。

## 兼容性与验证

- 旧 JSON 方案结构版本保持 7。存储的显式点列按原值保留；本次 D1 校验作用于“生成点列”，不会在导入时擅自重新量化历史点列。
- 新生成不合精度的点列会报错；逐点表与显式路由混用的旧方案需删除或禁用冲突节点。
- 160 项 unittest：159 通过，1 项需要显示环境的实绘测试跳过。新增 17 项测试包含多项子场景。记录在 audit/regression_1.7.4.txt。
- 独立解析波形与 200 次噪声试验通过，结果在 audit/numeric_results_1.7.4.json。
- 未连接真实 6221/2182A/4462/Oxford/旋转控制器；测试通过不等于实机联调完成。已有幅值、相位与时钟校准边界保持不变。

解压到新目录后运行 start_windows.bat，再载入原方案。包内既有历史报告属于对应旧版本；当前行为以本说明为准。

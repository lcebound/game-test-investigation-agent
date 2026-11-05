# Harness 第二批实现：JSON 字段导航与定向召回

日期：2026-11-02。接续[第一批实现](harness-phase1-implementation-2026-11-02.md)，落实[渐进方案](super-agent-migration-plan-2026-11-02.md)中 P1 的有界字段读取及部分工具展示优化。

本批沿用 Python 原生调查流程、Evidence/Artifact、读取预算、调用结果日志和现有 AssertionVerifier；没有引入 DSH 运行时。借鉴 Super-agent 的资源引用与按需展开方式，将完整原文留在存储中，让模型先拿到有界目录，再读取问题相关字段。

## 本批解决的问题

1. 大 JSON 的相关字段位于预览之外时，可以直接定位字段，不必顺序翻完整文件。
2. 不知道字段路径时，可以分页查看对象、数组的直接子项。目录包含 JSON Pointer、类型和原文位置。
3. 查找较早用户要求时，可以搜索完整输入账本，不只搜索窗口中的卡片预览。
4. 长要求的连续回读计入任务进展，不再因为超过三个新读取区间而过早进入停滞收尾；重复读取仍受原来的停滞与预算约束。

## 接口与数据语义

扩展已有 agent_read_context，增加可选 pointer；没有再增加一个模型工具：

```json
{"ref":"evidence/<evidence_id>","pointer":"","limit":10,"max_bytes":4096}
```

pointer 为空字符串表示 JSON 根节点。对象、数组返回 entries 和 next_offset，可以用 offset/limit 继续查看直接子项；目录不是递归展开的整棵树。

```json
{"ref":"evidence/<evidence_id>","pointer":"/queue/depth","expected_sha256":"<64位小写哈希>"}
```

字段路径采用 RFC 6901，支持 ~0、~1 转义和数组下标。不能与 query 或非零 byte_offset 混用。省略 pointer 时，保留原来的字面搜索与 UTF-8 字节分页；pointer=null 与省略相同。

字段返回中：

- result 是源文件内连续的原始 JSON 字面量片段，包含源文件中的引号、转义及数值写法，不是重新序列化的值；
- byte_offset、value_bytes 定位完整字段值，excerpt_sha256 校验 result；sha256、source_revision 绑定原始证据；
- entries 是导航信息，不能整体当作连续原文引用；其中的 literal_preview 明确标注截断；
- next_offset 只用于直接子项目录，next_byte_offset 用于继续读取原始文件字节。后者为 null 仅说明本次选中的字段值读全，不说明整个产物或远端分页完整；
- scope 明确限制为当前保存产物的当前字段。空数组、成功字段或字段读全不提升结论等级。

长字符串仍可能截断，后续可省略 pointer，使用返回的原始字节位置继续回读。超过路径长度/深度限制的子项不返回可调用 pointer，仍保留字节定位信息。

输入目录支持：

```json
{"ref":"inputs","query":"恢复条件","offset":0,"limit":10,"max_bytes":4096}
```

目录 query 对完整原始输入作大小写折叠后的字面匹配，卡片仍是有界前缀，未必包含匹配词。按 next_offset 继续搜索；searched_from_offset、scanned_items、complete 说明本次扫描范围。complete 只表示已经扫描到当前目录末尾，不覆盖起始 offset 之前的输入。读取单条 input/<id> 时，query 继续使用原有的区分大小写字面搜索。

## 复用与执行边界

native_json.py 复用现有断言模块的 JSON Pointer 和重复键拒绝逻辑，使用 Decimal 解析小数；不通过 float 重写原始数值。现有 AssertionVerifier 继续独立回查源证据并生成字段主张，读取工具不承担报告判定。

字段读取限制为最多 4 MiB 原始 JSON、32 层 Pointer、20 个直接子项，以及最多 4,096 UTF-8 字节的完整序列化响应。拒绝重复键、NaN/Infinity、尾随数据、无效路径和过小的输出封套。字段元数据也计入响应上限，max_bytes 过小会明确报错。

解析在后台线程进行，等待上限取剩余调查时间与 5 秒的较小值；数据库归属校验在调查线程完成，并在等待结束后重新核对证据。超时返回受控错误，但 Python 线程不能强制终止，已开始的解析可能继续到结束；4 MiB 输入上限仍然有效。这不是进程级硬资源隔离。

所有读取继续先预留原有预算，核对调查归属、来源哈希与版本。已缓存结果在来源撤回、哈希或版本变化时不再复用；旧调用日志回放也校验当前哈希和来源版本。调用错误不回显原始内容或底层异常文本。

检查点保持版本 3，兼容 1/2/3。恢复时为旧召回请求补齐可选字段，避免仅因新增 pointer 导致已完成的读取失去缓存身份。字段摘录、导航线索及输入读取范围继续使用现有状态与调用结果日志持久化，不增加数据库表。

## 工具菜单与上下文

没有用户补充、也没有 JSON 证据时，继续使用较小的原文读取工具。出现 JSON 证据后展示 agent_read_context，并隐藏功能重叠的 agent_read_collected_evidence Schema；旧工具执行入口保留兼容。

JSON 使用说明放在按需展示的工具描述中，不持续增加所有调查的系统提示词。预算不足时仍进入独立报告上下文，不扩大模型窗口或跳过收尾预留。

## 验证与交付

新增 tests/test_native_json.py，33 项离线测试，覆盖原始 Unicode/转义/大整数/指数写法、对象和数组导航、输出限额、非法 JSON/Pointer、完整输入搜索、长输入进展、预算先行、跨调查隔离、来源版本变化、旧缓存升级和中断恢复。

模型替身集成测试走通：大 JSON 首次 Spill → 根目录 → 目标字段 → 现有结构化断言核验 → 报告。Provider 仅调用一次；后续读取使用已收集证据。JSON 流程在配置的 16,384 Token 窗口下验证，原有非 JSON 的 8,192 Token 小窗口回归保持通过；不承诺任意复杂 Schema 都能放进更小窗口。

完整离线回归：**512 passed、28 skipped**，记录为 data/local/harness-phase2-full-tests-20261102.xml。跳过项不计为通过。全项目 Ruff、版本化契约检查、Wheel 构建和隔离导入通过，冻结契约和数据库迁移未改动。

合成样本的原始 JSON 为 7,038 字节。根目录响应为 1,124 字节，包含两个直接子项与 256 字节连续预览；/queue/depth 响应为 538 字节，原文字面量为 7。记录为 data/local/harness-phase2-comparison-20261102.json。示例使用短合成 evidence_id 和空 source；实际来源元数据会影响大小。这些是序列化字节数，不是供应商 Token、完整推理成本或调查质量分数。

交付记录：

- 修改前备份：data/local/harness-phase2-before-f5acb535.zip；
- 本批源码差异：data/local/harness-phase2-source-20261102.patch；
- 检查结果：data/local/harness-phase2-validation-20261102.json；
- Wheel：data/local/harness-phase2-dist-20261102/game_test_agent-0.1.0-py3-none-any.whl。

本轮未调用付费模型或真实游戏平台，未安装包到当前运行环境，也未重启服务。scripts/start_workbench.ps1 从 src 加载源码，重启后使用本批实现；安装包部署需采用新 Wheel，并保持 API 与 Worker 版本一致。

## 后续范围

尚未实现完整 EvidenceMap、逐问题答案账本、有效约束自动归并、WorkerStep 提取、Router、事件合并批处理及跨任务 agent_find_context。当前是 P1 的部分实现；下一步应继续补逐问题证据缺口和完成标准，并用相同保存案例比较回答覆盖、引用有效性与实际调用成本，再扩展多任务协调。

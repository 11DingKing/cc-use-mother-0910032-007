# 年度核算审计封账

本项目维护年度核算审计封账的领域约定、角色边界与样例数据，并提供封账服务端实现：将申报输入、核算规则、人工调整和签署人汇总为封账快照，达到法定签署数后锁定内容并生成分块摘要，杜绝"报告签发后仍可修改车型明细、导出总额与审计留档不一致"的问题。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/sealing/`：封账领域服务与 HTTP API（纯标准库，无第三方依赖）。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约完整性 + 封账服务 + HTTP API 回归测试。

## 封账状态机

与领域契约一致：`草稿 → 待核算 → 已确认 → 执行中 → 已封存`

- **草稿**：企业申报员录入车型明细；核算专员绑定核算规则集、登记人工调整。
- **待核算**：申报员提交；核算专员执行核算，锁定待签署摘要。
- **已确认 / 执行中**：内容冻结，开始收集签署（核算专员、交易运营员、监管审计员，不可重复）。
- **已封存**：达到法定签署数（默认 3 人）自动封存——冻结载荷、按 4KiB 分块、生成 Merkle 分块摘要。

封存后任何录入/调整接口一律拒绝（409）。迟到材料只能：

- **进入下一版**（`next_version`）：自动开立或追加到本期间未封存的新版本；
- **更正单**（`correction`）：针对已封存版本，须独立监管审计员批准（申请人不得自批）后生效，封存原文不动。

**重开**：须监管审计员独立批准，批准后派生下一版草稿（继承封存内容），原封存版本永久保留。

**导出**：仅对已封存版本开放。导出内容只来自封存时冻结的载荷，因此重复导出字节一致；分块拉取支持中断后续传，重组结果可用清单中的整体哈希与逐块哈希校验。

## HTTP API

启动：`PYTHONPATH=src python3 -m sealing --port 8091 [--store data/ledger.json]`

身份通过请求头声明（角色为中文，需 URL 编码）：`X-Actor-Id`、`X-Actor-Role`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/periods/{period}/snapshots` | 创建期间草稿（每期间仅一个未封存版本） |
| POST | `/snapshots/{id}/inputs` | 录入车型明细（申报员，草稿态） |
| POST | `/snapshots/{id}/rule-set` | 绑定核算规则集（核算专员） |
| POST | `/snapshots/{id}/adjustments` | 人工调整（核算专员，确认前） |
| POST | `/snapshots/{id}/submit` / `/compute` | 提交核算 / 执行核算 |
| POST | `/snapshots/{id}/signatures` | 签署；达到法定人数自动封存 |
| POST | `/snapshots/{id}/late-materials` | 迟到材料：`next_version` 或 `correction` |
| POST | `/snapshots/{id}/corrections`、`/corrections/{cid}/decision` | 更正单与独立批准 |
| POST | `/snapshots/{id}/reopen-requests`、`/reopen-requests/{rid}/decision` | 重开申请与独立批准 |
| POST | `/snapshots/{id}/exports` | 创建导出会话（仅已封存） |
| GET | `/exports/{eid}`、`/exports/{eid}/chunks/{n}` | 导出清单 / 分块拉取（可续传） |
| GET | `/snapshots/{id}/chunks/{n}/proof` | 分块 Merkle 证明 |
| POST | `/snapshots/{id}/chunks/{n}/verify` | 验证任一分块 |
| GET | `/snapshots/{id}/seal-integrity` | 封存完整性自检 |
| GET | `/snapshots/{id}/diff/{other}` | 封账前后版本差异对比 |
| GET | `/snapshots/{id}/effective` | 封存净额 + 已批准更正 = 有效净额 |

错误映射：`validation→400`、`forbidden→403`、`not_found→404`、`conflict→409`。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

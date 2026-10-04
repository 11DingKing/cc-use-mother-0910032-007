# 年度核算审计封账

本项目维护年度核算审计封账的领域约定、角色边界与样例数据，并提供完整的 Python 服务端：将申报输入、核算规则、人工调整和签署人汇总为封账快照，达到法定签署数后锁定内容并生成分块摘要；迟到材料进入下一版或更正单，重开需独立批准；重复导出与中断续传不改变签发内容；API 可验证任一分块并对比封账前后的差异。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/sealing_service/`：封账服务端（领域用例 + HTTP API，仅依赖标准库）。
  - `canonical.py`：规范化 JSON 字节流、SHA-256、分块与 Merkle 摘要。
  - `store.py`：线程安全存储，可选 JSON 文件原子持久化。
  - `service.py`：状态机与全部领域用例。
  - `api.py`：HTTP 路由、ETag 幂等导出、Range 断点续传。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：启动封账 HTTP 服务。
- `tests/`：契约、领域服务与 HTTP API 回归测试。

## 状态机与关键约束

状态流转（与 `domain/contract.json` 对齐）：`草稿 → 待核算 → 已确认 → 执行中 → 已封存`。

- **封账输入快照**：`已确认` 时冻结候选内容并生成待签署摘要；封存时把申报输入、核算规则、人工调整、签署人一并固化为不可变快照。
- **法定签署人数**：达到 `quorum` 个指定签署人的不同签名才允许封存；未知签署人与重复签署被拒绝。
- **分块摘要校验**：封存时对规范字节流逐块计算 SHA-256，并生成 Merkle 根作为内容标识（导出 ETag）。
- **受控重开更正**：封存后申报通道关闭；迟到材料只能进入下一版（排队）或更正单；更正单批准与重开批准都必须由独立于申请人的监管审计员完成；旧快照永久可导出、可校验。

## HTTP API

| 方法与路径 | 说明 |
| --- | --- |
| `POST /periods` | 创建核算期间（签署人、quorum、核算规则） |
| `POST /periods/{id}/inputs` | 追加申报输入（车型明细，草稿状态） |
| `POST /periods/{id}/submit` | 提交核算（草稿 → 待核算） |
| `POST /periods/{id}/adjustments` | 登记人工调整（待核算状态） |
| `POST /periods/{id}/confirm` | 确认并冻结候选内容（→ 已确认） |
| `POST /periods/{id}/sign` | 签署；达到法定人数自动封存并生成分块摘要 |
| `GET /periods/{id}` | 期间状态与实时核算结果 |
| `GET /periods/{id}/snapshots` | 期间全部快照清单 |
| `GET /periods/{id}/audit` | 审计留档（追加式事件流） |
| `GET /periods/{id}/diff-working?snapshot_id=` | 封存快照 vs 当前工作稿差异 |
| `GET /snapshots/{id}` / `/manifest` | 快照清单（分块摘要、Merkle 根、签署人） |
| `GET /snapshots/{id}/export` | 导出规范字节流；ETag 幂等，`If-None-Match` 命中 304；支持 `Range` 断点续传（206/416） |
| `GET /snapshots/{id}/chunks/{i}` | 逐块下载（续传最小单元），响应头携带块摘要 |
| `GET /snapshots/{id}/chunks/{i}/proof` | 该分块的 Merkle 证明 |
| `POST /snapshots/{id}/verify-chunk` | 校验任一分块是否归于封存根 |
| `GET /snapshots/{id}/diff/{other}` | 两个封存快照差异（封账前后对比） |
| `POST /periods/{id}/late-material` | 迟到材料：`next_version` 排队或 `correction` 生成更正单 |
| `POST /corrections/{id}/approve` | 监管审计员批准更正单，开启下一版草稿 |
| `POST /snapshots/{id}/reopen-requests` | 申请重开已封存快照 |
| `POST /reopen-requests/{id}/approve` | 独立批准重开；旧快照保持不可变 |

错误响应统一为 `{"error": {"code", "message"}}`，状态码语义：404 不存在、409 状态冲突、422 参数校验、403 越权、416 区间不可满足。

## 运行

启动服务：`python3 tools/run_server.py --host 127.0.0.1 --port 8000 --data-file data/sealing.json --chunk-size 4096`

`--data-file` 可选；提供时每次变更原子落盘，重启后状态恢复。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

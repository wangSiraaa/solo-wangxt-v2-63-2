# 逾期升级 SLA 暂停服务

暴雨、封路等合法原因会使整改现场暂时无法作业。本服务为**逾期升级**提供可审查的
SLA 暂停记录：每条申请绑定事件、原因与证据，经历完整状态机；基于注入时钟的升级
计算自动扣除已批准暂停区间的**并集**；历史不可变，迟到批准只生成待复核补偿建议。

## 核心规则

1. **暂停申请状态机**（全程留痕）

   ```
   申请 PENDING ──批准──▶ 批准生效 APPROVED ──结束──▶ ENDED（终态）
       │                     │
       ├──拒绝──▶ REJECTED   └──撤销──▶ CANCELLED（已走表区间计到撤销时刻）
       └──撤销──▶ CANCELLED（从未生效）
   ```

   - 每条申请必须携带 `event_type`（暴雨/封路/…）、`reason`、至少一条 `evidence`。
   - 审批可指定实际生效区间 `approved_start/end`（可能与申请窗口不同）。
   - 结束操作幂等：重复 `end` 返回同一条记录、`ended_at` 不变、时长不双算。
   - 结案后禁止新建暂停；既有暂停的结束/撤销只作账面收尾。

2. **有效时长与升级**

   ```
   有效时长 = 原始时长 − 已批准暂停区间在统计窗口内的并集
   ```

   - 多个重叠/相邻暂停先 `merge_union` 再扣减，**重叠绝不重复延长**。
   - 升级策略（可替换）：L1 逾期即警告；L2 有效逾期 24h（¥100）；L3 72h（¥300）。
   - 扫描任务 `POST /jobs/escalation-sweep` 基于注入时钟，逐案件事务化、可重跑；
     `(case_id, level)` 唯一约束 + 保存点写入兜底并发与崩溃，不产生重复升级/处罚。
   - **结案冻结时钟**：统计时点固定在 `closed_at`，扫描跳过结案案件，不再升级。

3. **历史不可变与迟到批准补偿**

   - 已产生的 `Escalation`、`Penalty` **永不删除、永不就地改写**。
   - 若批准的生效起点早于当前时钟（迟到批准），系统对每个“若当时已含本次暂停
     便不会触发”的升级生成 `PENDING_REVIEW` 补偿建议（反事实重算）：
     - 已挂处罚（含已锁定）→ `PENALTY_CREDIT` 建议，确认后以
       `PenaltyCorrection` 追加冲抵，原金额/状态/处罚链完整保留；
     - 仅警告级升级 → `ESCALATION_REVIEW` 标注，升级记录保留。
   - 建议可确认或驳回，已决定的建议不能重复处理。

4. **旧事件迁移**（`migrations/m0001_init_and_legacy.py`）

   - 完整旧区间 → 导入为审计链完整的 `ENDED` 申请；
   - 旧系统悬空暂停（无结束时间）→ **不允许**导入成生效暂停，一律在迁移
     截止点落账为 `ENDED` 并记录 `migration_note`；
   - 非法数据（end≤start、案件不存在）隔离到 `migration_issues`；
   - 可重跑（`legacy_ref` 唯一 + 申请/问题双查重），末尾执行一次幂等升级扫描。

## 追溯输出

`GET /cases/{id}/sla-trace` 返回原始时长、每条计入的暂停区间、并集合并结果、
扣减秒数、有效/剩余/逾期时长、当前级别与全部升级记录，可直接用于审计。

## 技术栈

Python 3.11 · FastAPI（自动生成 OpenAPI，见 `/docs` 与仓库根 `openapi.json`）
· SQLAlchemy 2 · SQLite（可换任意 SQLAlchemy 方言）· pytest

## 运行

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --reload            # http://127.0.0.1:8000/docs
python -m pytest                                    # 全部验收测试
python scripts/export_openapi.py                    # 重新导出 openapi.json
python -m migrations.run --db sqlite:///./sla.db \
    --cutoff 2026-09-25T00:00:00Z                   # 旧事件迁移
```

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/cases` | 建整改案件（SLA 时限） |
| POST | `/cases/{id}/close` | 结案（冻结时钟） |
| POST | `/cases/{id}/pause-requests` | 申请暂停（事件/原因/证据/窗口） |
| POST | `/pause-requests/{id}/approve` | 批准生效（可迟到批准 → 补偿建议） |
| POST | `/pause-requests/{id}/reject` | 拒绝 |
| POST | `/pause-requests/{id}/end` | 结束（幂等，不双算） |
| POST | `/pause-requests/{id}/cancel` | 撤销 |
| GET | `/pause-requests`、`/cases/{id}/pause-requests` | 查询（可按状态过滤） |
| GET | `/cases/{id}/sla-trace` | 追溯输出 |
| GET | `/cases/{id}/escalations`、`/penalties` | 升级/处罚链 |
| POST | `/penalties/{id}/lock` | 锁定处罚 |
| GET/POST | `/compensation-suggestions[/{id}/confirm|dismiss]` | 补偿建议复核 |
| POST | `/jobs/escalation-sweep` | 升级扫描（可重启重跑） |

## 代码结构

```
app/
  clock.py        注入时钟（SystemClock / ManualClock）
  intervals.py    时间区间服务（并集合并、裁剪）
  models.py       案件/暂停/升级/处罚/更正/建议/迁移问题
  services.py     SLA 计算、扫描、暂停状态机、补偿、迁移辅助
  api.py          路由
  main.py         应用工厂（时钟/策略可注入，异常回滚）
migrations/
  m0001_init_and_legacy.py  schema + 旧事件回填
  run.py                     CLI
tests/            验收测试（见各文件 docstring 与验收点的对应关系）
```

## 验收测试对照

| 验收点 | 测试 |
| --- | --- |
| 批准后剩余时长正确续算 | `test_approved_pause_then_remaining_resumes_correctly`、`test_sweep_during_open_pause_stalls_escalation_until_pause_ends` |
| 重叠暂停不双算 | `test_overlapping_pauses_not_double_counted` + `test_intervals.py` |
| 重复结束请求不双算 | `test_duplicate_end_request_is_idempotent` |
| 暂停中整改后不再升级 | `test_close_during_pause_prevents_further_escalation` |
| 迟到批准不静默撤销锁定处罚 | `test_late_approval_on_locked_penalty_creates_suggestion_not_reversal`、`test_dismissed_suggestion_changes_nothing` |
| 审批失败不留半截停表 | `test_approval_failure_leaves_no_half_pause`、`test_approve_invalid_window_leaves_request_pending` |
| 重启重跑不双算 | `test_sweep_is_idempotent_across_restarts` |
| 旧事件迁移不留半截停表 | `test_legacy_migration_backfills_without_dangling_pauses`、`test_migration_is_rerunnable` |
| OpenAPI | `test_openapi.py` |

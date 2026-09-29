# 特殊教育支持计划合规

纯Python标准库实现的特殊教育支持计划合规原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：计划状态转换、同意、服务履约、复查期限和计划版本和冲突检查。
- `src/rule_versioning.py`：区级规则的版本化（草稿/定时生效/生效/归档）、规则快照、差异与按快照核对。
- `src/repository.py`：SQLite建表、事务、规则乐观并发、到点生效与旧库快照迁移。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：计划时间线、规则时间线与统一时间线。
- `static/index.html`：最小演示页面（含规则版本操作区）。
- `tests/`：完整流程、规则计算、失败场景、规则版本化与旧库迁移测试。

## 规则版本化

- 区级规则含服务上限（`service_cap`，分钟）和复查周期（`review_cycle_days`，天）。
- 规则先存草稿（`draft`），发布时可给 `effective_at` 实现到点生效（`scheduled` -> `effective`）；
  缺省为立即生效。被新版本取代后为 `superseded`。
- 草稿用 `revision` 做乐观并发：修订或发布时若发现草稿已被他人更新，晚到的提交返回409并提示先刷新。
- 计划建档时把当时规则固化为 `payload.rule_snapshot`，之后一律按该快照核对；
  区里随后调整上限或周期不影响在库旧计划。
- 计划复查（`review`）时可带 `rule_change_reason` 改用新版本（`use_latest_rule` 或 `use_rule_version`），
  审计事件记录计划版本、规则版本迁移与逐字段差异。
- 回滚不删除历史：复制某个旧版本的值生成一个新的生效版本。
- 旧库中没有快照的计划在启动建表时幂等补一份原规则快照（基线版本，`backfilled=true`）。

## 启动

```bash
python3 app.py --db ./data.db --port 8328
```

默认端口为`8328`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情（含按规则快照计算的 `evaluation`）。
- `GET /api/records/{id}/audit`：计划审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
  复查动作可在 `data` 中带 `use_latest_rule`/`use_rule_version` 与必填的 `rule_change_reason`。
- `GET /api/rules/current`：当前生效规则；`GET /api/rules`：全部版本；`GET /api/rules/{version_no}`：指定版本。
- `POST /api/rules/drafts`：建草稿，`data` 为 `{"service_cap":600,"review_cycle_days":30}`。
- `POST /api/rules/drafts/{id}/revise`：提交修订，请求体为`{"expected_revision":1,"data":{...}}`。
- `POST /api/rules/drafts/{id}/publish`：发布，可带 `data.effective_at`（ISO时间，缺省立即）与必填 `data.reason`。
- `POST /api/rules/rollback`：回滚，`data` 为 `{"version_no":1,"reason":"..."}`，生成新的生效版本。
- `GET /api/rules/timeline`：规则时间线；`GET /api/timeline`：规则+计划统一时间线。

规则的草稿、修订、发布、回滚需 `administrator` 角色。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。

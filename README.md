# 特殊教育支持计划合规

纯Python标准库实现的特殊教育支持计划合规原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 规则版本与计划快照

区定规则只有两项参数：服务上限（`service_cap`）和复查周期（`review_cycle_days`）。

- 规则先存**草稿**（可多次修订，修订号`revision`递增），发布时可指定到点时间，留空立即生效；到期版本在读取/列表时自动切换为生效，旧生效版本转为`superseded`。
- 计划在**建档时**固化当时生效规则的快照（`payload.rule_snapshot`），区里之后调整规则不会改动任何在用计划；服务履约上限、复查周期始终按建档快照核对。
- 复查修订（`amend`）时可传`rule_change_reason`，带原因改用当前生效的规则新版本，生成新的计划版本，复查期限按新周期重新起算。
- 规则草稿的修订、发布均采用乐观并发（`expected_version` + `expected_revision`）：若已有修订先提交，晚到的请求返回`409 conflict`并提示“请先刷新”。
- 回滚不复活旧版本，而是按历史版本参数生成一个**新的生效版本**，时间线上留有`rule_rolled_back`及差异。
- 旧库中没有快照的计划在服务启动时自动补建基线规则快照（不改计划版本号），并在该计划时间线写入`rule_snapshot_backfilled`，重复启动幂等。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/clock.py`：UTC时钟（可固定时间，用于到点生效测试）。
- `src/rule_policy.py`：规则版本纯领域逻辑（校验、差异、快照、改用新版本）。
- `src/rule_store.py`：规则版本与规则审计的SQLite持久化、乐观并发、到点生效、回滚。
- `src/rule_service.py`：规则用例编排与管理员权限。
- `src/rules.py`：计划状态转换、同意、服务履约、复查期限和建档规则核对。
- `src/repository.py`：计划表SQLite事务、查询和旧库快照回填。
- `src/plan_service.py`：计划用例编排、建档绑定快照、乐观并发。
- `src/audit.py`：计划事件时间线读取。
- `src/timeline_service.py`：规则事件与计划事件的只读合并时间线。
- `src/service.py`：用例门面，聚合规则与计划服务并触发旧库回填。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `static/index.html`：规则版本管理与计划查看的演示页面。
- `tests/`：完整流程、规则计算、规则版本、计划快照、旧库回填、时间线、HTTP和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8328
```

默认端口为`8328`，默认数据库位于项目目录。服务启动时自动建表、初始化v1基线规则并回填旧计划快照。

## 主要接口

计划：

- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情（含`rule_snapshot`）。
- `GET /api/records/{id}/audit`：计划自身审计时间线。
- `GET /api/records/{id}/timeline`：该计划在合并时间线中的事件。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，按当时生效规则核对。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
  - `amend`的`data`中可带`rule_change_reason`，复查时改用当前生效规则。

规则（管理员可写，已知角色可读）：

- `GET /api/rules`：全部规则版本（同时驱动到点生效）。
- `GET /api/rules/current`：当前生效版本。
- `GET /api/rules/{version}`：指定版本。
- `GET /api/rules/timeline`：规则审计时间线。
- `POST /api/rules/drafts`：新建草稿，`{"data":{"service_cap":900,"review_cycle_days":25}}`。
- `POST /api/rules/drafts/revise`：修订草稿，`{"expected_version":2,"expected_revision":1,"data":{...}}`。
- `POST /api/rules/drafts/publish`：发布草稿，`{"expected_version":2,"expected_revision":2,"data":{"effective_at":"..."}}`（`effective_at`留空立即生效）。
- `POST /api/rules/drafts/discard`：放弃草稿。
- `POST /api/rules/rollback`：`{"target_version":1}`，按旧参数生成新生效版本。

统一视图：

- `GET /api/timeline`：规则与计划事件合并后的时间线（含版本、差异）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、草稿/修订/发布并发、到点生效、回滚、计划快照绑定、调规不影响在用计划、复查改用新版本、旧库回填、统一时间线、HTTP接口和权限/版本冲突。

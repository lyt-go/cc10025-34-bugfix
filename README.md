# 数据资产与生命周期治理平台

面向数据资产编目、冷热分层、生命周期管理、血缘追踪与合规治理的统一数据治理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              19个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 资产编目 | `asset_catalog` | 数据资产 | 资产编号、资产名称、所属域 |
| 分层策略 | `tier_policy` | 分层策略 | 策略编号、目标资产、当前层级 |
| 分层种子发布凭据 | `publish_credential` | 发布凭据 | 凭据编号、目标资产、发布版本 |
| 血缘图谱 | `lineage_graph` | 血缘记录 | 记录编号、上游资产、下游资产 |
| 保留规则 | `retention_rule` | 保留规则 | 规则编号、目标资产、保留天数 |
| 合规检查 | `compliance_check` | 合规规则 | 规则编号、检查维度、合规标准 |
| 冷存储管理 | `cold_storage` | 冷存任务 | 任务编号、源资产、目标存储 |
| 预热回迁 | `hot_prefetch` | 回迁任务 | 任务编号、冷存位置、目标分区 |
| 版本快照 | `version_snapshot` | 数据快照 | 快照编号、关联资产、快照时间 |
| 标签体系 | `label_tag` | 数据标签 | 标签编号、标签名称、标签类型 |
| 审计日志 | `audit_log` | 审计记录 | 记录编号、操作主体、操作类型 |
| 访问网关 | `access_gate` | 访问策略 | 策略编号、目标资产、授权主体 |
| 成本追踪 | `cost_tracker` | 成本记录 | 记录编号、关联资产、存储成本 |
| Schema注册 | `schema_registry` | Schema版本 | 版本编号、关联资产、字段清单 |
| 分区规划 | `partition_plan` | 分区方案 | 方案编号、关联资产、分区键 |
| 补数任务 | `backfill_job` | 补数作业 | 作业编号、目标资产、补数区间 |
| 质量报告 | `quality_report` | 质量报告 | 报告编号、关联资产、检核时间 |
| 回收清理 | `reclaim_job` | 回收任务 | 任务编号、目标资产、回收范围 |
| 变更通知 | `change_notice` | 变更通知 | 通知编号、关联资产、变更类型 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数仓与数据湖

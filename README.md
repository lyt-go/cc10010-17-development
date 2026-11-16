# 数据仓库ETL任务编排平台

面向数据仓库ETL任务开发、作业编排调度、任务依赖解析、源端数据校验、异常任务重跑、血缘分板与运行全景监控的ETL工程管理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
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
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| ETL作业 | `etljob` | 作业定义 | 作业编号、作业名称、源端数据表 |
| 任务编排 | `dag` | 编排图 | 图编号、图名称、拓扑结构 |
| 依赖解析 | `dependency` | 依赖关系 | 关系编号、上游作业、下游作业 |
| 源端校验 | `sourcecheck` | 校验配置 | 配置编号、源端数据表、行数下限 |
| 运行实例 | `runtime` | 运行记录 | 记录编号、作业名称、开始时间 |
| 作业执行 | `execution` | 基线水位视图 | 由基线时间线派生（只读） |
| 异常重跑 | `rerun` | 重跑任务 | 任务编号、原始运行编号、重跑区间开始 |
| 基线管理 | `baseline` | 基线定义 | 基线编号、基线名称、承诺完成时间 |
| 分区检测 | `partitioncheck` | 检测规则 | 规则编号、数据表名、期望分区数 |
| 数据比对 | `comparator` | 比对任务 | 任务编号、源表名、目标表名 |
| 元数据存储 | `metastore` | 元数据表 | 表编号、表名、所属数仓层 |
| 资源配置 | `resource` | 资源池 | 池编号、池名称、最大并发数 |
| SLA监控 | `slavalidate` | SLA规则 | 规则编号、适用作业、完成截止时间 |
| SQL模板 | `sqltpl` | 模板定义 | 模板编号、模板名称、模板类型 |
| 数据备份 | `backuprestore` | 备份策略 | 策略编号、数据表名、备份周期 |
| 日志归档 | `logarchive` | 归档策略 | 策略编号、日志类型、保留天数 |
| 通知渠道 | `notifychan` | 渠道配置 | 渠道编号、渠道类型、接收地址 |
| 发布计划 | `publishplan` | 发布单 | 发布编号、发布内容、变更说明 |

## 基线健康时间线

基线管理不再只有静态的承诺完成时间与预警余量，而是把基线定义、基线版本和作业的
分区运行结果关联成一条可追溯的时间线，并生成基线健康结论。

核心规则：

- **多基线联动**：同一作业可属于多条基线，一条运行结果到达后，所有引用它的基线时间线同时更新。
- **版本归属**：基线字段修改会生成新版本并承接上一版；运行结果可显式指定版本，未指定时按
  到达时间取最新生效版本。版本只覆盖自己声明过的字段，其余字段沿承接链回落到基线定义。
- **幂等**：每个关键节点有稳定的节点键（基线 + 版本 + 节点类型 + 运行编号 + 时间 + 签名），
  重复计算不会重复写入；相同运行结果重复提交只生效一次（按运行指纹判重）。
- **同一事务**：节点、基线当前状态与运行实例结论投影在一次 Store 事务里落库，失败整体回滚。
- **历史不可变**：节点只追加不修改，“只记录已经发生的事实”；未来的预警/破线只作为基线状态里的
  风险预测，不会提前写成历史节点。结论节点仅在结论签名变化时追加。

主要接口：

| 接口 | 说明 |
| --- | --- |
| `GET /api/baseline/timeline/overview` | 时间线概览，按承诺完成时间排序 |
| `GET /api/baseline/{id}/timeline` | 单条基线的版本、归属明细与按时间排列的节点 |
| `GET /api/baseline/{id}/versions` | 版本承接链 |
| `POST /api/baseline/{id}/versions` | 修改基线字段并生成新版本 |
| `GET /api/baseline/timeline/risk-board` | 破线风险看板（按风险分栏 + 最近风险节点） |
| `GET /api/baseline/timeline/execution?job=` | 作业执行组件读取口径：水位、缺失分区、预计完成时间 |
| `GET /api/baseline/timeline/version-conflicts` | 同一作业被多条基线/多版本引用时的预警时间差异 |
| `POST /api/baseline/timeline/run-results` | 提交作业分区运行结果（多基线联动、幂等） |
| `POST /api/baseline/timeline/recompute` | 手动重算（不产生重复节点） |
| `GET /api/runtime/{id}/baseline-timeline` | 运行实例回查基线时间线中的水位、缺失分区与预计完成时间 |

示例数据以服务启动当天为锚点生成，启动后即可看到“正常 / 预警中 / 已破线 / 待观测”四种状态；
`BASE-0001` 带 v1→v2 版本承接，`dws_user_tag` 同时被 `BASE-0002` 与 `BASE-0003` 引用。

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
- 基线健康时间线的计算集中在 `app/services/baseline_timeline.py`，节点、状态、回执三类
  内部表以下划线开头，不进运营概览；跨表写入统一走 `store.transaction()`。

领域：数据工程
子方向：数仓与数据湖

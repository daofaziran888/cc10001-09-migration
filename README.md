# 采集工坊 · 爬虫与RPA开发平台

面向网页爬虫规则编排、自动化采集流程、代理调度、反爬对抗、结果校验与持久化发布的爬虫与RPA数据采集开发平台。

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
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 站点画像 | `site_profile` | 目标站点 | 站点编号、域名、编码格式 |
| 代理调度 | `proxy_pool` | 代理节点 | 节点编号、代理类型、IP地址 |
| 凭证管理 | `cookie_vault` | 会话凭证 | 凭证编号、关联站点、Cookie文本 |
| 频控策略 | `rate_limiter` | 请求策略 | 策略编号、目标站点、每秒上限 |
| 抽取模板 | `extract_rule` | 抽取规则 | 规则编号、目标站点、页面类型 |
| 采集任务 | `crawl_task` | 任务批次 | 任务编号、目标站点、入口URL |
| RPA脚本库 | `rpa_script` | 自动化脚本 | 脚本编号、目标应用、触发事件 |
| 采集流水线 | `data_pipeline` | 流水线段 | 段编号、输入来源、转换算子 |
| 采集仓储 | `result_store` | 结果批次 | 批次编号、关联任务、原始行数 |
| 反爬对抗 | `anti_block` | 对抗策略 | 策略编号、目标站点、触发特征 |
| 质量审计 | `quality_check` | 审计记录 | 记录编号、关联批次、检查项 |
| 增量同步 | `delta_sync` | 同步水位 | 水位编号、源数据集、增量字段 |
| 采集监控 | `log_monitor` | 监控事件 | 事件编号、事件类型、严重级别 |
| 模板市场 | `template_mgr` | 采集模板 | 模板编号、适用站点、模板类型 |
| 数据分发 | `export_hub` | 分发通道 | 通道编号、输出格式、目标地址 |
| 定时调度 | `scheduler` | 调度计划 | 计划编号、关联任务、Cron表达式 |
| 解析引擎 | `parse_engine` | 解析配置 | 配置编号、页面类型、引擎类型 |
| 重试队列 | `retry_queue` | 重试条目 | 条目编号、失败页面、失败原因 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 质量审计记录版本化迁移

历史质量审计记录以 JSON 数组保存（`backend/data/audit_records.json` 及
`backend/data/legacy_audit/*.json`），字段没有稳定版本；新版记录以信封结构写入
（`backend/data/new_envelopes/*.json`）。迁移由 `app/services/audit_migration.py` 负责：

- **来源与映射**：同时读取旧数组与新版信封，按记录编号（`record_no`）映射
  版本号、关联批次、检查项、判定结果，生成带版本号的信封数据集
  （联合表 `audit_envelopes`，落盘 `data/audit_envelopes.json`），每条信封保存
  完整记录信封 `payload`、来源类型/文件/来源版本，以及旧数组原样快照 `legacy_snapshot`。
- **批量与原子性**：一次迁移批量处理全部来源文件、逐条核对，先在内存工作副本上
  完成全部变更，最后整组一次提交（唯一落盘点）；任一来源文件损坏则整组回滚。
  每批迁移在 `data/audit_migrations.json` 留凭证（含每文件扫描/迁移/跳过/冲突计数）。
- **幂等**：同来源文件 + 同来源记录 id、或同记录编号同内容哈希，重复导入不产生
  重复审计版本，只累计跳过计数。
- **单一当前版本**：同一记录编号同时只有一个 `current` 版本，旧结论转为
  `archived`，旧字段与审计结论始终保留可追溯。
- **冲突不静默覆盖**：同记录编号新旧实际值不一致时支持两种策略——
  `manual`（默认）：两条都保留，新版进 `pending_confirm` 冲突清单，读取服务呈现
  待确认状态，经人工 `use_incoming` / `keep_current` 确认后再调整当前版本；
  `archive`：以新版信封为当前结论，旧数组归档。两种方式都不会静默覆盖历史。
- **统一读取**：`GET /api/quality_check/versions` 同时返回新版数据集与未迁移旧记录；
  `GET /api/quality_check/records/{记录编号}/detail` 返回当前版本与完整版本链。
- **落库闸门**：`GET /api/quality_check/export` 仅在数据集已落库时允许导出，
  空数据集返回 409。
- **跨模块入口同步**：`/api/pipeline/audit/*` 与质量审计模块共用同一服务，
  审计引擎新产出的结论通过 `append_live_audit` 直接进入版本化数据集。
- 服务启动时自动执行一次幂等迁移（`bootstrap_auto_migrate`），读取服务开箱即可
  看到落库数据；也可通过 `POST /api/quality_check/migration/run` 手动触发。

领域：数据工程
子方向：数据采集、爬虫与RPA

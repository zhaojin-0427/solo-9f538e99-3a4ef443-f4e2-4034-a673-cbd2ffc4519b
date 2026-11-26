# 壁画裂缝定位图 · 对位更新操作台

壁画复测发现局部照片错位。本项目用 **Flask + SQLite** 做一个操作页：修复师在两张
同比例、同朝向的样片上点选同一处纹样并提交新对位，系统在严格约束下求最优撤法，
更新裂缝定位总图。

## 业务规则（与实现一一对应）

- 内置 5 张同比例（480×360）、同朝向的 SVG 壁画样片，以及按旧对位排好的总图；
  台账中标出每条对位是否**已复核**、每条裂缝是否**已贴签**。
- 样片只有平移变量：样片点 `p` 的总图坐标为 `p + (tx, ty)`。
- 对位 `(a,b)` 上同一纹样的两点 `pa,pb` 给出硬约束 `tb - ta = pa - pb`。
- 提交的新对位**必须采用**（视为已复核、不可撤）。
- **基准图 `img0` 固定**，其余照片只平移。
- 求解必须同时满足：
  1. 每张图都能经保留对位**定位到基准图**（图连通性）；
  2. **保留的对位全部成立**（默认容差 2px，见 `app/seed_data.py`）；
  3. **已贴签裂缝的总图坐标照旧**——等价于把所在照片钉在当前平移上。
- 只允许撤下**未复核**的旧对位，且**最多 2 条**。
- 优选顺序：**撤下条数最少 → 移位裂缝数最少**；再相同按对位编号取确定解。
- 有解：事务内更新照片平移（总图）、撤下旧对位、把新对位记为**已复核**，
  并列出撤下对位与移位裂缝的新坐标。
- 无解：**原图与对位完全不变**（仅多一条 `no_solution` 审计记录）。

## 现场复核批次（照片对位完成后，逐条复核裂缝位置）

对位更新之后，修复队还要让现场人员逐条核对裂缝位置。新增的复核闭环
（`app/review.py` + SQLite 三张新表 + 首页“现场裂缝复核批次”区）规则如下：

- **开始批次**：在首页勾选当前总图上的若干裂缝 → `POST /review/batches`。
  批次（号 `PC-0001…`）记录开始时的**对位版本** `alignment_version`（种子为 v1，
  每成功应用一次对位更新 +1），并为每条裂缝固化一份**总图坐标快照**
  `review_items.snap_x/snap_y/snap_version`。
- **提交复核**：必须带**批次号 + 裂缝号 + 现场 x/y 坐标**
  （`POST /review/submit` 表单或 `POST /api/review/submit` JSON；
  也兼容 `batch_id/crack_id/fx/fy/coord_x/coord_y` 等别名字段）。
  现场坐标到该条目快照的**欧氏距离 ≤ 2px**（含边界）才确认。
- **同裂缝只留一次**：`review_items` 对 `(batch_id, crack_id)` 有唯一约束；
  首次合法提交写入唯一确认（现场坐标/偏差/时间），之后重复提交返回
  `already_confirmed`，**不新增记录、不覆盖首次历史坐标**。
- **对位更新 → 未提交项过期**：有解对位应用时，在**同一事务**内把所有
  `pending` 且 `snap_version` 落后的条目置为 `expired`，对位版本 +1。
  向过期项提交旧坐标会被拒绝（`reason_code=expired`）；**已确认项完全不动**，
  旧现场坐标、旧快照、确认时间等历史保留（页面同时给出它在新总图上的当前坐标）。
- **重开过期项**：每条过期记录有“↻ 重开”按钮
  （`POST /review/items/<id>/reopen`，JSON：`/api/review/reopen`）。
  重开时按**当前总图**重新取快照、`snap_version` 更新为当前版本、轮次 `round_no+1`，
  条目回到待复核，即可按新坐标再次提交。已确认项不允许重开（历史保护）。
- **拒绝原因**：超差（`too_far`）或过期（`expired`）的提交会向 append-only 的
  `review_submissions` 审计表追加一条中文原因（含提交坐标/偏差/时间/轮次），
  页面在对应行的“拒绝原因”列列出当前轮次最近一次原因。
- **无效提交不改状态**：缺批次/裂缝、编号不存在、坐标非数字等返回 400 `invalid`，
  不写任何业务数据；超差/过期返回 400 `rejected` 并写审计原因，但条目的
  `status / snap_* / field_* / confirmed_at` 均不变。表单被拒时服务端直接以
  400 重渲染页面并显示原因；成功/幂等重复走 PRG 302。
- 重置为内置示例会清空复核三表，并把对位版本复位为 v1。

## Docker Compose 启动

```bash
docker compose up --build
# 打开 http://localhost:5000
# 若宿主机 5000 被占用： WEB_PORT=8000 docker compose up --build
```

SQLite 数据库保存在命名卷 `mural-data` 的 `/data/mural.db`，容器重启数据不丢。
首次启动自动建表并播种内置样片数据；页面右上角可随时“重置为内置示例数据”。

## 本地直接运行

```bash
python -m pip install -r requirements.txt
python wsgi.py                 # http://127.0.0.1:5000
# 或： gunicorn --bind 0.0.0.0:5000 wsgi:application
```

本地默认数据库路径为 `data/mural.db`，可用环境变量 `MURAL_DB` 覆盖。

## 自测（冲突有解 / 无解两种情形）

```bash
python selftest.py
# 或： python -m unittest discover -s tests -v
# 也兼容： pytest -q
```

共 41 个用例（纯求解器 + Flask/SQLite 接口/事务 + 复核批次状态机），覆盖：0/1/2 条撤下、
“同撤条数比移位裂缝数”的优选、连通性、贴签钉死、基准固定、
需要撤 3 条（超过上限）的无解、撞已复核对位的无解，HTTP/表单/重置通道，
以及复核批次的建批快照、≤2px 正常确认、重复提交只留一次、超差拒绝且状态不变、
对位更新后未提交过期/旧坐标被拒/已确认留史、过期重开按新快照再复核、表单 400/PRG。

### 内置种子数据的两个标准情形

| 情形 | 点选（A 图 → B 图） | 结果 |
|---|---|---|
| **有解冲突**（页面按钮“填入有解冲突示例”） | `img1 (440,170)` → `img2 (40,170)`，同一朵莲纹 | 新对位 `DW-06` 已复核；必须撤 `DW-02、DW-03`（枚举确认不存在撤 0/1 条的解；撤 `DW-02+DW-05` 虽也可行但会多移 1 条裂缝，故不选）；`LF-03 (990,220)→(960,200)`、`LF-04 (1150,110)→(1120,90)` |
| **无解冲突**（页面按钮“填入无解冲突示例”） | `img0 (470,150)` → `img1 (20,150)` | 隐含位移 (450,0) 与已复核 `DW-01` 的 (400,0) 直接矛盾，已复核对位不可撤；枚举全部 7 种撤法（≤2 条）均不可行，原图与 5 条对位不变 |

## HTTP / JSON 接口

| 方法与路径 | 说明 |
|---|---|
| `GET /` | 操作页（总图、点选台、对位/裂缝台账、处理记录、结果横幅） |
| `POST /submit` | 无 JS 也可用的表单提交，PRG 跳转到 `/?run=<id>` |
| `POST /reset` | 表单重置回内置示例 |
| `GET /api/state` | 全部图像（含当前平移）、对位、裂缝（含当前总图坐标）、记录、示例载荷 |
| `POST /api/submit` | JSON 提交：`{image_a,image_b,ax,ay,bx,by}`，返回求解与落库结果 |
| `POST /api/reset` | JSON 重置 |
| `GET /api/runs/<id>` | 某次提交的完整结果 |
| `POST /review/batches` | 表单建批：多选 `crack_ids`（+可选 `note`），PRG 到 `/?batch=<id>#review` |
| `POST /review/submit` | 表单复核：`batch_no,crack_code,x,y`；成功 302，被拒 400 重渲染并显示原因 |
| `POST /review/items/<id>/reopen` | 表单“重开”过期项，PRG 回批次页 |
| `GET /api/review/batches` | 全部批次（含各状态计数）与当前对位版本/容差 |
| `POST /api/review/batches` | JSON 建批：`{"crack_ids":[...],"note"?}`，返回含快照条目的批次详情 |
| `GET /api/review/batches/<PC-0001 或 id>` | 批次详情：条目快照、状态、现场坐标、最近拒绝原因、当前总图坐标 |
| `POST /api/review/submit` | JSON 复核（别名 `/api/review`、`/api/reviews`）；200 `confirmed/already_confirmed`，400 `invalid/rejected`（`reason_code=too_far/expired`） |
| `POST /api/review/items/<id>/reopen` / `POST /api/review/reopen` | JSON 重开过期项（后者用 `batch_no+crack_code`） |
| `GET /healthz` | 健康检查 |

`POST /api/submit` 的 `status`：

- `applied`：含 `removed[]`（撤下对位快照）、`shifted[]`（裂缝 `old/new/delta` 坐标）、
  `new_alignment.code`；
- `no_solution`：含 `reason` 与尝试过的候选数，数据库无改动；
- `invalid`（HTTP 400）：选图相同、坐标越界等输入问题。

## 代码结构

```
app/
  seed_data.py     # 样片/旧对位/裂缝/示例点 的唯一数据来源 + 播种/重置
  solver.py        # 纯逻辑：约束传播、连通性、贴签钉、枚举 ≤2 撤法、双目标优选
  services.py      # 校验 + 求解 + SQLite 事务（应用或整体不动；应用时联动复核过期）
  review.py        # 复核批次：版本/快照、2px 容差提交、幂等唯一确认、过期、重开
  db.py            # 连接、schema（含 review_batches/items/submissions、app_meta）、初始化
  scene.py         # 由 seed_data 生成样片 SVG（python -m app.scene）
  __init__.py      # Flask 工厂与路由（页面 + 对位/复核两套表单与 JSON API）
  templates/       # base.html / index.html（含复核批次工作台）
  static/          # style.css、app.js（点选）、review.js（全选/填入/滚动）、samples/*.svg
tests/             # test_solver.py、test_api.py、test_review.py
selftest.py        # 一键跑全部用例并说明关键情形
Dockerfile / docker-compose.yml / requirements.txt
```

## 本次完工核对说明（含“现场复核批次”增量）

**已实现**：

- 原有对位求解/页面/API/SQLite/Docker 能力保持不变；
- 新增复核批次完整闭环：当前总图多选裂缝建批、记录对位版本与每条裂缝总图快照；
  批次号+裂缝号+现场坐标提交、欧氏距离 ≤2px（含边界）校验；
  `(批次,裂缝)` 唯一约束 + 首次确认落库 + 重复提交幂等不覆盖；
  对位“有解应用”同事务升版本并把未提交项置过期、拒绝旧坐标，已确认项保留历史；
  过期项一键重开并按新总图刷新快照/版本/轮次；
  页面分区列出待复核/已确认/已过期及拒绝原因，提供行内快捷提交与重开入口；
  无效提交不改复核状态；拒绝原因入 append-only 审计表；重置联动清空并复位版本；
- 新增 `tests/test_review.py`（20 例），总计 **41 个用例**；新增 `static/review.js`。

**已验证（在本沙箱内实际执行）**：

- `python selftest.py` 与 `pytest -q`：**41/41 全部通过**
  （原 21 个对位用例无回归 + 20 个复核用例）；
- 在隔离依赖目录安装 Flask/gunicorn 后，用与 Docker 相同参数的 gunicorn
  （`--workers 2 --preload wsgi:application`，独立临时 `MURAL_DB`）真实起服，
  以标准库 HTTP 脚本跑通并断言了题目要求的三种情形，共 15 项检查全部 PASS：
  1. **正常**：建批快照正确 → 偏差 1px 确认 → 重复提交 `already_confirmed`
     且首次现场坐标不被覆盖、批次内仅一条确认；恰好 2px 边界可确认；
  2. **坐标偏差**：偏差 4px 返回 400 `rejected/too_far`，条目仍 `pending`、
     `field_*`/`confirmed_at` 为空、快照不变，拒绝原因已记录并出现在页面；
  3. **对位更新后过期**：提交内置有解对位后版本 v1→v2，未提交项自动 `expired`，
     旧坐标提交返回 400 `rejected/expired`；已确认的 LF-03 保留 (990,220)
     历史现场坐标（当前总图已移到 (960,200)）；重开 LF-04 后快照刷新为
     (1120,90)@v2、轮次 2，按新快照偏差 1px 提交成功；
- 另用真实 HTTP 核对了无 JS 表单通道：多选建批 302、超差表单提交返回 400 且
  HTML 含具体偏差与“拒绝原因”、合法表单 302 PRG、重开路由与异常 400；
  `GET /?batch=<id>` 页面含待复核/已确认/已过期/拒绝原因/重开/提交字段；
  `/static/review.js`、`style.css`、首页均 200；
- `node --check static/app.js static/review.js` 通过；
  `python -m app.scene`（Docker 构建步骤）可正常生成 5 个 SVG；
  `docker-compose.yml` 通过 YAML 解析，服务/端口/命名卷/`MURAL_DB=/data/mural.db` 不变。

**未在此环境验证 / 未完成**：

- 该沙箱内没有 Docker CLI/守护进程（`docker: command not found`），因此
  **`docker compose up --build` 本身没有在本机实际执行**；Dockerfile 未改，
  仅基于 `python:3.12-slim` 安装 `requirements.txt`（未新增第三方依赖），
  并以已实际验证过的 gunicorn 参数启动；新表在容器首启时由 `init_db`
  `CREATE TABLE IF NOT EXISTS` 自动建立，旧命名卷升级也会幂等补表。
- 沙箱内没有无头浏览器，未做真实点击/截图级自动化；复核区所有关键操作都是
  标准 form POST（无 JS 可用），`review.js` 仅做全选、填入与滚动等渐进增强。
- 复核批次未额外做“关闭/归档批次”功能（题目只要求进行中过期与重开）；
  批次长期保留在左侧列表，重置示例数据时会一并清空。

# 提花织带纹版修复 API

根据同系列**参考 PNG** 与用透明像素标注缺损的**残片 PNG**，补出可反复织造的
重复单元纹版。技术栈：FastAPI + Pillow + OR-Tools CP-SAT（含内置回溯兜底）+ SQLite。

## 目录结构

```
.
├── app/
│   ├── images.py    # PNG 解析/校验、周期投影、2×2 图块统计
│   ├── solver.py    # CP-SAT 可行性 + 局部搜索优化 + 回溯兜底
│   ├── storage.py   # 草案 SQLite 持久化
│   └── main.py      # FastAPI 路由
├── tests/
│   └── test_app.py  # 26 个单元/端到端测试
├── demo/
│   ├── make_demo_data.py   # 生成演示 PNG
│   ├── demo_requests.sh    # 可直接运行的 curl 演示
│   └── *.png               # 演示图片
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
docker compose up --build -d
# 健康检查
curl http://127.0.0.1:8000/health
# 跑全套演示请求（创建草案、补录、冲突、领取 PNG）
bash demo/demo_requests.sh
```

SQLite 数据库通过卷挂载持久化在 `./data/drafts.db`。

## 本地运行（无 Docker）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 交互式文档：http://127.0.0.1:8000/docs
```

## 约束与规则

- 两张图每边至多 **64 像素**；参考图最多 **4 色**且必须完全不透明。
- 残片透明像素（alpha < 128）表示缺损，不透明像素为完好色点。
- 重复单元每边 **4～12 格**；残片像素 `(x,y)` 投影到纹版格 `(x%W, y%H)`。
- 纹版按环形（toroidal）处理：以每格为左上角的 2×2 图块都必须在参考图
  出现过，**跨边、跨角环绕也算**。
- 目标：在所有可行纹版中，最大化各 2×2 图块在参考图中出现次数（频次）之和。
- 求解限时 **5 秒**（墙钟，含安全余量）。

## 求解状态

| 状态 | 含义 |
|------|------|
| `optimal` | 已证最优（达到理论上界，或 CP-SAT 精确证明） |
| `feasible` | 仅找到可行纹样（未证全局最优） |
| `infeasible` | 已证无解 |
| `unknown` | 待定（限时内连可行性都未判定） |

只有 `optimal` / `feasible` 这类有候选的结果会写入 SQLite 并返回 `draft_id`。

## API

### `POST /api/drafts`
multipart 表单：`reference`（PNG）、`fragment`（PNG）、`unit_w`、`unit_h`。

- 同格异色（周期对齐后）→ `409`，返回冲突格与残片**两处原坐标**，不建草案。
- 正常 → 返回状态、图块频次统计、候选网格；有候选时返回 `draft_id`。

### `POST /api/drafts/{id}/points`
在缺损处补录完好色点。表单：`color=r,g,b`，坐标二选一：
- 残片原坐标 `x,y`（自动取模投影）；
- 纹版格坐标 `cell_x,cell_y`。

补录后重新限时求解并更新草案。同格异色 → `409` 且**草案原样保留**。

### `GET /api/drafts/{id}`
查看草案当前状态、全部色点约束、候选网格与图块使用情况。

### `GET /api/drafts/{id}/pattern.png?scale=1`
领取正式纹版 PNG。仅当草案**已证最优**且纹版**符合当前全部色点**时放行，
否则 `409`。

## 求解器设计（三层）

1. **CP-SAT 精确优化（短预算）**：结构简单/约束多时直接证最优。
2. **CP-SAT 纯可行性**：颜色整数变量 + 每位置一条 `add_allowed_assignments`
   四元组允许表（≤256 行），最坏尺寸通常 0.1 秒内判定可行性或严格证无解。
3. **局部搜索**：在可行网格上单格变色爬山 + 随机重启，增量评估只看受影响的
   4 个图块，用满剩余预算冲最高分；达到「格数 × 最高频次」理论上界即声明最优。

无 OR-Tools 时自动退回内置的行编码回溯 + 分支定界（带递归剪枝与时限检查）。

## 测试

```bash
pip install pytest httpx
python -m pytest tests/ -v
```

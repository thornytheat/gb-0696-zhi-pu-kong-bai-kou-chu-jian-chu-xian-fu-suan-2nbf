# 质谱空白扣除与检出限复算

环境实验室分析员使用本工具接收仪器峰面积与标曲空白，复算浓度并检查稀释与检出限制。

## 核心计算

最小二乘标曲必须满秩，空白扣除保留负值，浓度按稀释倍数复算；低于检出限保留限定符。

## 启动

```bash
docker compose up --build -d
docker compose exec -T app pytest -q
docker compose config --quiet
```

浏览器访问 http://localhost:18210 。默认载入仓内样例。工具条可选择CSV文件运行；样例文件位于 `sample_data/instrument.csv`。重复执行同一内容复用同一批次。

## 模块

- M1 仪器接收：`/module/1`，独立计算 `POST /api/1`。
- M2 内标归一：`/module/2`，独立计算 `POST /api/2`。
- M3 标曲拟合：`/module/3`，独立计算 `POST /api/3`。
- M4 空白扣除：`/module/4`，独立计算 `POST /api/4`。
- M5 浓度复算：`/module/5`，独立计算 `POST /api/5`。
- M6 质控判定：`/module/6`，独立计算 `POST /api/6`。
- M7 结果复核：`/module/7`，独立计算 `POST /api/7`。
- M8 定量下限：`/module/8`，档案管理 `POST /api/8/confirm`、`/api/8/activate`、`/api/8/apply`。

## 定量下限档案

方法确认文件（空白+低浓度加标，样例 `sample_data/confirmation_pb_3rep.csv`）经 `POST /api/8/confirm` 生成定量下限档案：内容寻址、只插不改，同一确认文件重试不新增档案。档案给出检出限、暂扣上限、定量下限与依据；平行数不足7次为“暂定”，低浓度证据不足区间的样品判“暂扣”，避免把噪声当检出。

`POST /api/8/activate` 切换某分析项的在用档案：校验全部前置（档案完整、阈值有序、原始记录在位），切换记录与指针在单事务内提交，失败保留上一版。`POST /api/8/apply` 让常规批次按同一版在用档案给出未检出、暂扣、检出、定量；同批次同档案重试复用同一结果。切换记录载明报告范围的扩大/收窄区间、依据与受影响样品，`/module/8` 与 `GET /api/8/report?dossier_id=` 可复核。原始加标与空白记录只有插入路径，覆盖写在结构上不存在。

## 输入与输出

CSV可用UTF-8或GB18030；上传保留字符串编号和物理行号。POST `/api/run` 接收 `text` 字段。`GET /api/runs` 查询历史，`POST /api/review` 接收 id、revision、decision。导出按钮下载唯一证据CSV，含运行摘要与隔离记录。

SQLite命名卷保存运行记录。重建演示数据可更换卷名后重启。无需外部网络数据。规则定义见 `docs/DATA-DICTIONARY.md`；边界和测试对应关系见 `docs/DEPTH.md`。

## 范围

只处理仓内CSV定义，不连接生产系统。单次输入最多2MB。原始输入流式解析，业务计算阶段需容纳该文件的采信行。并非无限容量任务。

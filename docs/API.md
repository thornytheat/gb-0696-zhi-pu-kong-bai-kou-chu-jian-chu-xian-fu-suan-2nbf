# 接口

`POST /api/run`：{"text":"CSV内容"}。同规则版本与输入摘要只产生一条批次。

`GET/POST /api/1` 至 `/api/7`：独立样例或自定义文件计算。各模块返回 report、quarantine、balance。

`POST /api/review`：id、整数revision、decision取通过或退回。旧版本409；阻断项不能通过。

`POST /api/8/confirm`：{"text":"确认CSV","params":{}}。生成定量下限档案；同一确认文件重试返回同一dossier_id且created=false。

`POST /api/8/activate`：{"dossier_id":"…","force":false}。切换该分析项在用档案；档案不存在、被改、阈值异常或无效（未force）时409/404，在用版本保持上一版。返回范围变化与受影响样品。

`POST /api/8/apply`：{"run_id":"…"}。常规批次按在用档案判定未检出/暂扣/检出/定量；同批次同档案重试复用同一结果。无在用档案409。

`GET /api/8`：在用档案、分区、依据与切换记录。`GET /api/8/report?dossier_id=`：单份档案复核。`GET /api/8/sample`：样例确认文件。

`GET /api/export/{id}`：唯一证据CSV。`GET /health`：存活检查。

错误：结构/数值422，版本冲突409，不存在404，文件过大413。逐行异常进入quarantine。

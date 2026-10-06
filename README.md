# 值班津贴复核台

跨午夜值班同时命中夜班、周末、专项津贴时，**倍率不能直接相加**（会重复计费），
人工按整小时切段又会漏掉规则边界。本项目是一个轻量全栈复核台：

- **web/**：React + Vite + TypeScript，编辑值班区间与每周重复规则，展示分段时间轴与逐段明细。
- **backend/**：FastAPI，对区间**逐分钟裁决**，在午夜与规则边界处精确切片，
  每段工资与总额都以**不可约分数**返回，全程不四舍五入。
- **docker-compose.yml**：两个服务，`web`（nginx:8080）反代 `/api` 到 `api`（uvicorn:8000）。

## 裁决规则

1. 请求包含一个不超过 36 小时、精确到分钟的本地（无时区）值班区间、正整数
   `baseRateCents`，以及至多 20 条每周重复规则。
2. 规则字段：唯一 `id`、星期集合（0=周一 … 6=周日）、可跨午夜的分钟窗口
   `[startMinute, endMinute)`、`priority`、以 basisPoints 表示的倍率（10000 = 1.0×）。
   `endMinute < startMinute` 表示跨午夜；`endMinute = 1440` 即 24:00。
3. **同一分钟只采用一条规则**：取 `priority` 最大者；仍并列时取 `id` 的
   **UTF-8 字节序**最小者；无规则命中按 10000。
4. 相邻且裁决相同的分钟归并为一段；**午夜 00:00 一定断开**，即使两侧规则相同。
5. 段工资 = `分钟数 × baseRateCents × basisPoints / (10000 × 60)`，
   用 Python `Fraction` 累加，每段与总额都是不可约分数（另附仅用于展示的六位小数）。
6. 无效日期、空星期集合、空窗口（start==end）、结束不晚于开始、超过 36 小时、
   重复 id、超过 20 条规则等一律返回 **422**；前端每次提交先清除旧结果。

## 一次性例外（节假日临时值班通知）

请求可额外携带至多 10 条锚定**具体日期**的分钟级例外，用于在每周规则之上叠加
一次性通知。例外字段：唯一 `id`、`date`（`YYYY-MM-DD`）、
`[startMinute, endMinute)`、`kind`，以及（仅 REPLACE 需要的）`basisPoints`：

- `endMinute` 可超过 1440，超出部分落在**真实次日**（上限 2880 = 次日 24:00），
  跨午夜部分按午夜切片归入实际次日，午夜 00:00 一定断开。
- **SUPPRESS**：该窗口按基础倍率 10000 计，任何周规则（含最高 priority）都不生效，
  且不得携带 `basisPoints`。
- **REPLACE**：该窗口采用例外自带倍率（必须给出正整数 `basisPoints`），
  任何周规则都不生效；**倍率从不相加**。
- 例外优先于一切周规则；**窗口之外**仍按周规则的 priority / UTF-8 id 裁决。
  响应分段会额外给出 `exceptionId`、`exceptionKind`，并在 `ruleId` 保留
  被压过的周规则（可能为 null），仅用于向复核员解释该分钟原本命中哪条规则。
- 两个例外窗口在真实时间线上有正时长**重叠**时**整次请求拒绝（422）**，
  报错 id 对按 UTF-8 字节序规范化，**与输入顺序无关**；**相接**（end==start，
  含跨午夜相接）合法。
- `exceptions` 缺省、为 `null` 或 `[]` 时走旧流程，旧结果逐项不变。

后端在**午夜、周规则边界与例外边界**统一重新切片，时间轴、分段表与总额来自
**同一份响应**（同一组分段数据）。

复核员通过每段的「开始/结束/星期/分钟数/采用来源/被压过的规则 id/倍率/分数工资」即可解释其中的
**每一分钟**采用了哪条规则；页面时间轴悬停可见同样的信息。

## 不计薪休息（经批准的跨午夜休息）

请求可额外携带至多 10 段休息时间，用于登记值班区间内**经批准的不计薪休息**。
休息字段：`start`、`end`——**真实本地分钟时间**（无时区、精确到分钟）的半开区间
`[start, end)`，可跨午夜（直接用次日的本地时间表示，仍在 00:00 断段）：

```json
"breaks": [
  {"start": "2026-09-25T23:30", "end": "2026-09-26T00:30"}
]
```

- **休息优先于一切倍率规则**（含一次性例外）：休息分钟工资为零（倍率按 0 计）。
- 分段仍保留该分钟**原本命中的周规则与例外**（`ruleId`、`exceptionId`、
  `exceptionKind`），并标记 `isBreak: true`、`basisPoints: 0`、`pay: 0/1`，
  复核员可据此解释「休息与周规则/例外相遇时为何没有计费」。
- 计薪与休息片段在**同一次逐分钟裁决**中生成，响应分别汇总
  `totalMinutes` / `paidMinutes` / `breakMinutes` 与精确分数 `total`
  （各段分数之和），**不是**先按旧规则算总额再扣一个近似值。
- 休息必须**整段落在值班区间内**；**相接**（end==start，含跨午夜相接）允许；
  正时长**重叠**、**越界**或非法时间（带秒/带时区/空窗口）一律整次拒绝（422），
  判定与输入顺序无关。
- `breaks` 缺省、为 `null` 或 `[]` 时走旧流程，旧分段与总额逐项不变。

## Docker Compose 运行

```bash
docker compose up --build
# web: http://localhost:8080   api: http://localhost:8000/api/health
```

## 本地开发

```bash
# 后端
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# 前端（dev server 已配置 /api 代理到 8000）
cd web
npm install
npm run dev          # http://localhost:5173
```

## 测试

**pytest 逐分钟对拍**（40 个随机种子 + 例外 30 个随机种子 + 边界用例 + HTTP 校验用例）：

```bash
cd backend
pip install pytest httpx
pytest
```

`tests/test_slice.py` 内置一个与引擎实现相互独立的 oracle（独立的窗口覆盖判定与
排序裁决），逐分钟跑一遍后与引擎分段逐段对拍，包括分段边界、分钟数、分数总额、
不可约性、UTF-8 id 平局（含中文 id）。

`tests/test_exceptions.py` 内置第二个独立 oracle（独立的真实时间线区间与例外
压过判定），逐分钟对拍跨午夜例外归入次日、SUPPRESS/REPLACE 压过周末高
priority 规则、周规则/例外边界重切、相接（含跨午夜相接）但不重叠、
正时长重叠（含跨午夜重叠 1 分钟）整次拒绝且与输入顺序无关、以及未提交例外时
旧结果逐项不变。

`tests/test_breaks.py` 内置第三个独立 oracle（独立的真实本地分钟半开区间
休息覆盖判定），逐分钟对拍休息覆盖（工资精确为零、原本命中的周规则/例外保留）、
休息压过 SUPPRESS/REPLACE 例外、休息跨午夜断段、相接（含跨午夜相接）允许、
正时长重叠与越界整次拒绝且与输入顺序无关、计薪/休息时长分别汇总、以及
未提交休息时旧结果逐项不变。

**Playwright 浏览器测试**（一次录入与结果展示，另含一次 422 清除旧结果；
休息用例额外核对时间轴 tooltip 与明细表逐段金额一致、各段分数之和等于总额）：

```bash
cd web
npx playwright install chromium
E2E_BASE_URL=http://localhost:8080 npx playwright test   # compose 已启动
# 或开发模式：E2E_BASE_URL=http://localhost:5173 npx playwright test
```

## API

`POST /api/calculate`，请求体（camelCase）：

```json
{
  "start": "2026-09-25T22:00",
  "end": "2026-09-26T02:00",
  "baseRateCents": 6000,
  "rules": [
    {"id": "night",   "weekdays": [0,1,2,3,4,5,6], "startMinute": 1320, "endMinute": 360,  "priority": 1, "basisPoints": 12000},
    {"id": "weekend", "weekdays": [5,6],           "startMinute": 0,    "endMinute": 1440, "priority": 2, "basisPoints": 15000},
    {"id": "special", "weekdays": [4],             "startMinute": 1380, "endMinute": 1440, "priority": 3, "basisPoints": 20000}
  ]
}
```

上例（周五 22:00 → 周六 02:00）切成三段：22–23 night 7200 分、
23–24 special 12000 分、00–02 weekend 18000 分，总额 **37200/1 分**——
倍率从不相加，每分钟只由一条规则负责。

`exceptions`（可选）示例：周五 23:00 起的临时通知跨午夜到周六 02:00，
窗口内一律按基础倍率（SUPPRESS，压过包括 weekend 在内的一切周规则）：

```json
"exceptions": [
  {"id": "holiday", "date": "2026-09-25", "startMinute": 1380,
   "endMinute": 1560, "kind": "SUPPRESS"}
]
```

REPLACE 例：`{"id": "duty", "date": "2026-09-26", "startMinute": 600,
"endMinute": 720, "kind": "REPLACE", "basisPoints": 20000}`。

`breaks`（可选）例：周五 23:30 起的经批准休息跨午夜到周六 00:30，休息分钟
不计薪，但分段保留原本命中的规则/例外：

```json
"breaks": [{"start": "2026-09-25T23:30", "end": "2026-09-26T00:30"}]
```

响应在 `segments` / `total` / `totalMinutes` 之外另给 `paidMinutes` 与
`breakMinutes`；休息段 `isBreak: true`、`basisPoints: 0`、`pay: 0/1`。

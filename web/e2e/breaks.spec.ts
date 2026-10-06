import { test, expect, type Page } from '@playwright/test'

// 不计薪休息（经批准的跨午夜休息）的浏览器流程：
// 默认场景 周五22:00 → 周六02:00 + night/weekend/special 三条周规则。
// 重点：休息段工资为零但保留原本命中的规则/例外；时间轴与明细表消费同一响应，
// 两者金额必须一致。

/** 明细表每段的分数工资（如 "7200/1"）与分钟数。 */
async function tableSegments(page: Page): Promise<{ pay: string; minutes: number }[]> {
  const rows = page.locator('[data-testid^="seg-row-"]')
  const count = await rows.count()
  const out: { pay: string; minutes: number }[] = []
  for (let i = 0; i < count; i++) {
    const pay = (await rows.nth(i).locator('td.frac').textContent())!.trim()
    const minutes = Number((await rows.nth(i).locator('td').nth(4).textContent())!.trim())
    out.push({ pay, minutes })
  }
  return out
}

/** 时间轴每段 tooltip 中的分数工资与分钟数。 */
async function timelineSegments(page: Page): Promise<{ pay: string; minutes: number }[]> {
  const titles = await page
    .locator('[data-testid^="timeline-seg-"]')
    .evaluateAll((els) => els.map((e) => e.getAttribute('title') ?? ''))
  return titles.map((t) => {
    const pay = /工资: (\d+\/\d+) 分/.exec(t)![1]
    const minutes = Number(/时长: (\d+) 分钟/.exec(t)![1])
    return { pay, minutes }
  })
}

function gcd(a: bigint, b: bigint): bigint {
  return b === 0n ? a : gcd(b, a % b)
}

/** 把 "n/d" 分数列表求和并约分，返回 "n/d"。 */
function sumFractions(fracs: string[]): string {
  let n = 0n
  let d = 1n
  for (const f of fracs) {
    const [fn, fd] = f.split('/').map(BigInt)
    n = n * fd + fn * d
    d = d * fd
    const g = gcd(n < 0n ? -n : n, d)
    n /= g
    d /= g
  }
  return `${n}/${d}`
}

test.describe('不计薪休息', () => {
  test('跨午夜休息：休息段工资为零、保留原本命中的周规则，图表金额一致', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('shift-start').fill('2026-09-25T22:00')
    await page.getByTestId('shift-end').fill('2026-09-26T02:00')
    await page.getByTestId('base-rate').fill('6000')

    // 休息 周五 23:30 → 周六 00:30（跨午夜）
    await page.getByTestId('add-break').click()
    await page.getByTestId('break-start-0').fill('2026-09-25T23:30')
    await page.getByTestId('break-end-0').fill('2026-09-26T00:30')

    await page.getByTestId('submit').click()
    await expect(page.getByTestId('result-panel')).toBeVisible()

    // 分段：night(60) / special(30) / 休息(30, 原 special) / 休息(30, 原 weekend) / weekend(90)
    const rows = page.getByTestId(/seg-row-\d+/)
    await expect(rows).toHaveCount(5)
    await expect(page.getByTestId('seg-break-2')).toContainText('休息（不计薪）')
    await expect(page.getByTestId('seg-break-3')).toContainText('休息（不计薪）')
    // 休息段保留原本命中的周规则，供复核解释
    await expect(page.getByTestId('seg-row-2')).toContainText('special')
    await expect(page.getByTestId('seg-row-3')).toContainText('weekend')
    await expect(page.getByTestId('seg-row-2')).toContainText('0.00×')
    // 休息跨午夜在 00:00 断段
    await expect(page.getByTestId('seg-row-2')).toContainText('2026-09-26 00:00')
    await expect(page.getByTestId('seg-row-3')).toContainText('2026-09-26 00:00')

    // 总额 = 7200 + 6000 + 0 + 0 + 13500 = 26700；计薪 180 分钟、休息 60 分钟
    await expect(page.getByTestId('total')).toContainText('26700/1')
    await expect(page.getByTestId('total')).toContainText('240 分钟')
    await expect(page.getByTestId('total')).toContainText('计薪 180')
    await expect(page.getByTestId('total')).toContainText('休息 60')

    // 时间轴与明细表消费同一响应：逐段金额、分钟数一致
    const table = await tableSegments(page)
    const chart = await timelineSegments(page)
    expect(chart).toEqual(table)
    // 各段分数之和严格等于总额（同一次裁决生成，非旧总额扣近似值）
    expect(sumFractions(table.map((s) => s.pay))).toBe('26700/1')
    // 时间轴休息段 tooltip 可见被压过的周规则
    const breakTitle = await page.getByTestId('timeline-seg-2').getAttribute('title')
    expect(breakTitle).toContain('休息（不计薪）')
    expect(breakTitle).toContain('周规则 special')
    expect(breakTitle).toContain('工资: 0/1 分')
  })

  test('休息压过 REPLACE 例外：例外信息保留，工资为零', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('shift-start').fill('2026-09-25T22:00')
    await page.getByTestId('shift-end').fill('2026-09-26T02:00')
    await page.getByTestId('base-rate').fill('6000')

    // REPLACE 例外 周五 23:30-23:59（3.0×）
    await page.getByTestId('add-exception').click()
    await page.getByTestId('exc-id-0').fill('temp')
    await page.getByTestId('exc-date-0').fill('2026-09-25')
    await page.getByTestId('exc-kind-0').selectOption('REPLACE')
    await page.getByTestId('exc-bp-0').fill('30000')
    await page.getByTestId('exc-start-0').fill('23:30')
    await page.getByTestId('exc-end-0').fill('23:59')

    // 休息 周五 23:45 → 周六 00:15，覆盖例外窗口尾部
    await page.getByTestId('add-break').click()
    await page.getByTestId('break-start-0').fill('2026-09-25T23:45')
    await page.getByTestId('break-end-0').fill('2026-09-26T00:15')

    await page.getByTestId('submit').click()
    await expect(page.getByTestId('result-panel')).toBeVisible()

    // night(60) / special(30) / temp(15) / 休息(14, 原 temp+special) / 休息(1, 原 special)
    // / 休息(15, 原 weekend) / weekend(105)
    const rows = page.getByTestId(/seg-row-\d+/)
    await expect(rows).toHaveCount(7)
    await expect(page.getByTestId('seg-exception-2')).toContainText('例外 temp（REPLACE）')
    // 休息段同时保留原本命中的例外与周规则
    await expect(page.getByTestId('seg-break-3')).toContainText('休息（不计薪）')
    await expect(page.getByTestId('seg-row-3')).toContainText('例外 temp（REPLACE）')
    await expect(page.getByTestId('seg-row-3')).toContainText('周规则 special')
    await expect(page.getByTestId('seg-row-5')).toContainText('周规则 weekend')

    // 总额 = 7200 + 6000 + 4500 + 0 + 0 + 0 + 15750 = 33450
    await expect(page.getByTestId('total')).toContainText('33450/1')
    await expect(page.getByTestId('total')).toContainText('计薪 210')
    await expect(page.getByTestId('total')).toContainText('休息 30')

    const table = await tableSegments(page)
    const chart = await timelineSegments(page)
    expect(chart).toEqual(table)
    expect(sumFractions(table.map((s) => s.pay))).toBe('33450/1')
  })

  test('休息重叠或越界整次拒绝（422）并清除旧结果，相接则通过', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('shift-start').fill('2026-09-25T22:00')
    await page.getByTestId('shift-end').fill('2026-09-26T02:00')
    await page.getByTestId('base-rate').fill('6000')

    // 先成功一次
    await page.getByTestId('submit').click()
    await expect(page.getByTestId('result-panel')).toBeVisible()

    // 两段休息真实时间线重叠 30 分钟（跨午夜）
    await page.getByTestId('add-break').click()
    await page.getByTestId('break-start-0').fill('2026-09-25T23:00')
    await page.getByTestId('break-end-0').fill('2026-09-26T00:30')
    await page.getByTestId('add-break').click()
    await page.getByTestId('break-start-1').fill('2026-09-26T00:00')
    await page.getByTestId('break-end-1').fill('2026-09-26T01:00')

    await page.getByTestId('submit').click()
    await expect(page.getByTestId('error-panel')).toBeVisible()
    await expect(page.getByTestId('error-panel')).toContainText('重叠')
    await expect(page.getByTestId('result-panel')).toHaveCount(0)

    // 把第二段休息改成 00:30 起（相接）→ 通过（休息共 90+30=120 分钟）
    await page.getByTestId('break-start-1').fill('2026-09-26T00:30')
    await page.getByTestId('submit').click()
    await expect(page.getByTestId('result-panel')).toBeVisible()
    await expect(page.getByTestId('error-panel')).toHaveCount(0)
    await expect(page.getByTestId('total')).toContainText('休息 120')

    // 休息越出值班区间 → 422，旧结果清除
    await page.getByTestId('break-end-1').fill('2026-09-26T03:00')
    await page.getByTestId('submit').click()
    await expect(page.getByTestId('error-panel')).toBeVisible()
    await expect(page.getByTestId('error-panel')).toContainText('越出值班区间')
    await expect(page.getByTestId('result-panel')).toHaveCount(0)
  })
})

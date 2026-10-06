import { test, expect } from '@playwright/test'

// 经批准的不计薪休息浏览器流程：
// 默认场景 周五22:00 → 周六02:00 + night/weekend/special 三条周规则。
test.describe('经批准的不计薪休息', () => {
  test('跨午夜休息零工资、午夜断段、保留原本命中，图表与明细金额一致', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('shift-start').fill('2026-09-25T22:00')
    await page.getByTestId('shift-end').fill('2026-09-26T02:00')
    await page.getByTestId('base-rate').fill('6000')

    // 周五 23:00 → 周六 00:00 休息（压在 special 上）
    await page.getByTestId('add-break').click()
    await page.getByTestId('break-start-0').fill('2026-09-25T23:00')
    await page.getByTestId('break-end-0').fill('2026-09-26T00:00')

    await page.getByTestId('submit').click()
    await expect(page.getByTestId('result-panel')).toBeVisible()

    // 分段：night(60 计薪) / 休息(60, 原本 special) / weekend(120 计薪)
    const rows = page.getByTestId(/seg-row-\d+/)
    await expect(rows).toHaveCount(3)
    await expect(page.getByTestId('seg-break-1')).toContainText('休息（不计薪）')
    // 休息分钟仍保留原本命中的 special 与 2.00× 倍率
    await expect(page.getByTestId('seg-break-underlying-1')).toContainText('special')
    await expect(page.getByTestId('seg-row-1')).toContainText('2.00×')
    await expect(page.getByTestId('seg-row-1')).toContainText('0/1')

    // 总时长不变（240），计薪 180、休息 60；总额 7200 + 18000 = 25200
    await expect(page.getByTestId('total')).toContainText('25200/1')
    await expect(page.getByTestId('total')).toContainText('共 240 分钟')
    await expect(page.getByTestId('total')).toContainText('计薪 180 分钟')
    await expect(page.getByTestId('total')).toContainText('休息 60 分钟')

    // 图表：三段、休息段 hover 可见零工资与原本命中，与明细表同一响应
    const segs = page.locator('[data-testid^="timeline-seg-"]')
    await expect(segs).toHaveCount(3)
    const restTitle = await segs.nth(1).getAttribute('title')
    expect(restTitle).toContain('休息（不计薪）')
    expect(restTitle).toContain('工资: 0/1 分')
    expect(restTitle).toContain('原本命中: special')
    const chartMinutes = await segs.evaluateAll((els) =>
      els
        .map((el) => el.getAttribute('title')?.match(/时长: (\d+) 分钟/)?.[1] ?? '0')
        .reduce((a, b) => a + Number(b), 0),
    )
    expect(chartMinutes).toBe(240)

    // 明细表各段分数工资之和必须等于页头总额（图表/明细消费同一响应）
    const rowPays = await page.locator('td.frac').allInnerTexts()
    const sum = rowPays
      .map((t) => t.split('/').map(Number))
      .reduce((acc, [n, d]) => acc + n / d, 0)
    expect(sum).toBe(25200)
  })

  test('休息压过 REPLACE 例外：零工资但保留例外与被压过的周规则', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('shift-start').fill('2026-09-26T00:00')
    await page.getByTestId('shift-end').fill('2026-09-26T03:00')
    await page.getByTestId('base-rate').fill('6000')

    // 只保留 weekend 规则：删除 night、special（行索引变化，连续删除 0 两次）
    await page.getByTestId('rule-remove-0').click()
    await page.getByTestId('rule-remove-1').click()

    // REPLACE 例外 周六 01:00-03:00 按 2.0×
    await page.getByTestId('add-exception').click()
    await page.getByTestId('exc-id-0').fill('duty')
    await page.getByTestId('exc-date-0').fill('2026-09-26')
    await page.getByTestId('exc-kind-0').selectOption('REPLACE')
    await page.getByTestId('exc-bp-0').fill('20000')
    await page.getByTestId('exc-start-0').fill('01:00')
    await page.getByTestId('exc-end-0').fill('03:00')

    // 两段相接休息：01:00-02:00、02:00-03:00 → 合法（相接），覆盖整个例外
    await page.getByTestId('add-break').click()
    await page.getByTestId('break-start-0').fill('2026-09-26T01:00')
    await page.getByTestId('break-end-0').fill('2026-09-26T02:00')
    await page.getByTestId('add-break').click()
    await page.getByTestId('break-start-1').fill('2026-09-26T02:00')
    await page.getByTestId('break-end-1').fill('2026-09-26T03:00')

    await page.getByTestId('submit').click()
    await expect(page.getByTestId('result-panel')).toBeVisible()

    const rows = page.getByTestId(/seg-row-\d+/)
    // weekend(60 计薪) + 两个休息段（相接但午夜外、裁决相同会归并为一段 120 分钟）
    await expect(rows).toHaveCount(2)
    await expect(page.getByTestId('seg-break-1')).toContainText('休息（不计薪）')
    // 休息分钟保留例外 duty(REPLACE)、被压过的周规则 weekend、倍率 2.00×、工资 0
    await expect(page.getByTestId('seg-break-underlying-1')).toContainText('例外 duty（REPLACE）')
    await expect(page.getByTestId('seg-row-1')).toContainText('weekend')
    await expect(page.getByTestId('seg-row-1')).toContainText('2.00×')
    await expect(page.getByTestId('seg-row-1')).toContainText('0/1')
    // 仅第一小时 weekend@1.5× 计薪 = 9000；休息 120 分钟
    await expect(page.getByTestId('total')).toContainText('9000/1')
    await expect(page.getByTestId('total')).toContainText('计薪 60 分钟')
    await expect(page.getByTestId('total')).toContainText('休息 120 分钟')
  })

  test('休息重叠整次拒绝（422），改成相接后恢复，失败时无旧结论', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('shift-start').fill('2026-09-25T22:00')
    await page.getByTestId('shift-end').fill('2026-09-26T02:00')
    await page.getByTestId('base-rate').fill('6000')

    // 先成功一次，看到旧总额
    await page.getByTestId('submit').click()
    await expect(page.getByTestId('result-panel')).toBeVisible()
    await expect(page.getByTestId('total')).toContainText('37200/1')

    // 两段真实重叠 1 分钟的休息
    await page.getByTestId('add-break').click()
    await page.getByTestId('break-start-0').fill('2026-09-25T23:00')
    await page.getByTestId('break-end-0').fill('2026-09-26T00:01')
    await page.getByTestId('add-break').click()
    await page.getByTestId('break-start-1').fill('2026-09-26T00:00')
    await page.getByTestId('break-end-1').fill('2026-09-26T01:00')

    await page.getByTestId('submit').click()
    await expect(page.getByTestId('error-panel')).toBeVisible()
    await expect(page.getByTestId('error-panel')).toContainText('重叠')
    await expect(page.getByTestId('result-panel')).toHaveCount(0)

    // 越界（结束超出值班区间）同样整次拒绝
    await page.getByTestId('break-start-1').fill('2026-09-26T00:01')
    await page.getByTestId('break-end-1').fill('2026-09-26T02:01')
    await page.getByTestId('submit').click()
    await expect(page.getByTestId('error-panel')).toBeVisible()
    await expect(page.getByTestId('error-panel')).toContainText('越界')
    await expect(page.getByTestId('result-panel')).toHaveCount(0)

    // 改成 [00:01, 02:00)：与第一段相接、且整体位于值班区间 → 成功
    await page.getByTestId('break-end-1').fill('2026-09-26T02:00')
    await page.getByTestId('submit').click()
    await expect(page.getByTestId('result-panel')).toBeVisible()
    await expect(page.getByTestId('error-panel')).toHaveCount(0)
    // 休息 23:00-00:01（61 分钟）+ 00:01-02:00（119 分钟）= 180 分钟；
    // 计薪仅 22:00-23:00 night 一小时 = 7200
    await expect(page.getByTestId('total')).toContainText('7200/1')
    await expect(page.getByTestId('total')).toContainText('休息 180 分钟')
  })
})

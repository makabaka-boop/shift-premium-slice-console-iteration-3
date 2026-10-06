import { test, expect } from '@playwright/test'

// 一次性例外（节假日临时值班通知）的浏览器流程：
// 默认场景 周五22:00 → 周六02:00 + night/weekend/special 三条周规则。
test.describe('一次性例外', () => {
  test('REPLACE 例外压过周五晚上的高 priority 专项规则并精确切片', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('shift-start').fill('2026-09-25T22:00')
    await page.getByTestId('shift-end').fill('2026-09-26T02:00')
    await page.getByTestId('base-rate').fill('6000')

    // 添加一个周五 23:30-23:59 的 REPLACE 例外（3.0×），压过 special（2.0×）
    await page.getByTestId('add-exception').click()
    await page.getByTestId('exc-id-0').fill('temp')
    await page.getByTestId('exc-date-0').fill('2026-09-25')
    await page.getByTestId('exc-kind-0').selectOption('REPLACE')
    await page.getByTestId('exc-bp-0').fill('30000')
    await page.getByTestId('exc-start-0').fill('23:30')
    await page.getByTestId('exc-end-0').fill('23:59')

    await page.getByTestId('submit').click()
    await expect(page.getByTestId('result-panel')).toBeVisible()

    // 分段：night(60) / special(30) / temp(29) / special(1) / weekend(120)
    const rows = page.getByTestId(/seg-row-\d+/)
    await expect(rows).toHaveCount(5)
    await expect(page.getByTestId('seg-exception-2')).toContainText('例外 temp（REPLACE）')
    await expect(page.getByTestId('seg-row-2')).toContainText('3.00×')
    await expect(page.getByTestId('seg-row-2')).toContainText('special') // 被压过的周规则仍可见
    // 总额：7200 + 6000 + 8700 + 200 + 18000 = 40100
    await expect(page.getByTestId('total')).toContainText('40100/1')
    await expect(page.getByTestId('total')).toContainText('240 分钟')
  })

  test('SUPPRESS 跨午夜：次日部分归周六，压过周末规则', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('shift-start').fill('2026-09-25T22:00')
    await page.getByTestId('shift-end').fill('2026-09-26T02:00')
    await page.getByTestId('base-rate').fill('6000')

    // SUPPRESS 周五 23:00 → 次日 02:00（勾选「次日」，结束时刻 02:00）
    await page.getByTestId('add-exception').click()
    await page.getByTestId('exc-id-0').fill('holiday')
    await page.getByTestId('exc-date-0').fill('2026-09-25')
    await page.getByTestId('exc-kind-0').selectOption('SUPPRESS')
    await page.getByTestId('exc-start-0').fill('23:00')
    await page.getByTestId('exc-end-nextday-0').check()
    await page.getByTestId('exc-end-0').fill('02:00')

    await page.getByTestId('submit').click()
    await expect(page.getByTestId('result-panel')).toBeVisible()

    const rows = page.getByTestId(/seg-row-\d+/)
    await expect(rows).toHaveCount(3)
    await expect(page.getByTestId('seg-row-1')).toContainText('例外 holiday（SUPPRESS）')
    await expect(page.getByTestId('seg-row-2')).toContainText('例外 holiday（SUPPRESS）')
    // 周六 00-02 段被 SUPPRESS 压过的是 weekend 规则，倍率 1.0×
    await expect(page.getByTestId('seg-row-2')).toContainText('weekend')
    await expect(page.getByTestId('seg-row-2')).toContainText('1.00×')
    // 7200(night 1h) + 18000(suppress 3h) = 25200
    await expect(page.getByTestId('total')).toContainText('25200/1')
  })

  test('非法重叠整次拒绝（422），且不因输入顺序改变', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('shift-start').fill('2026-09-25T22:00')
    await page.getByTestId('shift-end').fill('2026-09-26T02:00')
    await page.getByTestId('base-rate').fill('6000')

    // 先成功一次
    await page.getByTestId('submit').click()
    await expect(page.getByTestId('result-panel')).toBeVisible()

    // 添加两个真实时间线重叠 1 分钟的例外（跨午夜）
    await page.getByTestId('add-exception').click()
    await page.getByTestId('exc-id-0').fill('a')
    await page.getByTestId('exc-date-0').fill('2026-09-25')
    await page.getByTestId('exc-start-0').fill('23:00')
    await page.getByTestId('exc-end-nextday-0').check()
    await page.getByTestId('exc-end-0').fill('00:01')

    await page.getByTestId('add-exception').click()
    await page.getByTestId('exc-id-1').fill('b')
    await page.getByTestId('exc-date-1').fill('2026-09-26')
    await page.getByTestId('exc-start-1').fill('00:00')
    await page.getByTestId('exc-end-1').fill('01:00')

    await page.getByTestId('submit').click()
    await expect(page.getByTestId('error-panel')).toBeVisible()
    await expect(page.getByTestId('error-panel')).toContainText('重叠')
    await expect(page.getByTestId('result-panel')).toHaveCount(0)

    // 把 b 的起点改成 00:01（相接）即可通过
    await page.getByTestId('exc-start-1').fill('00:01')
    await page.getByTestId('submit').click()
    await expect(page.getByTestId('result-panel')).toBeVisible()
    await expect(page.getByTestId('error-panel')).toHaveCount(0)
  })
})

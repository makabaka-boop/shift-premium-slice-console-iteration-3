import { test, expect } from '@playwright/test'

// 浏览器测试只走一次录入与结果展示：填表单 → 一次提交 → 看到完整复核结果。
// 使用页面默认预置场景：周五 22:00 → 周六 02:00，night/weekend/special 三规则同时作用。
test.describe('值班津贴复核台', () => {
  test('一次录入即可看到精确切片与每段规则', async ({ page }) => {
    await page.goto('/')

    // —— 唯一一次录入：校对/重填默认值 ——
    await page.getByTestId('shift-start').fill('2026-09-25T22:00')
    await page.getByTestId('shift-end').fill('2026-09-26T02:00')
    await page.getByTestId('base-rate').fill('6000')
    await page.getByTestId('submit').click()

    // —— 结果展示（不再有第二次请求）——
    await expect(page.getByTestId('result-panel')).toBeVisible()

    // 总时长 240 分钟；三段：周五22-23 night / 23-24 special / 周六00-02 weekend
    await expect(page.getByTestId('total')).toContainText('37200/1')
    await expect(page.getByTestId('total')).toContainText('240 分钟')
    await expect(page.getByTestId('total')).toContainText('3 段')

    const rows = page.getByTestId(/seg-row-\d+/)
    await expect(rows).toHaveCount(3)

    await expect(page.getByTestId('seg-row-0')).toContainText('night')
    await expect(page.getByTestId('seg-row-0')).toContainText('60')
    await expect(page.getByTestId('seg-row-1')).toContainText('special')
    await expect(page.getByTestId('seg-row-1')).toContainText('60')
    await expect(page.getByTestId('seg-row-2')).toContainText('weekend')
    await expect(page.getByTestId('seg-row-2')).toContainText('120')

    // 时间轴三段均可 hover 查看适用规则
    await expect(page.locator('[data-testid^="timeline-seg-"]')).toHaveCount(3)

    // 分数均为不可约形式展示（总额 37200/1）
    await expect(page.getByTestId('total')).toContainText('37200/1')
  })

  test('422 时清除旧结果并展示校验信息', async ({ page }) => {
    await page.goto('/')

    // 先得到一次成功结果
    await page.getByTestId('shift-start').fill('2026-09-25T22:00')
    await page.getByTestId('shift-end').fill('2026-09-26T02:00')
    await page.getByTestId('base-rate').fill('6000')
    await page.getByTestId('submit').click()
    await expect(page.getByTestId('result-panel')).toBeVisible()

    // 再提交一个区间错误（结束早于开始）
    await page.getByTestId('shift-end').fill('2026-09-25T20:00')
    await page.getByTestId('submit').click()
    await expect(page.getByTestId('error-panel')).toBeVisible()
    await expect(page.getByTestId('result-panel')).toHaveCount(0)
  })
})

import { useState } from 'react'
import { calculate, ApiError } from './api'
import type { CalculateResponse, ExceptionInput, RuleInput, ValidationIssue } from './types'
import { RuleEditor } from './components/RuleEditor'
import { ExceptionEditor } from './components/ExceptionEditor'
import { Timeline } from './components/Timeline'
import { SegmentTable } from './components/SegmentTable'
import { DEFAULT_RULES, defaultStart, defaultEnd } from './defaults'

const MAX_RULES = 20
const MAX_EXCEPTIONS = 10

function blankRule(): RuleInput {
  return {
    id: '',
    weekdays: [0, 1, 2, 3, 4],
    startMinute: 22 * 60,
    endMinute: 6 * 60,
    priority: 0,
    basisPoints: 10000,
  }
}

function blankException(): ExceptionInput {
  return {
    id: '',
    date: '2026-09-26',
    startMinute: 10 * 60,
    endMinute: 11 * 60,
    kind: 'SUPPRESS',
    basisPoints: null,
  }
}

export default function App() {
  const [start, setStart] = useState(defaultStart())
  const [end, setEnd] = useState(defaultEnd())
  const [baseRate, setBaseRate] = useState(6000)
  const [rules, setRules] = useState<RuleInput[]>(DEFAULT_RULES)
  const [exceptions, setExceptions] = useState<ExceptionInput[]>([])

  const [result, setResult] = useState<CalculateResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [issues, setIssues] = useState<ValidationIssue[]>([])
  const [submitting, setSubmitting] = useState(false)

  const updateRule = (i: number, next: RuleInput) =>
    setRules((rs) => rs.map((r, idx) => (idx === i ? next : r)))
  const removeRule = (i: number) => setRules((rs) => rs.filter((_, idx) => idx !== i))
  const addRule = () => setRules((rs) => (rs.length >= MAX_RULES ? rs : [...rs, blankRule()]))

  const updateException = (i: number, next: ExceptionInput) =>
    setExceptions((es) => es.map((e, idx) => (idx === i ? next : e)))
  const removeException = (i: number) => setExceptions((es) => es.filter((_, idx) => idx !== i))
  const addException = () =>
    setExceptions((es) => (es.length >= MAX_EXCEPTIONS ? es : [...es, blankException()]))

  /** 任何新的提交都先清除旧结果，避免复核员误看陈旧数据。 */
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setResult(null)
    setError(null)
    setIssues([])
    setSubmitting(true)
    try {
      const res = await calculate({
        start,
        end,
        baseRateCents: baseRate,
        rules,
        // 未提交例外时不带该字段：后端按旧周规则流程返回，旧结果逐项不变
        exceptions: exceptions.length > 0 ? exceptions : undefined,
      })
      setResult(res)
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message)
        setIssues(err.issues)
      } else {
        setError('网络错误，请确认 API 服务可用')
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="page">
      <header>
        <h1>值班津贴复核台</h1>
        <p className="subtitle">
          精确到分钟 · 同一分钟只取最高 priority（并列取 id 的 UTF-8 字节序最小者）·
          一次性例外优先于周规则 · 分数计费不四舍五入
        </p>
      </header>

      <form onSubmit={handleSubmit} className="panel">
        <div className="shift-grid">
          <label className="field">
            <span>值班开始（本地时间）</span>
            <input
              type="datetime-local"
              value={start}
              required
              data-testid="shift-start"
              onChange={(e) => setStart(e.target.value)}
            />
          </label>
          <label className="field">
            <span>值班结束（不含，本地时间）</span>
            <input
              type="datetime-local"
              value={end}
              required
              data-testid="shift-end"
              onChange={(e) => setEnd(e.target.value)}
            />
          </label>
          <label className="field">
            <span>基础时薪（分，正整数）</span>
            <input
              type="number"
              min={1}
              step={1}
              value={baseRate}
              required
              data-testid="base-rate"
              onChange={(e) => setBaseRate(Number(e.target.value))}
            />
          </label>
        </div>

        <div className="rules-head">
          <h2>每周重复规则（{rules.length}/{MAX_RULES}）</h2>
          <button
            type="button"
            onClick={addRule}
            disabled={rules.length >= MAX_RULES}
            data-testid="add-rule"
          >
            + 添加规则
          </button>
        </div>
        <div className="rules-list">
          {rules.map((rule, i) => (
            <RuleEditor
              key={i}
              index={i}
              rule={rule}
              onChange={(next) => updateRule(i, next)}
              onRemove={() => removeRule(i)}
            />
          ))}
          {rules.length === 0 && <p className="empty-hint">暂无规则：全部分钟按 10000（1.0×）计。</p>}
        </div>

        <div className="rules-head exc-head">
          <h2>一次性例外 · 节假日临时值班（{exceptions.length}/{MAX_EXCEPTIONS}）</h2>
          <button
            type="button"
            onClick={addException}
            disabled={exceptions.length >= MAX_EXCEPTIONS}
            data-testid="add-exception"
          >
            + 添加例外
          </button>
        </div>
        <p className="empty-hint">
          例外优先于一切周规则：<strong>SUPPRESS</strong> 窗口按基础倍率（1.0×）计，
          <strong>REPLACE</strong> 窗口采用指定倍率；窗口之外仍按周规则优先级裁决。
          窗口可跨午夜，超出部分归入实际次日；两个例外在真实时间线上重叠（相接不算）将整次拒绝。
        </p>
        <div className="rules-list">
          {exceptions.map((exc, i) => (
            <ExceptionEditor
              key={i}
              index={i}
              exception={exc}
              onChange={(next) => updateException(i, next)}
              onRemove={() => removeException(i)}
            />
          ))}
          {exceptions.length === 0 && (
            <p className="empty-hint">暂无例外：全部按每周重复规则裁决。</p>
          )}
        </div>

        <button type="submit" className="primary" disabled={submitting} data-testid="submit">
          {submitting ? '切片计算中…' : '精确切片并复核'}
        </button>
      </form>

      {error && (
        <section className="panel error-panel" data-testid="error-panel">
          <h2>422 · {error}</h2>
          <p>旧结果已清除，请修正后重新提交。</p>
          {issues.length > 0 && (
            <ul>
              {issues.map((it, i) => (
                <li key={i}>
                  <code>{it.loc.filter((x) => x !== 'body').join(' › ') || '(请求)'}</code>
                  ：{it.msg}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {result && (
        <section className="panel result-panel" data-testid="result-panel">
          <div className="result-head">
            <h2>复核结果</h2>
            <div className="total" data-testid="total">
              <div>
                总工资（分）：
                <strong>
                  {result.total.numerator}/{result.total.denominator}
                </strong>
              </div>
              <div className="decimal-hint">约 {result.total.decimal} 分（仅展示，不参与计算）</div>
              <div className="total-min">共 {result.totalMinutes} 分钟，{result.segments.length} 段</div>
            </div>
          </div>

          <Timeline segments={result.segments} />
          <SegmentTable segments={result.segments} />

          <details className="explainer">
            <summary>如何解释每一分钟？</summary>
            <ol>
              <li>后端按分钟遍历整个值班区间，对每分钟独立裁决命中的规则。</li>
              <li>
                一次性例外优先于一切周规则：SUPPRESS 按 10000 计，REPLACE 按例外倍率计
                （倍率从不相加）；窗口之外，同一分钟命中多条周规则时只取 priority 最大者，
                仍并列取规则 id 的 UTF-8 字节序最小者，无命中按 10000。
              </li>
              <li>
                相邻且裁决结果相同的分钟归并为一段；午夜 00:00 一定断开
                （例外跨午夜的分钟因此归入实际次日），周规则边界、例外边界同样断开。
              </li>
              <li>
                段工资 = 分钟数 × 时薪(分) × basisPoints / (10000 × 60)，全程用不可约分数累加，
                总额是各段分数之和，中途不做任何四舍五入。
              </li>
            </ol>
          </details>
        </section>
      )}
    </main>
  )
}

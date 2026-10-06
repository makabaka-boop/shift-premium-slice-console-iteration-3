import type { SegmentJson } from '../types'
import { weekdayName } from '../time'

interface Props {
  segments: SegmentJson[]
}

function fmt(iso: string): string {
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 逐段明细：每一段内的每一分钟都采用同一条裁决（段内任意分钟都可由此解释）。 */
export function SegmentTable({ segments }: Props) {
  return (
    <table className="seg-table" data-testid="seg-table">
      <thead>
        <tr>
          <th>#</th>
          <th>开始（本地）</th>
          <th>结束（本地，不含）</th>
          <th>星期</th>
          <th>分钟数</th>
          <th>采用来源</th>
          <th>被压过的周规则</th>
          <th>倍率</th>
          <th>段工资（分，分数）</th>
          <th>约值（分）</th>
        </tr>
      </thead>
      <tbody>
        {segments.map((s, i) => {
          const viaException = s.exceptionId !== null
          return (
            <tr
              key={i}
              data-testid={`seg-row-${i}`}
              className={viaException ? 'exception-row' : s.ruleId === null ? 'default-row' : ''}
            >
              <td>{i + 1}</td>
              <td>{fmt(s.start)}</td>
              <td>{fmt(s.end)}</td>
              <td>{weekdayName(s.weekday)}</td>
              <td>{s.minutes}</td>
              <td>
                {viaException ? (
                  <strong data-testid={`seg-exception-${i}`}>
                    例外 {s.exceptionId}（{s.exceptionKind}）
                  </strong>
                ) : s.ruleId === null ? (
                  <em>无规则（按 10000）</em>
                ) : (
                  s.ruleId
                )}
              </td>
              <td className="muted-cell">
                {viaException
                  ? s.ruleId === null
                    ? '（该分钟无周规则命中）'
                    : s.ruleId
                  : '—'}
              </td>
              <td>{(s.basisPoints / 10000).toFixed(2)}×</td>
              <td className="frac">
                {s.pay.numerator}/{s.pay.denominator}
              </td>
              <td className="decimal">{s.pay.decimal}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

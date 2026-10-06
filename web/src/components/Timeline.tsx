import type { SegmentJson } from '../types'
import { weekdayName } from '../time'

interface Props {
  segments: SegmentJson[]
}

function underlyingSource(s: SegmentJson): string {
  if (s.exceptionId !== null) return `例外 ${s.exceptionId}（${s.exceptionKind}）`
  return s.ruleId ?? '默认1.0×'
}

function segColor(s: SegmentJson): string {
  if (s.isBreak) return 'var(--seg-break)'
  if (s.exceptionId !== null) {
    return s.exceptionKind === 'SUPPRESS' ? 'var(--seg-exc-suppress)' : 'var(--seg-exc-replace)'
  }
  if (s.ruleId === null) return 'var(--seg-default)'
  if (s.basisPoints > 15000) return 'var(--seg-high)'
  if (s.basisPoints >= 10000) return 'var(--seg-mid)'
  return 'var(--seg-low)'
}

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

function axisLabel(iso: string): string {
  const d = new Date(iso)
  return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 按段时长等比绘制的时间轴；hover 可见每段适用来源。 */
export function Timeline({ segments }: Props) {
  const total = segments.reduce((sum, s) => sum + s.minutes, 0)
  return (
    <div className="timeline-wrap" data-testid="timeline">
      <div className="timeline" role="img" aria-label="值班分段时间轴">
        {segments.map((s, i) => {
          const width = total === 0 ? 0 : (s.minutes / total) * 100
          const source = s.isBreak
            ? '休息（不计薪）'
            : underlyingSource(s)
          const detail: string[] = []
          if (s.isBreak) {
            detail.push(`休息优先于所有倍率规则：工资 0；原本命中: ${underlyingSource(s)}`)
            if (s.exceptionId !== null) {
              detail.push(`被例外压过的周规则: ${s.ruleId ?? '（无命中）'}`)
            }
          } else if (s.exceptionId !== null) {
            detail.push(`被压过的周规则: ${s.ruleId ?? '（无命中）'}`)
          }
          return (
            <div
              key={i}
              className={`timeline-seg ${s.isBreak ? 'break' : s.exceptionId !== null ? 'exc' : ''}`}
              data-testid={`timeline-seg-${i}`}
              style={{ width: `${width}%`, background: segColor(s) }}
              title={`${axisLabel(s.start)} → ${axisLabel(s.end)}（${weekdayName(s.weekday)}）
采用来源: ${source}${detail.length ? `\n${detail.join('\n')}` : ''}
倍率: ${(s.basisPoints / 10000).toFixed(2)}×
时长: ${s.minutes} 分钟
工资: ${s.pay.numerator}/${s.pay.denominator} 分`}
            >
              <span className="timeline-label">{source}</span>
              <span className="timeline-min">{s.minutes}m</span>
            </div>
          )
        })}
      </div>
      <div className="timeline-axis">
        <span>{segments.length ? axisLabel(segments[0].start) : ''}</span>
        <span>{segments.length ? axisLabel(segments[segments.length - 1].end) : ''}</span>
      </div>
    </div>
  )
}

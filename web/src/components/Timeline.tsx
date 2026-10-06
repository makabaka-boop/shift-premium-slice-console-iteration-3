import type { SegmentJson } from '../types'
import { weekdayName } from '../time'

interface Props {
  segments: SegmentJson[]
}

function segColor(s: SegmentJson): string {
  if (s.isBreak) {
    // 休息段：灰色斜纹，直观区别于一切计薪段
    return 'repeating-linear-gradient(45deg, var(--seg-break) 0 8px, var(--seg-break-hi) 8px 16px)'
  }
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
            : s.exceptionId !== null
              ? `例外 ${s.exceptionId}（${s.exceptionKind}）`
              : s.ruleId ?? '默认1.0×'
          // 休息/例外压过的底层裁决，供复核员解释该分钟原本命中什么
          let suppressed = ''
          if (s.isBreak) {
            const parts: string[] = []
            if (s.exceptionId !== null) parts.push(`例外 ${s.exceptionId}（${s.exceptionKind}）`)
            if (s.ruleId !== null) parts.push(`周规则 ${s.ruleId}`)
            suppressed = `\n被压过: ${parts.length ? parts.join('；') : '（该分钟无规则命中）'}`
          } else if (s.exceptionId !== null) {
            suppressed = `\n被压过的周规则: ${s.ruleId ?? '（无命中）'}`
          }
          return (
            <div
              key={i}
              className={`timeline-seg ${s.exceptionId !== null ? 'exc' : ''} ${s.isBreak ? 'brk' : ''}`}
              data-testid={`timeline-seg-${i}`}
              style={{ width: `${width}%`, background: segColor(s) }}
              title={`${axisLabel(s.start)} → ${axisLabel(s.end)}（${weekdayName(s.weekday)}）
采用来源: ${source}${suppressed}
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

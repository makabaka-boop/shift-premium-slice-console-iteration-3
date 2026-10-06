import type { BreakInput } from '../types'

interface Props {
  index: number
  breakValue: BreakInput
  onChange: (next: BreakInput) => void
  onRemove: () => void
}

/**
 * 经批准的不计薪休息：真实本地分钟时间上的半开区间 [开始, 结束)。
 * 休息优先于一切倍率规则（含一次性例外），工资为 0 但保留原本命中的规则/例外。
 */
export function BreakEditor({ index, breakValue, onChange, onRemove }: Props) {
  return (
    <fieldset className="rule-card break-card" data-testid={`break-row-${index}`}>
      <legend>休息 #{index + 1}（不计薪，半开区间，须位于值班区间内）</legend>
      <div className="rule-grid">
        <label className="field">
          <span>休息开始</span>
          <input
            type="datetime-local"
            value={breakValue.start}
            data-testid={`break-start-${index}`}
            onChange={(e) => e.target.value && onChange({ ...breakValue, start: e.target.value })}
          />
        </label>

        <label className="field">
          <span>休息结束（不含该分钟，可跨午夜）</span>
          <input
            type="datetime-local"
            value={breakValue.end}
            data-testid={`break-end-${index}`}
            onChange={(e) => e.target.value && onChange({ ...breakValue, end: e.target.value })}
          />
        </label>
      </div>

      <button type="button" className="danger" onClick={onRemove} data-testid={`break-remove-${index}`}>
        删除休息
      </button>
    </fieldset>
  )
}

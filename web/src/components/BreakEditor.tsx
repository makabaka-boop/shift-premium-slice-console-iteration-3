import type { BreakInput } from '../types'

interface Props {
  index: number
  breakWindow: BreakInput
  onChange: (next: BreakInput) => void
  onRemove: () => void
}

/** 一段经批准的不计薪休息：真实本地分钟时间的半开区间，可跨午夜（直接用次日时间表示）。 */
export function BreakEditor({ index, breakWindow, onChange, onRemove }: Props) {
  return (
    <fieldset className="rule-card break-card" data-testid={`break-row-${index}`}>
      <legend>休息 #{index + 1}（不计薪）</legend>
      <div className="rule-grid">
        <label className="field">
          <span>休息开始（本地时间）</span>
          <input
            type="datetime-local"
            value={breakWindow.start}
            required
            data-testid={`break-start-${index}`}
            onChange={(e) => e.target.value && onChange({ ...breakWindow, start: e.target.value })}
          />
        </label>

        <label className="field">
          <span>休息结束（不含该分钟，本地时间）</span>
          <input
            type="datetime-local"
            value={breakWindow.end}
            required
            data-testid={`break-end-${index}`}
            onChange={(e) => e.target.value && onChange({ ...breakWindow, end: e.target.value })}
          />
        </label>
      </div>

      <button type="button" className="danger" onClick={onRemove} data-testid={`break-remove-${index}`}>
        删除休息
      </button>
    </fieldset>
  )
}

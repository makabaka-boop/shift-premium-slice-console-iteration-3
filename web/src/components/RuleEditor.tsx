import type { RuleInput } from '../types'
import { WEEKDAY_LABELS, minuteToLabel, labelToMinute } from '../time'

interface Props {
  index: number
  rule: RuleInput
  onChange: (next: RuleInput) => void
  onRemove: () => void
}

export function RuleEditor({ index, rule, onChange, onRemove }: Props) {
  const endIs2400 = rule.endMinute === 1440

  const toggleWeekday = (d: number) => {
    const has = rule.weekdays.includes(d)
    onChange({
      ...rule,
      weekdays: has
        ? rule.weekdays.filter((x) => x !== d)
        : [...rule.weekdays, d].sort((a, b) => a - b),
    })
  }

  return (
    <fieldset className="rule-card" data-testid={`rule-row-${index}`}>
      <legend>规则 #{index + 1}</legend>
      <div className="rule-grid">
        <label className="field">
          <span>ID</span>
          <input
            type="text"
            value={rule.id}
            data-testid={`rule-id-${index}`}
            onChange={(e) => onChange({ ...rule, id: e.target.value })}
          />
        </label>

        <label className="field">
          <span>priority（越大越优先）</span>
          <input
            type="number"
            value={rule.priority}
            data-testid={`rule-priority-${index}`}
            onChange={(e) => onChange({ ...rule, priority: Number(e.target.value) })}
          />
        </label>

        <label className="field">
          <span>倍率 basisPoints（10000=1.0×）</span>
          <input
            type="number"
            min={1}
            value={rule.basisPoints}
            data-testid={`rule-bp-${index}`}
            onChange={(e) => onChange({ ...rule, basisPoints: Number(e.target.value) })}
          />
        </label>

        <div className="field">
          <span>星期</span>
          <div className="weekdays" role="group" aria-label="星期集合">
            {WEEKDAY_LABELS.map((label, d) => (
              <label key={d} className={`weekday ${rule.weekdays.includes(d) ? 'on' : ''}`}>
                <input
                  type="checkbox"
                  checked={rule.weekdays.includes(d)}
                  data-testid={`rule-weekday-${index}-${d}`}
                  onChange={() => toggleWeekday(d)}
                />
                {label}
              </label>
            ))}
          </div>
        </div>

        <label className="field">
          <span>窗口开始</span>
          <input
            type="time"
            value={minuteToLabel(rule.startMinute)}
            data-testid={`rule-start-${index}`}
            onChange={(e) =>
              e.target.value && onChange({ ...rule, startMinute: labelToMinute(e.target.value) })
            }
          />
        </label>

        <div className="field">
          <span>窗口结束（不含该分钟）</span>
          <div className="end-time">
            <input
              type="time"
              disabled={endIs2400}
              value={endIs2400 ? '00:00' : minuteToLabel(rule.endMinute)}
              data-testid={`rule-end-${index}`}
              onChange={(e) =>
                e.target.value && onChange({ ...rule, endMinute: labelToMinute(e.target.value) })
              }
            />
            <label className={`check ${endIs2400 ? 'on' : ''}`}>
              <input
                type="checkbox"
                checked={endIs2400}
                data-testid={`rule-end2400-${index}`}
                onChange={(e) => onChange({ ...rule, endMinute: e.target.checked ? 1440 : 1380 })}
              />
              24:00（到次日 00:00）
            </label>
          </div>
        </div>
      </div>

      <button type="button" className="danger" onClick={onRemove} data-testid={`rule-remove-${index}`}>
        删除规则
      </button>
    </fieldset>
  )
}

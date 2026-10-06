import type { ExceptionInput } from '../types'
import { minuteToLabel, labelToMinute } from '../time'

interface Props {
  index: number
  exception: ExceptionInput
  onChange: (next: ExceptionInput) => void
  onRemove: () => void
}

/** 把可能跨午夜的 endMinute 拆成「次日勾选 + 当天时刻」。 */
function endDisplay(endMinute: number): { nextDay: boolean; label: string } {
  if (endMinute >= 1440) return { nextDay: true, label: minuteToLabel(endMinute - 1440) }
  return { nextDay: false, label: minuteToLabel(endMinute) }
}

export function ExceptionEditor({ index, exception, onChange, onRemove }: Props) {
  const endView = endDisplay(exception.endMinute)

  return (
    <fieldset className="rule-card exc-card" data-testid={`exc-row-${index}`}>
      <legend>例外 #{index + 1}（仅作用于指定日期）</legend>
      <div className="rule-grid">
        <label className="field">
          <span>ID</span>
          <input
            type="text"
            value={exception.id}
            data-testid={`exc-id-${index}`}
            onChange={(e) => onChange({ ...exception, id: e.target.value })}
          />
        </label>

        <label className="field">
          <span>日期</span>
          <input
            type="date"
            value={exception.date}
            data-testid={`exc-date-${index}`}
            onChange={(e) => e.target.value && onChange({ ...exception, date: e.target.value })}
          />
        </label>

        <label className="field">
          <span>类型</span>
          <select
            value={exception.kind}
            data-testid={`exc-kind-${index}`}
            onChange={(e) => {
              const kind = e.target.value as ExceptionInput['kind']
              onChange({
                ...exception,
                kind,
                basisPoints: kind === 'SUPPRESS' ? null : exception.basisPoints ?? 12000,
              })
            }}
          >
            <option value="SUPPRESS">SUPPRESS（按基础倍率 1.0×，压过所有周规则）</option>
            <option value="REPLACE">REPLACE（采用指定倍率）</option>
          </select>
        </label>

        {exception.kind === 'REPLACE' && (
          <label className="field">
            <span>例外倍率 basisPoints（10000=1.0×）</span>
            <input
              type="number"
              min={1}
              value={exception.basisPoints ?? ''}
              data-testid={`exc-bp-${index}`}
              onChange={(e) =>
                onChange({ ...exception, basisPoints: Number(e.target.value) || null })
              }
            />
          </label>
        )}

        <label className="field">
          <span>窗口开始</span>
          <input
            type="time"
            value={minuteToLabel(exception.startMinute)}
            data-testid={`exc-start-${index}`}
            onChange={(e) =>
              e.target.value && onChange({ ...exception, startMinute: labelToMinute(e.target.value) })
            }
          />
        </label>

        <div className="field">
          <span>窗口结束（不含该分钟）</span>
          <div className="end-time">
            <input
              type="time"
              value={endView.label}
              data-testid={`exc-end-${index}`}
              onChange={(e) => {
                if (!e.target.value) return
                const base = labelToMinute(e.target.value)
                onChange({
                  ...exception,
                  endMinute: endView.nextDay ? 1440 + base : base,
                })
              }}
            />
            <label className={`check ${endView.nextDay ? 'on' : ''}`}>
              <input
                type="checkbox"
                checked={endView.nextDay}
                data-testid={`exc-end-nextday-${index}`}
                onChange={(e) => {
                  const tod = endView.nextDay ? exception.endMinute - 1440 : exception.endMinute
                  onChange({
                    ...exception,
                    // 勾到次日但时刻为 00:00 时给一个安全默认 00:30，避免空窗口
                    endMinute: e.target.checked ? 1440 + (tod === 0 ? 30 : tod) : Math.max(1, tod),
                  })
                }}
              />
              次日（跨午夜部分归入实际次日）
            </label>
          </div>
        </div>
      </div>

      <button type="button" className="danger" onClick={onRemove} data-testid={`exc-remove-${index}`}>
        删除例外
      </button>
    </fieldset>
  )
}

export const WEEKDAY_LABELS = ['一', '二', '三', '四', '五', '六', '日']

/** 分钟数 -> HH:MM（1440 返回 24:00，供展示用） */
export function minuteToLabel(m: number): string {
  const h = Math.floor(m / 60)
  const mi = m % 60
  return `${String(h).padStart(2, '0')}:${String(mi).padStart(2, '0')}`
}

/** HH:MM -> 分钟数 */
export function labelToMinute(label: string): number {
  const [h, m] = label.split(':').map(Number)
  return h * 60 + m
}

export function describeWeekdays(days: number[]): string {
  if (days.length === 7) return '每天'
  const sorted = [...days].sort((a, b) => a - b)
  const isWorkday = sorted.length === 5 && [0, 1, 2, 3, 4].every((d) => sorted.includes(d))
  if (isWorkday) return '周一至周五'
  const isWeekend = sorted.length === 2 && [5, 6].every((d) => sorted.includes(d))
  if (isWeekend) return '周六、周日'
  return sorted.map((d) => `周${WEEKDAY_LABELS[d]}`).join('、')
}

/** 窗口是否跨午夜 */
export function crossesMidnight(start: number, end: number): boolean {
  return end < start
}

export function weekdayName(w: number): string {
  return `周${WEEKDAY_LABELS[w]}`
}

/** 取本地 datetime-local 需要的格式（无时区后缀） */
export function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export interface RuleInput {
  id: string
  weekdays: number[] // 0=周一 ... 6=周日
  startMinute: number // 0..1439
  endMinute: number // 1..1440（1440=24:00）
  priority: number
  basisPoints: number
}

export type ExceptionKind = 'SUPPRESS' | 'REPLACE'

export interface ExceptionInput {
  id: string
  date: string // YYYY-MM-DD，锚定的具体日期
  startMinute: number // 0..1439
  endMinute: number // > startMinute；可超过 1440，超出部分进入真实次日（上限 2880）
  kind: ExceptionKind
  // SUPPRESS 时为 null（按基础倍率）；REPLACE 必须给出正整数倍率
  basisPoints: number | null
}

export interface BreakInput {
  start: string // 真实本地分钟时间 datetime-local（YYYY-MM-DDTHH:MM）
  end: string // 半开：不含 end 这一分钟；必须整体落在值班区间内
}

export interface ShiftPayload {
  start: string
  end: string
  baseRateCents: number
  rules: RuleInput[]
  exceptions?: ExceptionInput[]
  breaks?: BreakInput[]
}

export interface FractionJson {
  numerator: number
  denominator: number
  decimal: string
}

export interface SegmentJson {
  start: string
  end: string
  minutes: number
  weekday: number
  ruleId: string | null
  basisPoints: number
  pay: FractionJson
  // 非 null 表示该分钟由一次性例外接管；ruleId 仍记录被压过的周规则（仅解释用）
  exceptionId: string | null
  exceptionKind: ExceptionKind | null
  // true 表示该段为经批准的不计薪休息：pay 为 0，其余字段保留原本命中的规则/例外
  isBreak: boolean
}

export interface CalculateResponse {
  segments: SegmentJson[]
  total: FractionJson
  totalMinutes: number
  paidMinutes: number
  breakMinutes: number
}

export interface ValidationIssue {
  loc: (string | number)[]
  msg: string
}

import type { RuleInput } from './types'

/** 预置：跨周五到周六的典型三规则场景，演示一次录入即可看到夜班/周末/专项。 */
export function defaultStart(): string {
  // 2026-09-25（周五）22:00 本地
  return '2026-09-25T22:00'
}

export function defaultEnd(): string {
  return '2026-09-26T02:00'
}

export const DEFAULT_RULES: RuleInput[] = [
  {
    id: 'night',
    weekdays: [0, 1, 2, 3, 4, 5, 6],
    startMinute: 22 * 60,
    endMinute: 6 * 60,
    priority: 1,
    basisPoints: 12000,
  },
  {
    id: 'weekend',
    weekdays: [5, 6],
    startMinute: 0,
    endMinute: 1440,
    priority: 2,
    basisPoints: 15000,
  },
  {
    id: 'special',
    weekdays: [4],
    startMinute: 23 * 60,
    endMinute: 1440,
    priority: 3,
    basisPoints: 20000,
  },
]

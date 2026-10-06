import type { CalculateResponse, ShiftPayload, ValidationIssue } from './types'

export class ApiError extends Error {
  status: number
  issues: ValidationIssue[]

  constructor(status: number, message: string, issues: ValidationIssue[]) {
    super(message)
    this.status = status
    this.issues = issues
  }
}

export async function calculate(payload: ShiftPayload): Promise<CalculateResponse> {
  const resp = await fetch('/api/calculate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!resp.ok) {
    let issues: ValidationIssue[] = []
    let message = `请求失败（${resp.status}）`
    try {
      const body = await resp.json()
      if (Array.isArray(body.detail)) {
        issues = body.detail.map((d: { loc: (string | number)[]; msg: string }) => ({
          loc: d.loc,
          msg: d.msg,
        }))
        message = '输入校验未通过'
      } else if (typeof body.detail === 'string') {
        message = body.detail
      }
    } catch {
      // 非 JSON 错误体，保留默认消息
    }
    throw new ApiError(resp.status, message, issues)
  }
  return resp.json()
}

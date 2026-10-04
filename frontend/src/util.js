// 转让/倒计时展示工具：墙卡、详情、我的认领共用同一口径。

export function remainMs(expiresAt, now = Date.now()) {
  if (!expiresAt) return null
  const t = Date.parse(expiresAt)
  if (Number.isNaN(t)) return null
  return Math.max(0, t - now)
}

export function fmtCountdown(expiresAt, now = Date.now()) {
  const ms = remainMs(expiresAt, now)
  if (ms === null) return '—'
  let s = Math.floor(ms / 1000)
  const d = Math.floor(s / 86400); s -= d * 86400
  const h = Math.floor(s / 3600); s -= h * 3600
  const m = Math.floor(s / 60); s -= m * 60
  const pad = (n) => String(n).padStart(2, '0')
  if (d > 0) return `${d}天 ${pad(h)}:${pad(m)}:${pad(s)}`
  return `${pad(h)}:${pad(m)}:${pad(s)}`
}

// 后端错误码 -> 中文文案
export const ERR_TEXT = {
  locked: '该愿望已被认领且未到期',
  pending_transfer: '转让托管中：存在未确认转让单，他人暂不能认领',
  invalid_target: '目标人非法：不能为空，也不能与当前认领人相同',
  not_owner: '只有当前认领人能发起或撤销转让',
  not_target: '只有目标人能确认转让',
  not_claimed: '愿望当前不是认领状态',
  wish_not_claimed: '愿望已非认领状态，转让无法确认',
  transfer_not_pending: '转让单已处理或已撤销',
  already_fulfilled: '愿望已核销',
  need_claim: '需先认领才能核销',
}

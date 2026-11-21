// 通用格式化工具：相对时间、时长、文件大小、JSON 美化等（使用原生 Date，零额外依赖）

/** 解析为 Date，非法返回 null */
function toDate(value) {
  if (value === null || value === undefined || value === '') return null
  const d = value instanceof Date ? value : new Date(value)
  return Number.isNaN(d.getTime()) ? null : d
}

const pad2 = (n) => String(n).padStart(2, '0')

/**
 * 相对时间显示：今天显示 HH:mm，昨天显示“昨天”，更早显示“N天前”
 * @param {string|number|Date} value
 * @returns {string}
 */
export function relativeTime(value) {
  const d = toDate(value)
  if (!d) return ''
  const now = new Date()
  const startOfDay = (dt) => new Date(dt.getFullYear(), dt.getMonth(), dt.getDate())
  const diffDay = Math.round((startOfDay(now) - startOfDay(d)) / 86400000)
  if (diffDay <= 0) return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
  if (diffDay === 1) return '昨天'
  if (diffDay === 2) return '前天'
  return `${diffDay}天前`
}

/** 完整日期时间：YYYY-MM-DD HH:mm:ss */
export function formatDateTime(value) {
  const d = toDate(value)
  if (!d) return '-'
  return (
    `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
    `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
  )
}

/** 仅 HH:mm */
export function formatTime(value) {
  const d = toDate(value)
  if (!d) return ''
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}

/** HH:mm:ss */
export function formatClockTime(value) {
  const d = toDate(value)
  if (!d) return '-'
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
}

/**
 * 毫秒时长转中文：如 3分12秒 / 1.2秒 / 320ms
 * @param {number} ms
 */
export function formatDuration(ms) {
  if (ms === null || ms === undefined || Number.isNaN(Number(ms))) return '-'
  const n = Number(ms)
  if (n < 1000) return `${Math.round(n)}ms`
  const totalSec = Math.round(n / 1000)
  if (totalSec < 60) return `${(n / 1000).toFixed(1)}秒`
  const min = Math.floor(totalSec / 60)
  const sec = totalSec % 60
  if (min < 60) return `${min}分${sec}秒`
  const hour = Math.floor(min / 60)
  return `${hour}小时${min % 60}分`
}

/** 秒表格式化：mm:ss（用于会话计时） */
export function formatClock(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds))
  const min = String(Math.floor(s / 60)).padStart(2, '0')
  const sec = String(s % 60).padStart(2, '0')
  return `${min}:${sec}`
}

/** 字节大小 */
export function formatBytes(bytes) {
  if (bytes === null || bytes === undefined || bytes === '') return '-'
  const units = ['B', 'KB', 'MB', 'GB']
  let i = 0
  let v = Number(bytes)
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i += 1
  }
  return `${v.toFixed(i === 0 ? 0 : 1)}${units[i]}`
}

/** JSON 美化输出 */
export function prettyJson(value) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'string') {
    try {
      return JSON.stringify(JSON.parse(value), null, 2)
    } catch {
      return value
    }
  }
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

/** 会话模式中文名 */
export function modeLabel(mode) {
  return (
    { bot: 'AI模式', waiting_human: '等待人工', human: '人工服务中' }[mode] ||
    mode ||
    'AI模式'
  )
}

/** 会话结束原因中文名 */
export function endReasonLabel(reason) {
  return { resolved: '已解决', user_closed: '用户关闭', unresolved: '未解决' }[reason] || reason || ''
}

/** 情绪 → el-tag 类型 */
export function emotionTagType(emotion) {
  return { positive: 'success', neutral: 'info', negative: 'danger' }[emotion] || 'info'
}

/** 情绪中文名 */
export function emotionLabel(emotion) {
  return { positive: '积极', neutral: '中性', negative: '消极' }[emotion] || '未知'
}

/** 模态中文名 */
export function modalityLabel(modality) {
  return { text: '文本', image: '图像', voice: '语音', table: '表格' }[modality] || modality || '文本'
}

/** 工单状态中文名 */
export function ticketStatusLabel(status) {
  return (
    { open: '待处理', in_progress: '处理中', resolved: '已解决', closed: '已关闭' }[status] ||
    status ||
    '-'
  )
}

/** 工单状态 → el-tag 类型 */
export function ticketStatusTagType(status) {
  return (
    { open: 'warning', in_progress: 'primary', resolved: 'success', closed: 'info' }[status] || 'info'
  )
}

/** 工单优先级中文名 */
export function priorityLabel(priority) {
  return { high: '高', medium: '中', low: '低' }[priority] || priority || '-'
}

/** 工单优先级 → el-tag 类型 */
export function priorityTagType(priority) {
  return { high: 'danger', medium: 'warning', low: 'info' }[priority] || 'info'
}

/** 工单分类中文名（与知识库分类枚举一致） */
export function ticketCategoryLabel(category) {
  return (
    {
      network: '网络/VPN',
      password: '密码/账号',
      software: '软件',
      finance: '财务/报销',
      email: '邮箱',
      general: '综合'
    }[category] ||
    category ||
    '-'
  )
}

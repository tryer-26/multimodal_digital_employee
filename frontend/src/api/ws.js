// WebSocket 封装：按角色连接 /ws/user | /ws/agent | /ws/admin，支持自动重连与事件分发
import { TOKEN_KEY } from './http'

// 服务端同账号互踢的关闭码（见 core/infra/ws_manager.py）：
// 收到后本端停止自动重连并提示，避免「旧端被踢→重连→踢掉新端」的互踢死循环
const WS_CLOSE_KICKED = 4409

// 连接建立中暂存消息的上限，防止长时间连不上时无界增长
const MAX_PENDING = 20

/**
 * 构建 WebSocket 地址。
 * 开发环境下 Vite devServer 已把 /ws 代理到后端（ws: true），
 * 因此这里直接使用当前页面的 host 即可（与 HTTP 同源）。
 * @param {'user'|'agent'|'admin'} role 角色
 * @param {string} token 登录令牌
 * @returns {string} ws 地址
 */
export function buildWsUrl(role, token) {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host = window.location.host
  return `${protocol}//${host}/ws/${role}?token=${encodeURIComponent(token)}`
}

/**
 * WebSocket 客户端
 * 用法：
 *   const ws = new WSClient('user')
 *   ws.on('bot_reply', (msg) => {...})
 *   ws.on('open', () => {...})
 *   ws.connect()
 *   ws.send({ type: 'user_message', ... })
 *   ws.close()
 */
export class WSClient {
  /**
   * @param {'user'|'agent'|'admin'} role
   * @param {{ autoReconnect?: boolean, maxRetry?: number }} options
   */
  constructor(role, options = {}) {
    this.role = role
    this.autoReconnect = options.autoReconnect !== false
    this.maxRetry = options.maxRetry ?? 10
    this.retry = 0
    this.ws = null
    this.manualClosed = false
    this.kicked = false // 被同账号在别处登录踢下线（4409），此后不再自动重连
    this.pending = [] // 连接建立中暂存的消息，onopen 后按序补发
    this.handlers = {} // type -> Set<fn>
  }

  /** 注册事件处理；type 可为消息 type，或 'open'/'close'/'error'/'message' */
  on(type, handler) {
    if (!this.handlers[type]) this.handlers[type] = new Set()
    this.handlers[type].add(handler)
    return this
  }

  /** 移除事件处理 */
  off(type, handler) {
    this.handlers[type]?.delete(handler)
    return this
  }

  /** 触发事件 */
  emit(type, payload) {
    this.handlers[type]?.forEach((fn) => {
      try {
        fn(payload)
      } catch (e) {
        // 单个处理函数异常不影响其它订阅者
        console.error('[WS] handler error:', e)
      }
    })
  }

  /** 建立连接 */
  connect() {
    const token = localStorage.getItem(TOKEN_KEY)
    if (!token) {
      console.warn('[WS] 缺少 token，取消连接')
      return
    }
    this.manualClosed = false
    this.kicked = false
    try {
      this.ws = new WebSocket(buildWsUrl(this.role, token))
    } catch (e) {
      console.error('[WS] 创建连接失败:', e)
      this.scheduleReconnect()
      return
    }

    this.ws.onopen = () => {
      this.retry = 0
      this.flushPending()
      this.emit('open')
    }

    this.ws.onmessage = (event) => {
      let msg
      try {
        msg = JSON.parse(event.data)
      } catch {
        msg = { type: 'raw', data: event.data }
      }
      // 先按消息类型分发，再统一分发
      if (msg?.type) this.emit(msg.type, msg)
      this.emit('message', msg)
    }

    this.ws.onerror = (err) => {
      this.emit('error', err)
    }

    this.ws.onclose = (ev) => {
      this.emit('close', ev)
      // 同账号在别处登录：本连接被服务端踢下线，停止自动重连（否则会与新端互踢死循环）
      if (ev.code === WS_CLOSE_KICKED) {
        this.kicked = true
        this.pending = []
        this.emit('kicked', ev)
        return
      }
      if (!this.manualClosed) this.scheduleReconnect()
    }
  }

  /** 断线重连（指数退避，上限 10s） */
  scheduleReconnect() {
    if (!this.autoReconnect || this.manualClosed || this.kicked) return
    if (this.retry >= this.maxRetry) {
      console.warn('[WS] 达到最大重连次数，停止重连')
      return
    }
    this.retry += 1
    const delay = Math.min(1000 * 2 ** (this.retry - 1), 10000)
    setTimeout(() => {
      if (!this.manualClosed) this.connect()
    }, delay)
  }

  /** 发送 JSON 消息；连接建立中则暂存，onopen 后按序补发 */
  send(payload) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(payload))
      return true
    }
    // 握手未完成（页面初始化 / 重连窗口）：暂存待补发，避免消息被静默丢弃
    if (this.ws && this.ws.readyState === WebSocket.CONNECTING) {
      if (this.pending.length >= MAX_PENDING) this.pending.shift()
      this.pending.push(payload)
      return true
    }
    console.warn('[WS] 连接未就绪，消息未发送:', payload)
    return false
  }

  /** 连接建立后按序补发暂存消息 */
  flushPending() {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return
    const items = this.pending
    this.pending = []
    items.forEach((payload) => this.ws.send(JSON.stringify(payload)))
  }

  /** 主动关闭（不再重连） */
  close() {
    this.manualClosed = true
    this.pending = []
    if (this.ws) {
      this.ws.close()
      this.ws = null
    }
  }
}

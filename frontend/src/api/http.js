// axios 实例：统一 baseURL、注入 Token、处理 401
import axios from 'axios'
import { ElMessage } from 'element-plus'

// localStorage 中保存 token / 用户信息的 key（与 store/user.js 保持一致）
export const TOKEN_KEY = 'mde_token'
export const USER_KEY = 'mde_user'

/**
 * 构造可直接用于 <img>/<audio> 等标签的媒体地址。
 * 这些标签无法携带 Authorization 头，而后端 /api/media 需要鉴权，
 * 故按中间件约定用 query 传 token；非媒体地址或已带 token 时原样返回。
 * 注意：发给后端的 image_url 必须保持原样（后端按 “/” 取最后一段作 asset_id）。
 */
export function mediaUrl(url) {
  if (!url || typeof url !== 'string') return ''
  if (!url.startsWith('/api/media/')) return url
  if (/[?&]token=/.test(url)) return url
  const token = localStorage.getItem(TOKEN_KEY)
  if (!token) return url
  return `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`
}

const http = axios.create({
  baseURL: '/api',
  timeout: 60000
})

// 请求拦截：注入 Authorization: Bearer <token>
http.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem(TOKEN_KEY)
    if (token) {
      config.headers = config.headers || {}
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error)
)

// 响应拦截：401 清理登录态并跳转登录页，其它错误统一提示
http.interceptors.response.use(
  (response) => response.data,
  (error) => {
    const status = error?.response?.status
    if (status === 401) {
      localStorage.removeItem(TOKEN_KEY)
      localStorage.removeItem(USER_KEY)
      const redirect = encodeURIComponent(
        window.location.pathname + window.location.search
      )
      // 避免在登录页重复跳转
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = `/login?redirect=${redirect}`
      }
      ElMessage.error('登录已失效，请重新登录')
    } else {
      const detail =
        error?.response?.data?.detail ||
        error?.response?.data?.message ||
        error?.message ||
        '请求失败'
      ElMessage.error(detail)
    }
    return Promise.reject(error)
  }
)

export default http

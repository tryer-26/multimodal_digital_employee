// 用户状态：token / 用户信息 / 角色（localStorage 持久化）
import { defineStore } from 'pinia'
import http, { TOKEN_KEY, USER_KEY } from '@/api/http'

export const useUserStore = defineStore('user', {
  state: () => ({
    token: localStorage.getItem(TOKEN_KEY) || '',
    user: JSON.parse(localStorage.getItem(USER_KEY) || 'null')
  }),

  getters: {
    isLoggedIn: (state) => !!state.token && !!state.user,
    role: (state) => state.user?.role || '',
    displayName: (state) => state.user?.display_name || state.user?.username || '',
    userId: (state) => state.user?.id || state.user?.user_id || ''
  },

  actions: {
    /** 调用后端登录接口 */
    async login(username, password) {
      const data = await http.post('/auth/login', { username, password })
      this.token = data.token
      this.user = {
        id: data.user_id,
        user_id: data.user_id,
        username: data.username,
        role: data.role,
        display_name: data.display_name
      }
      localStorage.setItem(TOKEN_KEY, this.token)
      localStorage.setItem(USER_KEY, JSON.stringify(this.user))
      return this.user
    },

    /** 登出：调用后端并清理本地态 */
    async logout() {
      try {
        await http.post('/auth/logout')
      } catch {
        // 忽略登出接口异常，仍然清理本地
      }
      this.clear()
    },

    /** 清理本地登录态 */
    clear() {
      this.token = ''
      this.user = null
      localStorage.removeItem(TOKEN_KEY)
      localStorage.removeItem(USER_KEY)
    },

    /** 读取角色对应入口 */
    homeRoute() {
      const map = { user: '/chat', agent: '/agent', admin: '/admin' }
      return map[this.role] || '/login'
    }
  }
})

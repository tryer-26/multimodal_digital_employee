// 路由表 + 权限守卫（meta.roles），设计见文档 12.6
import { createRouter, createWebHistory } from 'vue-router'
import { useUserStore } from '@/store/user'

// 布局与页面按需导入
import UserLayout from '@/layouts/UserLayout.vue'
import AgentLayout from '@/layouts/AgentLayout.vue'
import AdminLayout from '@/layouts/AdminLayout.vue'

const routes = [
  { path: '/', redirect: '/login' },
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/Login.vue'),
    meta: { public: true }
  },
  {
    path: '/chat',
    component: UserLayout,
    meta: { roles: ['user'] },
    children: [
      {
        path: '',
        name: 'chat',
        component: () => import('@/views/Chat.vue')
      }
    ]
  },
  {
    path: '/agent',
    component: AgentLayout,
    meta: { roles: ['agent'] },
    children: [
      {
        path: '',
        name: 'agent',
        component: () => import('@/views/AgentConsole.vue')
      }
    ]
  },
  {
    path: '/admin',
    component: AdminLayout,
    redirect: '/admin/dashboard',
    meta: { roles: ['admin'] },
    children: [
      {
        path: 'dashboard',
        name: 'admin-dashboard',
        component: () => import('@/views/admin/Dashboard.vue'),
        meta: { title: '数据看板' }
      },
      {
        path: 'knowledge',
        name: 'admin-knowledge',
        component: () => import('@/views/admin/Knowledge.vue'),
        meta: { title: '知识库管理' }
      },
      {
        path: 'tickets',
        name: 'admin-tickets',
        component: () => import('@/views/admin/Tickets.vue'),
        meta: { title: '工单管理' }
      },
      {
        path: 'logs',
        name: 'admin-logs',
        component: () => import('@/views/admin/Logs.vue'),
        meta: { title: '对话日志' }
      },
      {
        path: 'tools',
        name: 'admin-tools',
        component: () => import('@/views/admin/ToolLogs.vue'),
        meta: { title: '工具调用日志' }
      },
      {
        path: 'eval',
        name: 'admin-eval',
        component: () => import('@/views/admin/Eval.vue'),
        meta: { title: '全栈评测' }
      },
      {
        path: 'users',
        name: 'admin-users',
        component: () => import('@/views/admin/Users.vue'),
        meta: { title: '用户管理' }
      }
    ]
  },
  // 兜底：未匹配路由回登录页
  { path: '/:pathMatch(.*)*', redirect: '/login' }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

// 全局前置守卫：未登录跳登录页，角色不匹配跳登录页
router.beforeEach((to) => {
  const store = useUserStore()

  // 公开路由直接放行；已登录访问登录页则导向各自首页
  if (to.meta.public) {
    if (store.isLoggedIn) return store.homeRoute()
    return true
  }

  // 未登录 → 登录页（带 redirect）
  if (!store.isLoggedIn) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }

  // 合并所有匹配记录的 roles 进行校验
  const roles = to.matched
    .map((r) => r.meta?.roles)
    .filter(Boolean)
    .flat()
  if (roles.length && !roles.includes(store.role)) {
    return { path: '/login' }
  }

  return true
})

export default router

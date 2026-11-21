<script setup>
// 管理端布局：左侧导航 + 顶部栏 + 内容区
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import {
  DataAnalysis,
  Collection,
  Document,
  Tickets,
  User,
  Tools,
  Histogram
} from '@element-plus/icons-vue'
import { useUserStore } from '@/store/user'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

// 左侧导航菜单
const menus = [
  { path: '/admin/dashboard', title: '数据看板', icon: DataAnalysis },
  { path: '/admin/knowledge', title: '知识库管理', icon: Collection },
  { path: '/admin/tickets', title: '工单管理', icon: Tickets },
  { path: '/admin/logs', title: '对话日志', icon: Document },
  { path: '/admin/tools', title: '工具调用日志', icon: Tools },
  { path: '/admin/eval', title: '全栈评测', icon: Histogram },
  { path: '/admin/users', title: '用户管理', icon: User }
]

// 当前激活菜单（按路径前缀匹配）
const activeMenu = computed(() => {
  const hit = menus.find((m) => route.path.startsWith(m.path))
  return hit ? hit.path : '/admin/dashboard'
})

async function handleLogout() {
  try {
    await ElMessageBox.confirm('确定要退出登录吗？', '提示', {
      confirmButtonText: '退出',
      cancelButtonText: '取消',
      type: 'warning'
    })
  } catch {
    return
  }
  await userStore.logout()
  router.push('/login')
}
</script>

<template>
  <div class="admin-layout">
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-logo">MDE</span>
        <span class="brand-title">管理后台</span>
      </div>
      <el-menu
        :default-active="activeMenu"
        router
        class="side-menu"
        background-color="#1f2d3d"
        text-color="#c0c4cc"
        active-text-color="#ffffff"
      >
        <el-menu-item v-for="m in menus" :key="m.path" :index="m.path">
          <el-icon><component :is="m.icon" /></el-icon>
          <span>{{ m.title }}</span>
        </el-menu-item>
      </el-menu>
    </aside>

    <div class="main-wrap">
      <header class="app-header">
        <div class="page-title">{{ route.meta.title || '管理后台' }}</div>
        <div class="header-right">
          <el-tag type="warning" effect="plain" size="small">系统管理员</el-tag>
          <span class="user-name">{{ userStore.displayName }}</span>
          <el-button link type="primary" @click="handleLogout">退出</el-button>
        </div>
      </header>
      <main class="app-main">
        <router-view />
      </main>
    </div>
  </div>
</template>

<style scoped>
.admin-layout {
  height: 100vh;
  display: flex;
  overflow: hidden;
}

.sidebar {
  width: 220px;
  flex-shrink: 0;
  background: var(--mde-bg-nav);
  display: flex;
  flex-direction: column;
}

.brand {
  height: 56px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 20px;
  color: #fff;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.brand-logo {
  background: var(--mde-primary);
  font-weight: 700;
  padding: 4px 8px;
  border-radius: 6px;
  letter-spacing: 1px;
  font-size: 14px;
}

.brand-title {
  font-size: 16px;
  font-weight: 600;
}

.side-menu {
  border-right: none;
  flex: 1;
}

.main-wrap {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.app-header {
  height: 56px;
  flex-shrink: 0;
  background: var(--mde-bg-card);
  border-bottom: 1px solid var(--mde-border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
}

.page-title {
  font-size: 16px;
  font-weight: 600;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.user-name {
  font-size: 14px;
  color: var(--mde-text-regular);
}

.app-main {
  flex: 1;
  min-height: 0;
  overflow: auto;
  background: var(--mde-bg-page);
}
</style>

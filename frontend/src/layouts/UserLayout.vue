<script setup>
// 用户端布局：顶部品牌栏 + 内容区（路由出口）
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { useUserStore } from '@/store/user'

const router = useRouter()
const userStore = useUserStore()

// 退出登录：二次确认后清理并回登录页
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
  <div class="user-layout">
    <header class="app-header">
      <div class="brand">
        <span class="brand-logo">MDE</span>
        <span class="brand-title">MDE-LAB 多模态数字员工平台</span>
      </div>
      <div class="header-right">
        <el-tag type="info" effect="plain" size="small">员工端</el-tag>
        <span class="user-name">{{ userStore.displayName }}</span>
        <el-button link type="primary" @click="handleLogout">退出</el-button>
      </div>
    </header>
    <main class="app-main">
      <router-view />
    </main>
  </div>
</template>

<style scoped>
.user-layout {
  height: 100vh;
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
  box-shadow: var(--mde-shadow);
  z-index: 10;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
}

.brand-logo {
  background: var(--mde-primary);
  color: #fff;
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
  overflow: hidden;
}
</style>

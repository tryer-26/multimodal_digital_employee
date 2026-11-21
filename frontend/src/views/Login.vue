<script setup>
// 登录页：居中登录卡片 + 演示账号列表（文档 12.2）
import { ref, reactive } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { User, Lock } from '@element-plus/icons-vue'
import { useUserStore } from '@/store/user'

const router = useRouter()
const route = useRoute()
const userStore = useUserStore()

const formRef = ref(null)
const loading = ref(false)
const form = reactive({ username: '', password: '' })

const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }]
}

// 演示账号（密码统一 123456）
const demoAccounts = [
  { role: '用户', username: 'user01', password: '123456' },
  { role: '坐席', username: 'agent01', password: '123456' },
  { role: '管理员', username: 'admin', password: '123456' }
]

// 点击演示账号快速填充
function fillAccount(acc) {
  form.username = acc.username
  form.password = acc.password
}

async function handleLogin() {
  if (!formRef.value) return
  try {
    await formRef.value.validate()
  } catch {
    return
  }
  loading.value = true
  try {
    const user = await userStore.login(form.username.trim(), form.password)
    ElMessage.success(`欢迎回来，${user.display_name || user.username}`)
    // 角色对应入口；若存在 redirect 且角色匹配则优先跳转
    const redirect = route.query.redirect
    const home = userStore.homeRoute()
    router.push(typeof redirect === 'string' && redirect ? redirect : home)
  } catch {
    // 错误提示已在 axios 拦截器统一处理
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-head">
        <div class="logo">MDE</div>
        <h1 class="title">MDE-LAB 多模态数字员工平台</h1>
        <p class="subtitle">教学实训平台</p>
      </div>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        size="large"
        @keyup.enter="handleLogin"
      >
        <el-form-item prop="username">
          <el-input v-model="form.username" placeholder="用户名" :prefix-icon="User" clearable />
        </el-form-item>
        <el-form-item prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="密码"
            :prefix-icon="Lock"
            show-password
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" class="login-btn" :loading="loading" @click="handleLogin">
            登 录
          </el-button>
        </el-form-item>
      </el-form>

      <div class="demo-accounts">
        <div class="demo-title">演示账号（点击填充）</div>
        <div
          v-for="acc in demoAccounts"
          :key="acc.username"
          class="demo-item"
          @click="fillAccount(acc)"
        >
          <span class="demo-role">{{ acc.role }}</span>
          <span class="demo-cred">{{ acc.username }} / {{ acc.password }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #1f2d3d 0%, #2f6bff 100%);
}

.login-card {
  width: 400px;
  background: #fff;
  border-radius: 12px;
  padding: 36px 32px 28px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.2);
}

.login-head {
  text-align: center;
  margin-bottom: 26px;
}

.logo {
  display: inline-block;
  background: var(--mde-primary);
  color: #fff;
  font-weight: 700;
  font-size: 20px;
  letter-spacing: 2px;
  padding: 8px 16px;
  border-radius: 10px;
  margin-bottom: 14px;
}

.title {
  font-size: 18px;
  font-weight: 700;
  margin: 0 0 6px;
  color: var(--mde-text-primary);
}

.subtitle {
  margin: 0;
  font-size: 13px;
  color: var(--mde-text-secondary);
  letter-spacing: 2px;
}

.login-btn {
  width: 100%;
  letter-spacing: 4px;
}

.demo-accounts {
  margin-top: 8px;
  padding-top: 16px;
  border-top: 1px dashed var(--mde-border);
}

.demo-title {
  font-size: 12px;
  color: var(--mde-text-secondary);
  margin-bottom: 8px;
}

.demo-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 8px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  transition: background 0.15s;
}

.demo-item:hover {
  background: var(--mde-primary-light);
}

.demo-role {
  color: var(--mde-text-secondary);
}

.demo-cred {
  color: var(--mde-text-regular);
  font-family: 'Consolas', monospace;
}
</style>

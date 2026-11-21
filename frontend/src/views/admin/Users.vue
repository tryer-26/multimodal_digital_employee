<script setup>
// 管理端 - 用户管理（文档 12.5.5）
// 用户表格（含删除）+ 新建用户对话框 + 编辑抽屉（含修改密码）
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, RefreshRight } from '@element-plus/icons-vue'
import http from '@/api/http'
import { formatDateTime } from '@/utils/format'

const users = ref([])
const loading = ref(false)

const roleOptions = [
  { value: 'user', label: '用户' },
  { value: 'agent', label: '坐席' },
  { value: 'admin', label: '管理员' }
]
const roleLabel = (v) => roleOptions.find((r) => r.value === v)?.label || v
const roleTagType = (v) => ({ user: 'primary', agent: 'success', admin: 'warning' }[v] || 'info')

const statusOptions = [
  { value: 'active', label: '正常' },
  { value: 'disabled', label: '禁用' }
]
const statusLabel = (v) => statusOptions.find((s) => s.value === v)?.label || v || '正常'

const agentStatusLabel = (v) => ({ online: '在线', busy: '忙碌', offline: '离线' }[v] || v)

async function loadUsers() {
  loading.value = true
  try {
    const list = await http.get('/admin/users')
    users.value = Array.isArray(list) ? list : []
  } finally {
    loading.value = false
  }
}

/* ---------------- 新建用户 ---------------- */
const createVisible = ref(false)
const creating = ref(false)
const createFormRef = ref(null)
const createForm = reactive({
  username: '',
  password: '',
  display_name: '',
  role: 'user',
  department: ''
})

const createRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入初始密码', trigger: 'blur' }],
  role: [{ required: true, message: '请选择角色', trigger: 'change' }]
}

function openCreate() {
  Object.assign(createForm, {
    username: '',
    password: '',
    display_name: '',
    role: 'user',
    department: ''
  })
  createVisible.value = true
}

async function submitCreate() {
  try {
    await createFormRef.value?.validate()
  } catch {
    return
  }
  creating.value = true
  try {
    await http.post('/admin/users', { ...createForm })
    ElMessage.success('用户创建成功')
    createVisible.value = false
    loadUsers()
  } finally {
    creating.value = false
  }
}

/* ---------------- 编辑用户 ---------------- */
const editVisible = ref(false)
const saving = ref(false)
const changingPwd = ref(false)
const editForm = reactive({
  id: '',
  username: '',
  display_name: '',
  role: 'user',
  status: 'active',
  department: ''
})

function openEdit(row) {
  Object.assign(editForm, {
    id: row.id,
    username: row.username,
    display_name: row.display_name || '',
    role: row.role,
    status: row.status || 'active',
    department: row.department || ''
  })
  editVisible.value = true
}

async function saveEdit() {
  saving.value = true
  try {
    await http.put(`/admin/users/${editForm.id}`, {
      display_name: editForm.display_name,
      role: editForm.role,
      status: editForm.status,
      department: editForm.department
    })
    ElMessage.success('保存成功')
    editVisible.value = false
    loadUsers()
  } finally {
    saving.value = false
  }
}

async function changePassword() {
  let value
  try {
    ({ value } = await ElMessageBox.prompt(
      `请输入用户「${editForm.username}」的新密码（至少 6 位）`,
      '修改密码',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        inputType: 'password',
        inputPattern: /^.{6,}$/,
        inputErrorMessage: '密码长度至少 6 位'
      }
    ))
  } catch {
    return
  }
  changingPwd.value = true
  try {
    await http.put(`/admin/users/${editForm.id}/password`, { password: value })
    ElMessage.success('密码已修改')
  } catch {
    // 错误提示由 http 拦截器统一处理
  } finally {
    changingPwd.value = false
  }
}

/* ---------------- 删除用户 ---------------- */
async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(
      `确认删除用户「${row.display_name || row.username}」？其会话、消息与工具调用记录将一并删除，且不可恢复。`,
      '删除用户',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return
  }
  try {
    await http.delete(`/admin/users/${row.id}`)
    ElMessage.success('用户已删除')
    loadUsers()
  } catch {
    // 错误提示由 http 拦截器统一处理
  }
}

onMounted(loadUsers)
</script>

<template>
  <div class="users-page">
    <el-card shadow="never" class="section-card">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">用户管理</span>
          <div class="header-actions">
            <el-button size="small" :icon="Plus" type="primary" @click="openCreate">
              新建用户
            </el-button>
            <el-button size="small" :icon="RefreshRight" @click="loadUsers">刷新</el-button>
          </div>
        </div>
      </template>

      <el-table v-loading="loading" :data="users" size="small" empty-text="暂无用户">
        <el-table-column prop="username" label="用户名" width="140" />
        <el-table-column prop="display_name" label="姓名" width="160" />
        <el-table-column label="角色" width="110">
          <template #default="{ row }">
            <el-tag :type="roleTagType(row.role)" size="small" effect="plain">
              {{ roleLabel(row.role) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag
              :type="row.status === 'disabled' ? 'danger' : 'success'"
              size="small"
            >
              {{ statusLabel(row.status) }}
            </el-tag>
            <el-tag
              v-if="row.role === 'agent' && row.agent_status"
              size="small"
              effect="plain"
              class="agent-status"
            >
              {{ agentStatusLabel(row.agent_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="department" label="部门" width="140">
          <template #default="{ row }">{{ row.department || '-' }}</template>
        </el-table-column>
        <el-table-column label="最后登录" width="180">
          <template #default="{ row }">{{ formatDateTime(row.last_login_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
            <el-button
              link
              type="danger"
              size="small"
              :disabled="row.protected"
              :title="row.protected ? '默认账号不可删除' : '删除用户'"
              @click="handleDelete(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新建用户 -->
    <el-dialog v-model="createVisible" title="新建用户" width="480px">
      <el-form ref="createFormRef" :model="createForm" :rules="createRules" label-width="90px">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="createForm.username" placeholder="登录用户名" />
        </el-form-item>
        <el-form-item label="初始密码" prop="password">
          <el-input v-model="createForm.password" placeholder="初始密码" show-password />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="createForm.display_name" placeholder="显示姓名" />
        </el-form-item>
        <el-form-item label="角色" prop="role">
          <el-select v-model="createForm.role" style="width: 100%">
            <el-option
              v-for="r in roleOptions"
              :key="r.value"
              :label="r.label"
              :value="r.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="部门">
          <el-input v-model="createForm.department" placeholder="所属部门" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 编辑用户 -->
    <el-drawer v-model="editVisible" title="编辑用户" size="420px">
      <el-form :model="editForm" label-width="90px">
        <el-form-item label="用户名">
          <el-input v-model="editForm.username" disabled />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="editForm.display_name" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="editForm.role" style="width: 100%">
            <el-option
              v-for="r in roleOptions"
              :key="r.value"
              :label="r.label"
              :value="r.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="状态">
          <el-select v-model="editForm.status" style="width: 100%">
            <el-option
              v-for="s in statusOptions"
              :key="s.value"
              :label="s.label"
              :value="s.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="部门">
          <el-input v-model="editForm.department" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button :loading="changingPwd" @click="changePassword">修改密码</el-button>
        <el-button type="primary" :loading="saving" @click="saveEdit">保存</el-button>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.users-page {
  padding: 16px;
}

.section-card {
  border: 1px solid var(--mde-border);
}

.card-header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.card-title {
  font-weight: 600;
  font-size: 15px;
}

.header-actions {
  display: flex;
  gap: 8px;
}

.agent-status {
  margin-left: 4px;
}
</style>

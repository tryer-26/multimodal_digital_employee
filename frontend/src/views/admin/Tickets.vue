<script setup>
// 管理端 - 工单管理（文档 12.5.3）
// 工单表格（条件筛选 + 分页）+ 操作列（查看详情 / 状态流转 / 删除）
// 数据经中台 /api/admin/tickets 读写工单 MCP Server
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown } from '@element-plus/icons-vue'
import http from '@/api/http'
import {
  formatDateTime,
  ticketStatusLabel,
  ticketStatusTagType,
  priorityLabel,
  priorityTagType,
  ticketCategoryLabel
} from '@/utils/format'

const STATUSES = ['open', 'in_progress', 'resolved', 'closed']
const PRIORITIES = ['high', 'medium', 'low']
const CATEGORIES = ['network', 'password', 'software', 'finance', 'email', 'general']

const loading = ref(false)
const tickets = ref([])
const total = ref(0)
const userNames = ref({}) // user_id → 姓名，用于列表显示

// 筛选条件（服务端筛选）
const filters = ref({ status: '', priority: '', category: '', keyword: '' })
const page = ref(1)
const pageSize = ref(20)

async function loadTickets() {
  loading.value = true
  try {
    const res = await http.get('/admin/tickets', {
      params: { ...filters.value, page: page.value, page_size: pageSize.value }
    })
    tickets.value = res.items || []
    total.value = res.total || 0
  } finally {
    loading.value = false
  }
}

async function loadUserNames() {
  try {
    const list = await http.get('/admin/users')
    const map = {}
    ;(list || []).forEach((u) => {
      map[u.id] = u.display_name || u.username
    })
    userNames.value = map
  } catch {
    // 仅用于展示，失败不打断工单列表
  }
}

function resetFilters() {
  filters.value = { status: '', priority: '', category: '', keyword: '' }
  page.value = 1
  loadTickets()
}

function handleSearch() {
  page.value = 1
  loadTickets()
}

const otherStatuses = (current) => STATUSES.filter((s) => s !== current)

/* ---------------- 状态流转 ---------------- */
async function changeStatus(row, status) {
  try {
    const res = await http.put(`/admin/tickets/${row.id}/status`, { status })
    const updated = res.ticket || { ...row, status }
    Object.assign(row, updated)
    if (detail.value && detail.value.id === row.id) Object.assign(detail.value, updated)
    ElMessage.success(`工单 ${row.id} 已置为「${ticketStatusLabel(status)}」`)
  } catch {
    // 错误提示由 http 拦截器统一处理
  }
}

/* ---------------- 详情抽屉 ---------------- */
const drawerVisible = ref(false)
const detailLoading = ref(false)
const detail = ref(null)

async function openDetail(row) {
  drawerVisible.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await http.get(`/admin/tickets/${row.id}`)
  } finally {
    detailLoading.value = false
  }
}

/* ---------------- 删除 ---------------- */
async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(`确认删除工单 ${row.id}？该操作不可恢复。`, '删除工单', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消'
    })
  } catch {
    return // 取消
  }
  try {
    await http.delete(`/admin/tickets/${row.id}`)
    ElMessage.success(`工单 ${row.id} 已删除`)
    if (detail.value && detail.value.id === row.id) drawerVisible.value = false
    // 删掉当前页最后一条时回退一页，避免停在空页
    if (tickets.value.length === 1 && page.value > 1) page.value -= 1
    loadTickets()
  } catch {
    // 错误提示由 http 拦截器统一处理
  }
}

onMounted(() => {
  loadTickets()
  loadUserNames()
})
</script>

<template>
  <div class="tickets-page">
    <el-card shadow="never" class="section-card">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">工单管理</span>
          <div class="header-actions">
            <el-select v-model="filters.status" size="small" style="width: 120px" placeholder="状态">
              <el-option label="全部状态" value="" />
              <el-option v-for="s in STATUSES" :key="s" :label="ticketStatusLabel(s)" :value="s" />
            </el-select>
            <el-select v-model="filters.priority" size="small" style="width: 120px" placeholder="优先级">
              <el-option label="全部优先级" value="" />
              <el-option v-for="p in PRIORITIES" :key="p" :label="priorityLabel(p)" :value="p" />
            </el-select>
            <el-select v-model="filters.category" size="small" style="width: 130px" placeholder="分类">
              <el-option label="全部分类" value="" />
              <el-option
                v-for="c in CATEGORIES"
                :key="c"
                :label="ticketCategoryLabel(c)"
                :value="c"
              />
            </el-select>
            <el-input
              v-model="filters.keyword"
              size="small"
              style="width: 190px"
              placeholder="搜索工单号/标题/描述"
              clearable
              @keyup.enter="handleSearch"
            />
            <el-button size="small" type="primary" @click="handleSearch">查询</el-button>
            <el-button size="small" @click="resetFilters">重置</el-button>
          </div>
        </div>
      </template>

      <el-table
        v-loading="loading"
        :data="tickets"
        size="small"
        empty-text="暂无工单"
        highlight-current-row
      >
        <el-table-column prop="id" label="工单号" width="140" />
        <el-table-column prop="title" label="标题" min-width="180" show-overflow-tooltip />
        <el-table-column label="用户" width="110">
          <template #default="{ row }">{{ userNames[row.user_id] || row.user_id || '-' }}</template>
        </el-table-column>
        <el-table-column label="优先级" width="90">
          <template #default="{ row }">
            <el-tag :type="priorityTagType(row.priority)" size="small" effect="plain">
              {{ priorityLabel(row.priority) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="分类" width="110">
          <template #default="{ row }">{{ ticketCategoryLabel(row.category) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="ticketStatusTagType(row.status)" size="small">
              {{ ticketStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="190" fixed="right">
          <template #default="{ row }">
            <div class="row-actions">
              <el-button link type="primary" size="small" @click="openDetail(row)">查看</el-button>
              <el-dropdown trigger="click" @command="(s) => changeStatus(row, s)">
                <el-button link type="primary" size="small">
                  状态<el-icon><ArrowDown /></el-icon>
                </el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item
                      v-for="s in otherStatuses(row.status)"
                      :key="s"
                      :command="s"
                    >
                      置为{{ ticketStatusLabel(s) }}
                    </el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
              <el-button link type="danger" size="small" @click="handleDelete(row)">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>

      <div class="pager">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          background
          @current-change="loadTickets"
          @size-change="handleSearch"
        />
      </div>
    </el-card>

    <!-- 详情抽屉 -->
    <el-drawer v-model="drawerVisible" title="工单详情" size="46%">
      <div v-loading="detailLoading" class="drawer-body">
        <template v-if="detail">
          <el-descriptions :column="2" border size="small" class="desc">
            <el-descriptions-item label="工单号">{{ detail.id }}</el-descriptions-item>
            <el-descriptions-item label="状态">
              <el-tag :type="ticketStatusTagType(detail.status)" size="small">
                {{ ticketStatusLabel(detail.status) }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="标题" :span="2">{{ detail.title || '-' }}</el-descriptions-item>
            <el-descriptions-item label="用户">
              {{ userNames[detail.user_id] || detail.user_id || '-' }}
            </el-descriptions-item>
            <el-descriptions-item label="优先级">
              <el-tag :type="priorityTagType(detail.priority)" size="small" effect="plain">
                {{ priorityLabel(detail.priority) }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="分类">
              {{ ticketCategoryLabel(detail.category) }}
            </el-descriptions-item>
            <el-descriptions-item label="创建时间">
              {{ formatDateTime(detail.created_at) }}
            </el-descriptions-item>
            <el-descriptions-item label="更新时间" :span="2">
              {{ formatDateTime(detail.updated_at) }}
            </el-descriptions-item>
          </el-descriptions>

          <div class="block-title">问题描述</div>
          <div class="desc-text">{{ detail.description || '（无）' }}</div>

          <div class="block-title">状态流转</div>
          <div class="status-actions">
            <el-button
              v-for="s in STATUSES"
              :key="s"
              size="small"
              :type="s === detail.status ? 'primary' : 'default'"
              :disabled="s === detail.status"
              @click="changeStatus(detail, s)"
            >
              {{ ticketStatusLabel(s) }}
            </el-button>
          </div>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.tickets-page {
  padding: 16px;
}

.section-card {
  border: 1px solid var(--mde-border);
}

.card-header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.card-title {
  font-weight: 600;
  font-size: 15px;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.row-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 14px;
}

.drawer-body {
  min-height: 200px;
}

.desc {
  margin-bottom: 16px;
}

.block-title {
  font-weight: 600;
  font-size: 14px;
  margin: 16px 0 8px;
}

.desc-text {
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.6;
  color: var(--mde-text-regular);
  background: var(--mde-bg-page);
  border: 1px solid var(--mde-border);
  border-radius: 6px;
  padding: 10px 12px;
}

.status-actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
</style>

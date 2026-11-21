<script setup>
// 管理端 - 对话日志（文档 12.5.4）
// 会话表格（筛选 + 分页）+ 操作列（查看详情 / 删除）
// 详情抽屉含完整对话 + 工具调用 + 情绪变化 + 置信度
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import http from '@/api/http'
import MessageBubble from '@/components/MessageBubble.vue'
import {
  formatDateTime,
  formatDuration,
  modeLabel,
  endReasonLabel,
  emotionLabel,
  emotionTagType,
  formatClockTime
} from '@/utils/format'

const logs = ref([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)

// 服务端筛选（与工单管理一致）
const modeFilter = ref('')
const keyword = ref('')

async function loadLogs() {
  loading.value = true
  try {
    const res = await http.get('/admin/logs', {
      params: {
        mode: modeFilter.value,
        keyword: keyword.value,
        page: page.value,
        page_size: pageSize.value
      }
    })
    logs.value = res.items || []
    total.value = res.total || 0
  } finally {
    loading.value = false
  }
}

function handleSearch() {
  page.value = 1
  loadLogs()
}

function resetFilters() {
  modeFilter.value = ''
  keyword.value = ''
  page.value = 1
  loadLogs()
}

/* ---------------- 删除 ---------------- */
async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(
      `确认删除会话 ${row.id} 的日志？其消息与工具调用记录将一并删除，且不可恢复。`,
      '删除对话日志',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return // 取消
  }
  try {
    await http.delete(`/admin/logs/${row.id}`)
    ElMessage.success(`会话 ${row.id} 日志已删除`)
    if (detail.value?.conversation?.id === row.id) drawerVisible.value = false
    if (logs.value.length === 1 && page.value > 1) page.value -= 1
    loadLogs()
  } catch {
    // 错误提示由 http 拦截器统一处理
  }
}

/* ---------------- 详情抽屉 ---------------- */
const drawerVisible = ref(false)
const detailLoading = ref(false)
const detail = ref(null) // { conversation, messages, tool_calls }

async function openDetail(row) {
  drawerVisible.value = true
  detailLoading.value = true
  detail.value = null
  try {
    const res = await http.get(`/admin/logs/${row.id}`)
    detail.value = res
  } finally {
    detailLoading.value = false
  }
}

// 情绪变化序列（从消息中提取）
const emotionSeries = computed(() => {
  const msgs = detail.value?.messages || []
  return msgs
    .filter((m) => m.emotion)
    .map((m) => ({
      emotion: m.emotion,
      time: m.created_at,
      role: m.role
    }))
})

// 置信度列表（消息若带 confidence 字段则展示）
const confidenceList = computed(() => {
  const msgs = detail.value?.messages || []
  return msgs
    .filter((m) => m.confidence !== null && m.confidence !== undefined)
    .map((m) => ({
      time: m.created_at,
      role: m.role,
      confidence: m.confidence
    }))
})

const isHandoff = (row) => !!row.handoff_reason

onMounted(loadLogs)
</script>

<template>
  <div class="logs-page">
    <el-card shadow="never" class="section-card">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">对话日志</span>
          <div class="header-actions">
            <el-select
              v-model="modeFilter"
              size="small"
              style="width: 130px"
              placeholder="全部模式"
              @change="handleSearch"
            >
              <el-option label="全部模式" value="" />
              <el-option label="AI模式" value="bot" />
              <el-option label="等待人工" value="waiting_human" />
              <el-option label="人工服务中" value="human" />
            </el-select>
            <el-input
              v-model="keyword"
              size="small"
              style="width: 180px"
              placeholder="搜索用户/标题/会话ID"
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
        :data="logs"
        size="small"
        empty-text="暂无日志"
        highlight-current-row
      >
        <el-table-column prop="id" label="会话ID" width="160" show-overflow-tooltip />
        <el-table-column prop="user_name" label="用户" width="100" />
        <el-table-column prop="title" label="标题" min-width="180" show-overflow-tooltip />
        <el-table-column label="开始时间" width="170">
          <template #default="{ row }">{{ formatDateTime(row.started_at) }}</template>
        </el-table-column>
        <el-table-column prop="message_count" label="轮次" width="80" />
        <el-table-column label="模式" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ modeLabel(row.mode) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="是否转人工" width="110">
          <template #default="{ row }">
            <el-tag :type="isHandoff(row) ? 'warning' : 'info'" size="small">
              {{ isHandoff(row) ? '是' : '否' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="结束原因" width="100">
          <template #default="{ row }">
            {{ row.end_reason ? endReasonLabel(row.end_reason) : '-' }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <div class="row-actions">
              <el-button link type="primary" size="small" @click="openDetail(row)">查看</el-button>
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
          @current-change="loadLogs"
          @size-change="handleSearch"
        />
      </div>
    </el-card>

    <!-- 详情抽屉 -->
    <el-drawer v-model="drawerVisible" title="会话详情" size="60%">
      <div v-loading="detailLoading" class="drawer-body">
        <template v-if="detail">
          <!-- 基本信息 -->
          <el-descriptions :column="2" border size="small" class="desc">
            <el-descriptions-item label="会话ID">{{ detail.conversation?.id }}</el-descriptions-item>
            <el-descriptions-item label="用户">{{ detail.conversation?.user_name || '-' }}</el-descriptions-item>
            <el-descriptions-item label="标题">{{ detail.conversation?.title || '-' }}</el-descriptions-item>
            <el-descriptions-item label="模式">{{ modeLabel(detail.conversation?.mode) }}</el-descriptions-item>
            <el-descriptions-item label="状态">{{ detail.conversation?.status || '-' }}</el-descriptions-item>
            <el-descriptions-item label="结束原因">
              {{ detail.conversation?.end_reason ? endReasonLabel(detail.conversation.end_reason) : '-' }}
            </el-descriptions-item>
            <el-descriptions-item label="转接原因" :span="2">
              {{ detail.conversation?.handoff_reason || '-' }}
            </el-descriptions-item>
          </el-descriptions>

          <el-tabs class="detail-tabs">
            <el-tab-pane label="完整对话">
              <div class="chat-history">
                <MessageBubble
                  v-for="(m, i) in detail.messages || []"
                  :key="m.id || i"
                  :message="m"
                  :show-emotion="true"
                />
                <el-empty
                  v-if="!detail.messages?.length"
                  description="暂无消息"
                  :image-size="60"
                />
              </div>
            </el-tab-pane>

            <el-tab-pane label="工具调用">
              <el-table :data="detail.tool_calls || []" size="small" empty-text="暂无工具调用">
                <el-table-column label="时间" width="160">
                  <template #default="{ row }">{{ formatClockTime(row.created_at) }}</template>
                </el-table-column>
                <el-table-column prop="tool_name" label="工具名" width="160" />
                <el-table-column label="状态" width="100">
                  <template #default="{ row }">
                    <el-tag size="small" :type="row.status === 'success' ? 'success' : 'danger'">
                      {{ row.status }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="耗时" width="100">
                  <template #default="{ row }">{{ formatDuration(row.latency_ms) }}</template>
                </el-table-column>
              </el-table>
            </el-tab-pane>

            <el-tab-pane label="情绪变化">
              <div v-if="emotionSeries.length" class="emotion-series">
                <div v-for="(e, i) in emotionSeries" :key="i" class="emotion-item">
                  <span class="emotion-time">{{ formatClockTime(e.time) }}</span>
                  <el-tag :type="emotionTagType(e.emotion)" size="small" effect="plain">
                    {{ emotionLabel(e.emotion) }}
                  </el-tag>
                </div>
              </div>
              <el-empty v-else description="无情绪记录" :image-size="60" />
            </el-tab-pane>

            <el-tab-pane label="置信度">
              <div v-if="confidenceList.length" class="confidence-list">
                <div v-for="(c, i) in confidenceList" :key="i" class="confidence-item">
                  <span class="conf-time">{{ formatClockTime(c.time) }}</span>
                  <el-progress
                    :percentage="Math.round(Number(c.confidence) * 100)"
                    :stroke-width="14"
                    style="flex: 1"
                  />
                </div>
              </div>
              <el-empty v-else description="无置信度记录" :image-size="60" />
            </el-tab-pane>
          </el-tabs>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.logs-page {
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

.chat-history {
  max-height: 560px;
  overflow-y: auto;
  padding-right: 6px;
}

.emotion-series {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.emotion-item {
  display: flex;
  align-items: center;
  gap: 10px;
}

.emotion-time,
.conf-time {
  font-size: 12px;
  color: var(--mde-text-secondary);
  width: 80px;
}

.confidence-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.confidence-item {
  display: flex;
  align-items: center;
  gap: 12px;
}
</style>

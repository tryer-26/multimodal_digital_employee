<script setup>
// 坐席工作台（文档 12.4）
// 顶部状态切换；左：待接入队列；右：当前会话（AI摘要 + 转接原因 + 对话历史 + 回复 + AI辅助 + 操作）
import { ref, reactive, computed, onMounted, onBeforeUnmount, nextTick, watch } from 'vue'
import { ElMessage, ElMessageBox, ElNotification } from 'element-plus'
import { Promotion, Refresh } from '@element-plus/icons-vue'
import http from '@/api/http'
import { WSClient } from '@/api/ws'
import MessageBubble from '@/components/MessageBubble.vue'
import {
  relativeTime,
  formatClock,
  emotionTagType,
  emotionLabel
} from '@/utils/format'

/* ---------------- 状态 ---------------- */
const status = ref('online') // online | busy | offline
const statusLoading = ref(false)

const queue = ref([])
const queueLoading = ref(false)

const mySessions = ref([]) // 我「服务中」的会话（重启/刷新后据此恢复）
const mySessionsLoading = ref(false)

const session = ref(null) // { conversation, summary, messages, suggestions }
const messages = ref([])
const summary = ref('')
const handoffReason = ref('')
const suggestions = reactive({ knowledge: [], tickets: [], reply_draft: '' })

const replyText = ref('')
const sending = ref(false)
const sessionLoading = ref(false)
const assistLoading = ref(false) // AI 摘要 / 辅助推荐（含 LLM，耗时较长）

const elapsed = ref(0) // 会话计时（秒）
let timer = null
let wsEverOpened = false // 用于区分首次连接与重连

const ws = ref(null)
const messagesRef = ref(null)

/* ---------------- 计算属性 ---------------- */
const hasSession = computed(() => !!session.value)
const convId = computed(() => session.value?.conversation?.id || '')
const userName = computed(() => session.value?.conversation?.user_name || '用户')
const queueCount = computed(() => queue.value.length)
const mySessionCount = computed(() => mySessions.value.length)

const statusOptions = [
  { value: 'online', label: '在线', type: 'success' },
  { value: 'busy', label: '忙碌', type: 'warning' },
  { value: 'offline', label: '离线', type: 'info' }
]
const currentStatusMeta = computed(
  () => statusOptions.find((s) => s.value === status.value) || statusOptions[0]
)

/* ---------------- 数据加载 ---------------- */
async function loadStatus() {
  try {
    const res = await http.get('/agent/status')
    if (res?.status) status.value = res.status
  } catch {
    // ignore
  }
}

async function loadQueue() {
  queueLoading.value = true
  try {
    const list = await http.get('/agent/queue')
    queue.value = Array.isArray(list) ? list : []
  } finally {
    queueLoading.value = false
  }
}

// 我「服务中」的会话：会话进入 human 模式后不再出现在待接入队列，
// 且工作台状态只在内存里，必须靠接口在重启/刷新后取回（见文档 6.8）
async function loadMySessions() {
  mySessionsLoading.value = true
  try {
    const list = await http.get('/agent/sessions')
    mySessions.value = Array.isArray(list) ? list : []
  } finally {
    mySessionsLoading.value = false
  }
}

function refreshPanel() {
  loadQueue()
  loadMySessions()
}

async function changeStatus(val) {
  const prev = status.value
  if (val === prev) return
  statusLoading.value = true
  try {
    await http.post('/agent/status', { status: val })
    // 仅在服务端确认后才更新本地状态（下拉为受控组件，见模板 :model-value）；
    // 状态广播由后端 /agent/status 统一处理，无需前端再发一次 WS 消息
    status.value = val
    ElMessage.success(`状态已切换为「${statusOptions.find((s) => s.value === val)?.label}」`)
  } catch {
    // 失败：保持原状态；受控下拉需触发一次变更才能回显旧值
    status.value = ''
    await nextTick()
    status.value = prev
  } finally {
    statusLoading.value = false
  }
}

/* ---------------- 接入会话 ---------------- */
async function joinSession(item) {
  const id = item.id
  sessionLoading.value = true
  try {
    await http.post(`/agent/session/${id}/join`)
    ws.value?.send({ type: 'join', conversation_id: id })
    await loadSession(id)
    // 接入后会话从队列移入「服务中」：本地直接刷新两侧列表。
    // 不依赖 queue_update 广播——WS 断线/被踢时收不到广播，侧栏会停留在旧状态
    loadQueue()
    loadMySessions()
    ElMessage.success('已接入会话')
  } catch {
    // ignore
  } finally {
    sessionLoading.value = false
  }
}

// 打开「服务中」的会话：此前已接入过，这里只做展示与恢复，
// 不再走 join（mode 已是 human，会被 agent_join 以"不在等待人工状态"拒绝）
async function openSession(item) {
  sessionLoading.value = true
  try {
    await loadSession(item.id)
  } catch {
    // ignore
  } finally {
    sessionLoading.value = false
  }
}

// 打开会话：先秒开会话内容（对话历史），AI 摘要与辅助推荐异步补齐。
// 后者含 LLM 调用约 8 秒，若同步等待，点击后长时间无反馈，形同「点不动」。
// withAssist=false 用于断线重连：只补对话历史，不清空也不重跑 AI 摘要/辅助推荐，
// 否则每次重连都会丢弃已有摘要并重新发起一次 LLM 调用。
// 会话加载序号：快速连点会话时丢弃过期响应，避免旧会话数据覆盖新会话
let sessionSeq = 0
async function loadSession(id, { resetTimer = true, withAssist = true } = {}) {
  const seq = ++sessionSeq
  const res = await http.get(`/agent/session/${id}`)
  // 期间已加载了别的会话：丢弃本次过期结果（否则 convId 会被覆盖，回复将投递到错误会话）
  if (seq !== sessionSeq) return
  session.value = res
  messages.value = res.messages || []
  handoffReason.value = res.conversation?.handoff_reason || ''
  if (withAssist) {
    summary.value = ''
    suggestions.knowledge = []
    suggestions.tickets = []
    suggestions.reply_draft = ''
  }
  if (resetTimer) startTimer()
  scrollToBottom()
  if (withAssist) loadAssist(id)
}

// AI 摘要 + 辅助推荐：慢接口，独立加载与 loading；会话已切换则丢弃结果
async function loadAssist(id) {
  assistLoading.value = true
  try {
    const res = await http.get(`/agent/session/${id}/assist`)
    if (String(convId.value) !== String(id)) return
    summary.value = res.summary?.summary || ''
    suggestions.knowledge = res.suggestions?.knowledge || []
    suggestions.tickets = res.suggestions?.tickets || []
    suggestions.reply_draft = res.suggestions?.reply_draft || ''
  } catch {
    // ignore：辅助信息失败不影响会话主流程
  } finally {
    if (String(convId.value) === String(id)) assistLoading.value = false
  }
}

function startTimer() {
  stopTimer()
  elapsed.value = 0
  timer = setInterval(() => {
    elapsed.value += 1
  }, 1000)
}

function stopTimer() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

/* ---------------- 回复 ---------------- */
function sendReply() {
  const content = replyText.value.trim()
  if (!content || !convId.value) return
  sending.value = true
  http
    .post(`/agent/session/${convId.value}/reply`, { content })
    .then(() => {
      messages.value.push({
        role: 'agent',
        content,
        modality: 'text',
        created_at: new Date().toISOString()
      })
      replyText.value = ''
      scrollToBottom()
    })
    .finally(() => {
      sending.value = false
    })
}

// 使用 AI 建议话术
function useSuggestion(text) {
  replyText.value = text
}

/* ---------------- 会话操作 ---------------- */
async function endService() {
  if (!convId.value) return
  try {
    await ElMessageBox.confirm('确认结束当前会话？会话将结束并由系统记录。', '提示', {
      confirmButtonText: '结束会话',
      cancelButtonText: '取消',
      type: 'warning'
    })
  } catch {
    return
  }
  await http.post(`/agent/session/${convId.value}/end_service`)
  ElMessage.success('会话已结束')
  resetSession()
  loadQueue()
  loadMySessions()
}

async function returnToAi() {
  if (!convId.value) return
  let note = ''
  try {
    const res = await ElMessageBox.prompt(
      '请填写交还 AI 的备注，AI 下次回复时会参考该备注。',
      '转回 AI',
      {
        confirmButtonText: '确认转回',
        cancelButtonText: '取消',
        inputType: 'textarea',
        inputPlaceholder: '例如：已为用户重置VPN客户端配置，请继续跟进'
      }
    )
    note = res.value || ''
  } catch {
    return
  }
  await http.post(`/agent/session/${convId.value}/return_to_ai`, { note })
  ElMessage.success('会话已交还 AI')
  resetSession()
  loadQueue()
  loadMySessions()
}

function resetSession() {
  session.value = null
  messages.value = []
  summary.value = ''
  handoffReason.value = ''
  suggestions.knowledge = []
  suggestions.tickets = []
  suggestions.reply_draft = ''
  replyText.value = ''
  assistLoading.value = false
  stopTimer()
  elapsed.value = 0
}

/* ---------------- 滚动 ---------------- */
function scrollToBottom() {
  nextTick(() => {
    const el = messagesRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}
watch(messages, scrollToBottom, { deep: true })

/* ---------------- WebSocket ---------------- */
function handleWsMessage(msg) {
  const { type, conversation_id: msgConvId, data = {} } = msg || {}
  switch (type) {
    case 'queue_update':
      loadQueue()
      break
    case 'handoff_triggered':
      loadQueue()
      ElNotification({
        title: '新的转人工请求',
        message: `${data.user_name || '用户'}：${data.handoff_reason || '请求人工服务'}`,
        type: 'warning',
        duration: 4000
      })
      break
    case 'user_message':
      // 当前会话内的用户新消息
      if (msgConvId && convId.value && String(msgConvId) === String(convId.value)) {
        messages.value.push({
          role: 'user',
          content: data.content,
          modality: data.modality || 'text',
          image_url: data.image_url || '',
          audio_url: data.audio_url || '',
          attachments: data.attachments || [],
          created_at: data.timestamp || new Date().toISOString()
        })
      } else {
        // 其它会话，可能是排队中用户发来消息，也可能是自己服务中的会话，两者都刷新
        loadQueue()
        loadMySessions()
      }
      break
    default:
      break
  }
}

function connectWs() {
  ws.value = new WSClient('agent')
  ws.value.on('message', handleWsMessage)
  // 同账号在别处登录被踢（4409）：停止自动重连并提示，刷新本页可重新接管
  ws.value.on('kicked', () => {
    ElMessage.error('该账号已在其他窗口登录，本页实时连接已断开；刷新本页可重新接管')
  })
  ws.value.on('open', () => {
    // 注意：连接动作不发送状态。状态只由坐席显式切换（changeStatus），
    // 否则重连时会把库里已保存的「忙碌/离线」覆盖回默认值。
    // 重连（非首次连接）时补齐断线期间错过的用户消息、队列与服务中会话；
    // 但不重跑 AI 摘要/辅助推荐（withAssist=false），避免重连风暴反复触发 LLM 调用
    if (wsEverOpened) {
      loadQueue()
      loadMySessions()
      if (convId.value) loadSession(convId.value, { resetTimer: false, withAssist: false })
    }
    wsEverOpened = true
  })
  ws.value.connect()
}

/* ---------------- 生命周期 ---------------- */
onMounted(async () => {
  // 先取回坐席自己持久化的状态，再建立连接，避免用默认值覆盖库中状态
  await loadStatus()
  connectWs()
  await Promise.all([loadQueue(), loadMySessions()])
  // 应用重启/页面刷新后，自动恢复最近一个未结束、未交回 AI 的会话
  if (!session.value && mySessions.value.length) {
    await openSession(mySessions.value[0])
  }
})

onBeforeUnmount(() => {
  ws.value?.close()
  stopTimer()
})
</script>

<template>
  <div class="agent-page">
    <!-- 顶部工具栏：状态切换 -->
    <div class="agent-toolbar">
      <div class="toolbar-left">
        <el-tag :type="currentStatusMeta.type" effect="dark" size="small">
          {{ currentStatusMeta.label }}
        </el-tag>
        <el-select
          :model-value="status"
          size="small"
          style="width: 110px"
          :loading="statusLoading"
          @change="changeStatus"
        >
          <el-option
            v-for="s in statusOptions"
            :key="s.value"
            :label="s.label"
            :value="s.value"
          />
        </el-select>
        <span class="session-timer" v-if="hasSession">
          会话计时：{{ formatClock(elapsed) }}
        </span>
      </div>
      <el-button size="small" :icon="Refresh" @click="refreshPanel">刷新列表</el-button>
    </div>

    <div class="agent-body">
      <!-- 左栏：服务中 + 待接入队列 -->
      <aside class="queue-panel">
        <div class="panel-head">
          服务中 ({{ mySessionCount }})
        </div>
        <div v-loading="mySessionsLoading" class="queue-scroll sessions-scroll">
          <el-empty
            v-if="!mySessions.length && !mySessionsLoading"
            description="暂无进行中的会话"
            :image-size="50"
          />
          <div
            v-for="item in mySessions"
            :key="item.id"
            class="queue-item"
            :class="{ active: String(item.id) === String(convId) }"
            @click="openSession(item)"
          >
            <div class="queue-item-head">
              <span class="q-user">{{ item.user_name || '用户' }}</span>
              <el-tag type="success" size="small" effect="plain">服务中</el-tag>
            </div>
            <div class="q-reason mde-ellipsis">
              {{ item.last_message || item.title || '会话进行中' }}
            </div>
            <div class="queue-item-foot">
              <span class="q-time">
                {{ relativeTime(item.last_message_at || item.started_at) }}
              </span>
              <el-button
                type="primary"
                size="small"
                plain
                :loading="sessionLoading"
                @click.stop="openSession(item)"
              >
                打开
              </el-button>
            </div>
          </div>
        </div>

        <div class="panel-head">
          待接入队列 ({{ queueCount }})
        </div>
        <div v-loading="queueLoading" class="queue-scroll">
          <el-empty
            v-if="!queue.length && !queueLoading"
            description="暂无待接入会话"
            :image-size="60"
          />
          <div v-for="item in queue" :key="item.id" class="queue-item">
            <div class="queue-item-head">
              <span class="q-user">{{ item.user_name || '用户' }}</span>
              <el-tag
                v-if="item.emotion"
                :type="emotionTagType(item.emotion)"
                size="small"
                effect="plain"
              >
                {{ emotionLabel(item.emotion) }}
              </el-tag>
            </div>
            <div class="q-reason mde-ellipsis">
              {{ item.handoff_reason || '请求人工服务' }}
            </div>
            <div class="queue-item-foot">
              <span class="q-time">
                {{ relativeTime(item.last_message_at) }}
                <template v-if="item.rounds"> · {{ item.rounds }}轮</template>
              </span>
              <el-button
                type="primary"
                size="small"
                :loading="sessionLoading"
                @click="joinSession(item)"
              >
                接入
              </el-button>
            </div>
          </div>
        </div>
      </aside>

      <!-- 右栏：当前会话 -->
      <section class="session-panel">
        <template v-if="hasSession">
          <div class="session-title">
            <span>当前会话：{{ userName }}</span>
            <el-tag v-if="session?.conversation?.status === 'closed'" type="info" size="small">
              已结束
            </el-tag>
          </div>

          <div class="session-cols">
            <!-- 主列·上：对话历史（与右侧卡片区同一网格行，顶底自动对齐） -->
            <el-card shadow="never" class="info-card history-card">
                <template #header>
                  <span class="card-title">对话历史</span>
                </template>
                <div
                  ref="messagesRef"
                  v-loading="sessionLoading"
                  class="history-scroll"
                >
                  <MessageBubble
                    v-for="(m, i) in messages"
                    :key="m.id || i"
                    :message="m"
                    :show-emotion="true"
                    perspective="agent"
                  />
                </div>
              </el-card>

              <div class="reply-box">
                <el-input
                  v-model="replyText"
                  type="textarea"
                  :rows="2"
                  resize="none"
                  placeholder="输入回复..."
                  @keydown.enter.exact.prevent="sendReply"
                />
                <el-button
                  type="primary"
                  :icon="Promotion"
                  :loading="sending"
                  @click="sendReply"
                >
                  发送
              </el-button>
              </div>

            <!-- 侧列·上：摘要 / 转接原因 / AI 辅助（与对话历史同高，内容多时内部滚动） -->
            <div class="session-side-scroll">
              <!-- AI 摘要 -->
              <el-card shadow="never" class="info-card">
                <template #header>
                  <span class="card-title">AI 摘要</span>
                </template>
                <div v-loading="assistLoading" class="card-body-min">
                  <p class="card-text">{{ summary || (assistLoading ? '正在生成…' : '暂无摘要') }}</p>
                </div>
              </el-card>

              <!-- 转接原因 -->
              <el-card shadow="never" class="info-card">
                <template #header>
                  <span class="card-title">转接原因</span>
                </template>
                <p class="card-text">{{ handoffReason || '未提供' }}</p>
              </el-card>

              <!-- AI 辅助推荐 -->
              <el-card shadow="never" class="info-card assist-card">
                <template #header>
                  <span class="card-title">AI 辅助推荐</span>
                </template>

                <div v-loading="assistLoading" class="card-body-min">
                  <div class="assist-block">
                    <div class="assist-label">知识卡片</div>
                    <div v-if="suggestions.knowledge.length" class="assist-list">
                      <div
                        v-for="(k, i) in suggestions.knowledge"
                        :key="i"
                        class="assist-item"
                      >
                        <div class="assist-item-title">{{ k.title }}</div>
                        <div class="assist-item-content">{{ k.content }}</div>
                        <el-button
                          link
                          type="primary"
                          size="small"
                          @click="useSuggestion(k.content)"
                        >
                          使用此建议
                        </el-button>
                      </div>
                    </div>
                    <div v-else class="assist-empty">暂无知识卡片</div>
                  </div>

                  <div class="assist-block">
                    <div class="assist-label">相似工单</div>
                    <div v-if="suggestions.tickets.length" class="assist-list">
                      <div
                        v-for="(t, i) in suggestions.tickets"
                        :key="i"
                        class="assist-item"
                      >
                        <div class="assist-item-title">{{ t.id }} · {{ t.title }}</div>
                        <el-tag size="small" effect="plain">{{ t.status }}</el-tag>
                      </div>
                    </div>
                    <div v-else class="assist-empty">暂无相似工单</div>
                  </div>

                  <div class="assist-block">
                    <div class="assist-label">建议话术</div>
                    <div v-if="suggestions.reply_draft" class="assist-item">
                      <div class="assist-item-content">{{ suggestions.reply_draft }}</div>
                      <el-button
                        link
                        type="primary"
                        size="small"
                        @click="useSuggestion(suggestions.reply_draft)"
                      >
                        使用此建议
                      </el-button>
                    </div>
                    <div v-else class="assist-empty">暂无建议话术</div>
                  </div>
                </div>
              </el-card>

            </div>

            <!-- 侧列·下：操作（固定底部，不随卡片区滚动，保证随时可点） -->
            <div class="session-actions">
              <el-button type="danger" plain @click="endService">结束会话</el-button>
              <el-button type="warning" plain @click="returnToAi">转回 AI</el-button>
            </div>
          </div>
        </template>

        <el-empty
          v-else
          description="请从左侧队列接入一个会话"
          :image-size="120"
          class="no-session"
        />
      </section>
    </div>
  </div>
</template>

<style scoped>
.agent-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--mde-bg-page);
}

.agent-toolbar {
  height: 48px;
  flex-shrink: 0;
  background: var(--mde-bg-card);
  border-bottom: 1px solid var(--mde-border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.session-timer {
  font-size: 13px;
  color: var(--mde-text-regular);
  font-family: 'Consolas', monospace;
}

.agent-body {
  flex: 1;
  min-height: 0;
  display: flex;
  gap: 12px;
  padding: 12px;
}

/* 左栏队列 */
.queue-panel {
  width: 300px;
  flex-shrink: 0;
  background: var(--mde-bg-card);
  border-radius: var(--mde-radius);
  box-shadow: var(--mde-shadow);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.panel-head {
  height: 44px;
  display: flex;
  align-items: center;
  padding: 0 14px;
  font-weight: 600;
  border-bottom: 1px solid var(--mde-border);
}

.queue-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 10px;
}

/* 「服务中」列表：高度自适应，最多占左栏一半，其余留给待接入队列 */
.sessions-scroll {
  flex: 0 1 auto;
  max-height: 45%;
  border-bottom: 1px solid var(--mde-border);
}

.queue-item {
  border: 1px solid var(--mde-border);
  border-radius: var(--mde-radius-sm);
  padding: 10px;
  margin-bottom: 10px;
  transition: box-shadow 0.15s;
}

.sessions-scroll .queue-item {
  cursor: pointer;
}

.queue-item.active {
  border-color: var(--mde-primary);
  background: var(--mde-primary-light);
}

.queue-item:hover {
  box-shadow: var(--mde-shadow);
}

.queue-item-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}

.q-user {
  font-weight: 600;
  font-size: 14px;
}

.q-reason {
  font-size: 13px;
  color: var(--mde-text-regular);
  margin-bottom: 8px;
}

.queue-item-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.q-time {
  font-size: 12px;
  color: var(--mde-text-secondary);
}

/* 右栏会话 */
.session-panel {
  flex: 1;
  min-width: 0;
  background: var(--mde-bg-card);
  border-radius: var(--mde-radius);
  box-shadow: var(--mde-shadow);
  display: flex;
  flex-direction: column;
  padding: 20px 22px;
  overflow: hidden;
}

.session-title {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 16px;
  font-weight: 600;
  padding-bottom: 14px;
  margin-bottom: 18px;
  border-bottom: 1px solid var(--mde-border);
}

/* 右栏两行两列网格，强制左右对齐：
   第 1 行（撑满剩余高度）：对话历史 | 摘要+转接原因+AI辅助 —— 顶底严格对齐
   第 2 行（自适应高度）：回复框 | 操作栏 —— 同行等高 */
.session-cols {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 380px;
  grid-template-rows: minmax(0, 1fr) auto;
  column-gap: 18px;
  row-gap: 16px;
}

/* 侧列卡片区：与对话历史同一行同高，只有这块滚动；
   子项禁止收缩，否则内容会被压扁裁切 */
.session-side-scroll {
  grid-area: 1 / 2;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding-right: 4px;
}

.session-side-scroll > * {
  flex-shrink: 0;
}

/* AI 辅助推荐吸收侧列剩余高度：右列卡片总高度与对话历史底边对齐，不留底部空白 */
.session-side-scroll .assist-card {
  flex-grow: 1;
}

.info-card {
  border: 1px solid var(--mde-border);
}

.session-side-scroll .info-card {
  margin-bottom: 0;
}

/* 卡片标题栏与内容区统一留白，避免各卡片疏密不一 */
.info-card :deep(.el-card__header) {
  padding: 14px 20px;
}

.info-card :deep(.el-card__body) {
  padding: 16px 20px;
}

.card-title {
  font-weight: 600;
  font-size: 14px;
}

.card-text {
  margin: 0;
  font-size: 14px;
  line-height: 1.7;
  color: var(--mde-text-regular);
  white-space: pre-wrap;
}

/* AI 摘要 / 辅助推荐异步加载时的占位高度，保证 loading 遮罩可见 */
.card-body-min {
  min-height: 72px;
}

/* 对话历史卡：撑满网格第 1 行，滚动发生在卡内部 */
.history-card {
  grid-area: 1 / 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.history-card :deep(.el-card__body) {
  flex: 1;
  min-height: 0;
  display: flex;
  padding: 16px 18px;
}

.history-scroll {
  flex: 1;
  min-height: 0;
  width: 100%;
  overflow-y: auto;
  padding: 6px 6px 2px;
}

.reply-box {
  grid-area: 2 / 1;
  display: flex;
  align-items: flex-end;
  gap: 12px;
}

.assist-card :deep(.el-card__body) {
  padding: 16px 18px;
}

.assist-block {
  margin-bottom: 20px;
}

.assist-block:last-child {
  margin-bottom: 0;
}

.assist-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--mde-text-secondary);
  margin-bottom: 10px;
}

.assist-item {
  border: 1px solid var(--mde-border-light);
  background: var(--mde-bg-page);
  border-radius: var(--mde-radius-sm);
  padding: 12px 14px;
  margin-bottom: 10px;
}

.assist-item-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 6px;
}

.assist-item-content {
  font-size: 13px;
  color: var(--mde-text-regular);
  line-height: 1.75;
  margin-bottom: 8px;
}

.assist-empty {
  font-size: 13px;
  color: var(--mde-text-placeholder);
  padding: 10px 0;
}

/* 固定在侧列底部（网格第 2 行，与回复框同行等高），不随卡片区滚动；
   align-items: flex-end 让「结束会话/转回 AI」与主列「发送」按钮底边对齐 */
.session-actions {
  grid-area: 2 / 2;
  display: flex;
  align-items: flex-end;
  gap: 10px;
  border-top: 1px solid var(--mde-border);
}

.no-session {
  margin-top: 80px;
}
</style>

<script setup>
// 用户端聊天页（文档 12.3）
// 左：历史会话列表；右：聊天窗口（模式横幅 + 消息流 + 多模态输入 + 会话关闭）
import { ref, computed, onMounted, onBeforeUnmount, nextTick, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Microphone, Camera, Promotion, VideoPause, Close } from '@element-plus/icons-vue'
import http, { mediaUrl } from '@/api/http'
import { WSClient } from '@/api/ws'
import ConversationList from '@/components/ConversationList.vue'
import MessageBubble from '@/components/MessageBubble.vue'
import ModeBanner from '@/components/ModeBanner.vue'
import {
  formatDateTime,
  endReasonLabel,
  ticketStatusLabel,
  ticketStatusTagType,
  priorityLabel,
  priorityTagType,
  ticketCategoryLabel
} from '@/utils/format'

/* ---------------- 状态 ---------------- */
const conversations = ref([]) // 历史会话
const activeId = ref('')
const messages = ref([]) // 当前会话消息
const inputText = ref('')
const loadingConvs = ref(false)
const loadingMsgs = ref(false)
const sending = ref(false)

const pendingImage = ref(null) // 待发送图片 { url }
const isRecording = ref(false)
const uploading = ref(false)

const ticketsVisible = ref(false)
const tickets = ref([])
const ticketReceipt = ref('') // 结束会话后回执工单号

const ws = ref(null)
const messagesRef = ref(null)
const fileInputRef = ref(null)

let mediaRecorder = null
let audioChunks = []
let audioStream = null
let wsEverOpened = false // 用于区分首次连接与重连

// 流式回复占位消息的临时 id：增量累加到同一条气泡上，定稿后清除（避免下次复用 key 冲突）
const STREAM_TEMP_ID = '__streaming__'

/* ---------------- 计算属性 ---------------- */
const activeConv = computed(
  () =>
    conversations.value.find((c) => String(c.id) === String(activeId.value)) || null
)
const mode = computed(() => activeConv.value?.mode || 'bot')
const closed = computed(() => activeConv.value?.status === 'closed')
const endReason = computed(() => activeConv.value?.end_reason || '')
const inputDisabled = computed(() => closed.value || sending.value)
// 输入框提示语：会话结束 / AI 回复中（可中断）/ 常规
const inputPlaceholder = computed(() => {
  if (closed.value) return '该会话已结束，如需继续请新建会话'
  if (sending.value) return 'AI 正在回复中，可点击「停止」中断…'
  return '输入消息...'
})

/* ---------------- 会话操作 ---------------- */
async function loadConversations() {
  loadingConvs.value = true
  try {
    const list = await http.get('/conversations', { params: { limit: 50 } })
    conversations.value = Array.isArray(list) ? list : []
  } finally {
    loadingConvs.value = false
  }
}

async function createConversation() {
  const conv = await http.post('/conversations')
  conversations.value.unshift(conv)
  activeId.value = conv.id
  messages.value = []
  ticketReceipt.value = ''
  notifySwitch(conv.id)
  // 新会话后端已写入开场欢迎语：立即拉取，避免必须刷新页面才显示
  await loadMessages(conv.id)
  return conv
}

async function handleNewConversation() {
  try {
    await createConversation()
  } catch {
    // 拦截器已提示
  }
}

async function selectConversation(conv) {
  if (String(conv.id) === String(activeId.value)) return
  activeId.value = conv.id
  ticketReceipt.value = ''
  notifySwitch(conv.id)
  await loadMessages(conv.id)
}

// 删除历史会话：确认后调用后端，删除的是当前会话则自动切走
async function handleDeleteConversation(conv) {
  try {
    await ElMessageBox.confirm(
      `确认删除会话「${conv.title || '新会话'}」？删除后聊天记录不可恢复。`,
      '删除会话',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return
  }

  try {
    await http.delete(`/conversations/${conv.id}`)
  } catch {
    return // 拦截器已提示
  }

  const wasActive = String(conv.id) === String(activeId.value)
  conversations.value = conversations.value.filter(
    (c) => String(c.id) !== String(conv.id)
  )
  ElMessage.success('会话已删除')
  if (!wasActive) return

  ticketReceipt.value = ''
  const next = conversations.value[0]
  if (next) {
    activeId.value = next.id
    notifySwitch(next.id)
    await loadMessages(next.id)
  } else {
    activeId.value = ''
    messages.value = []
    try {
      await createConversation()
    } catch {
      // ignore
    }
  }
}

// 切换会话时通知后端（若已连接）
function notifySwitch(conversationId) {
  ws.value?.send({ type: 'switch', conversation_id: conversationId })
}

// 消息加载序号：快速切换会话时丢弃过期响应，避免消息串到别的会话
let msgSeq = 0
async function loadMessages(convId) {
  if (!convId) return
  const seq = ++msgSeq
  // 切换/重载会话时复位等待态，避免上一会话的「停止」状态残留
  sending.value = false
  loadingMsgs.value = true
  try {
    const list = await http.get(`/conversations/${convId}/messages`)
    if (seq !== msgSeq) return
    messages.value = Array.isArray(list) ? list : []
    scrollToBottom()
  } finally {
    if (seq === msgSeq) loadingMsgs.value = false
  }
}

/* ---------------- 消息渲染 ---------------- */
function appendMessage(msg) {
  messages.value.push(msg)
  scrollToBottom()
}

// 取得/创建当前流式占位气泡：同一次 AI 回复的增量都累加到它上面
function ensureStreamMsg() {
  let msg = messages.value.find((m) => m.id === STREAM_TEMP_ID)
  if (!msg) {
    messages.value.push({
      // _key 一经分配不再变化：流式期间/定稿后 Vue 都不会重建该组件
      _key: `stream-${Date.now()}`,
      id: STREAM_TEMP_ID,
      role: 'assistant',
      content: '',
      modality: 'text',
      streaming: true,
      created_at: new Date().toISOString()
    })
    // 取回响应式代理，后续直接改属性即可触发渲染
    msg = messages.value[messages.value.length - 1]
    scrollToBottom()
  }
  return msg
}

function scrollToBottom() {
  nextTick(() => {
    const el = messagesRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

// 收尾未完成的流式占位：已停止/断线时统一调用，避免留下永久「思考中」的空气泡
function finalizeStreaming() {
  const streamMsg = messages.value.find((m) => m.id === STREAM_TEMP_ID)
  if (!streamMsg) return
  streamMsg.id = ''
  streamMsg.streaming = false
  if (streamMsg.content && streamMsg.content.trim()) {
    streamMsg.stopped = true // 已有内容：保留并标注「已停止生成」
  } else {
    messages.value = messages.value.filter((m) => m !== streamMsg)
  }
}

// 非流式终态（bot_reply / system / 转人工）到来时，移除尚未产出内容的「思考中」占位，
// 避免它一直停在「思考中」：真正有内容的占位由对应分支自行定稿。
function discardEmptyStreamMsg() {
  const msg = messages.value.find((m) => m.id === STREAM_TEMP_ID)
  if (msg && !(msg.content || '').trim()) {
    messages.value = messages.value.filter((m) => m !== msg)
  }
}

watch(messages, scrollToBottom, { deep: true })

/* ---------------- 发送消息 ---------------- */
function sendMessage() {
  if (closed.value) return
  const text = inputText.value.trim()
  const imageUrl = pendingImage.value?.url
  if (!text && !imageUrl) return

  const data = {
    modality: imageUrl ? 'image' : 'text',
    content: text,
    image_url: imageUrl || undefined
  }

  const ok = ws.value?.send({
    type: 'user_message',
    conversation_id: activeId.value,
    data
  })

  if (!ok) {
    ElMessage.warning('连接尚未就绪，请稍后重试')
    return
  }

  // 本地乐观渲染用户消息（只填 image_url，attachments 会与 image_url 合并导致同一张图渲染两次）
  appendMessage({
    role: 'user',
    content: text,
    modality: data.modality,
    image_url: imageUrl || '',
    created_at: new Date().toISOString()
  })

  inputText.value = ''
  pendingImage.value = null
  // 进入等待态：bot 模式下输入区置灰、「发送」变「停止」（人工/排队模式无需等待 AI）
  if (mode.value === 'bot') {
    sending.value = true
    // 立即插入「思考中」占位气泡：首字可能要等几十秒，没有占位会让用户误以为“没反应/答案出不来”
    ensureStreamMsg()
  }
}

// 点击「停止」：通知后端取消本会话正在执行的 AI 回合（含流式生成）
function stopGeneration() {
  if (!activeId.value) return
  ws.value?.send({ type: 'stop', conversation_id: activeId.value })
}

/* ---------------- 媒体上传 ---------------- */
async function uploadFile(file) {
  const fd = new FormData()
  fd.append('file', file)
  return http.post('/media/upload', fd, {
    headers: { 'Content-Type': 'multipart/form-data' }
  })
}

// 相机：选择图片 → 上传 → 预览
function triggerImagePick() {
  if (inputDisabled.value) return
  fileInputRef.value?.click()
}

async function onImageChosen(e) {
  const file = e.target.files?.[0]
  e.target.value = ''
  if (!file) return
  uploading.value = true
  try {
    const res = await uploadFile(file)
    pendingImage.value = { url: res.url, assetId: res.asset_id }
  } catch {
    // 拦截器已提示
  } finally {
    uploading.value = false
  }
}

// 麦克风：点击开始录音，再次点击停止并上传发送
async function toggleRecording() {
  if (inputDisabled.value) return
  if (!isRecording.value) {
    await startRecording()
  } else {
    stopRecording()
  }
}

async function startRecording() {
  if (!navigator.mediaDevices?.getUserMedia) {
    ElMessage.warning('当前浏览器不支持录音')
    return
  }
  try {
    audioStream = await navigator.mediaDevices.getUserMedia({ audio: true })
    audioChunks = []
    mediaRecorder = new MediaRecorder(audioStream)
    mediaRecorder.ondataavailable = (ev) => {
      if (ev.data.size > 0) audioChunks.push(ev.data)
    }
    mediaRecorder.onstop = handleRecordingStop
    mediaRecorder.start()
    isRecording.value = true
  } catch {
    ElMessage.error('无法访问麦克风，请检查权限')
  }
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.stop()
  }
  isRecording.value = false
}

async function handleRecordingStop() {
  // 释放麦克风
  audioStream?.getTracks().forEach((t) => t.stop())
  audioStream = null

  const blob = new Blob(audioChunks, { type: 'audio/webm' })
  audioChunks = []
  if (!blob.size) return

  uploading.value = true
  try {
    const file = new File([blob], `voice-${Date.now()}.webm`, { type: 'audio/webm' })
    const res = await uploadFile(file)
    // 语音消息直接发送
    const ok = ws.value?.send({
      type: 'user_message',
      conversation_id: activeId.value,
      data: { modality: 'voice', content: '', audio_url: res.url }
    })
    if (!ok) {
      // 与文字发送一致：连接未就绪时必须提示，避免用户「录完没反应」
      ElMessage.warning('连接尚未就绪，语音未发送，请稍后重试')
      return
    }
    appendMessage({
      role: 'user',
      content: '',
      modality: 'voice',
      audio_url: res.url,
      created_at: new Date().toISOString()
    })
    if (mode.value === 'bot') sending.value = true
  } catch {
    // 拦截器已提示
  } finally {
    uploading.value = false
  }
}

/* ---------------- 会话关闭 ---------------- */
function updateActiveConv(patch) {
  const idx = conversations.value.findIndex(
    (c) => String(c.id) === String(activeId.value)
  )
  if (idx >= 0) conversations.value[idx] = { ...conversations.value[idx], ...patch }
}

// 按会话 id 局部更新（用于后端推送的标题变化等），无需整表刷新
function patchConversation(convId, patch) {
  const idx = conversations.value.findIndex((c) => String(c.id) === String(convId))
  if (idx >= 0) conversations.value[idx] = { ...conversations.value[idx], ...patch }
}

async function closeConversation(reason) {
  if (!activeId.value || closed.value) return
  const label = reason === 'resolved' ? '确认问题已解决并结束会话？' : '确认结束当前会话？'
  try {
    await ElMessageBox.confirm(label, '提示', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning'
    })
  } catch {
    return
  }

  try {
    const res = await http.post(`/conversations/${activeId.value}/close`, { reason })
    updateActiveConv({
      status: 'closed',
      end_reason: reason
    })
    if (res?.ticket_id) {
      ticketReceipt.value = res.ticket_id
      appendMessage({
        role: 'system',
        content: `已为您创建工单：${res.ticket_id}，可在“我的工单”中跟踪处理进度`,
        created_at: new Date().toISOString()
      })
      ElMessage.success(`工单已创建：${res.ticket_id}`)
    } else {
      ElMessage.success('会话已结束')
    }
  } catch {
    // 拦截器已提示
  }
}

/* ---------------- 我的工单 ---------------- */
async function openTickets() {
  ticketsVisible.value = true
  tickets.value = []
  try {
    const list = await http.get('/tickets')
    tickets.value = Array.isArray(list) ? list : []
  } catch {
    // 拦截器已提示
  }
}

/* ---------------- WebSocket ---------------- */
function handleWsMessage(msg) {
  const { type, conversation_id: convId, data = {} } = msg || {}
  // 仅处理当前会话的推送（队列等全局消息忽略）
  const isActive = !convId || String(convId) === String(activeId.value)

  switch (type) {
    case 'bot_stream_delta':
      // AI 流式增量：累加到同一条占位气泡，边到边渲染
      if (isActive) {
        const streamMsg = ensureStreamMsg()
        streamMsg.content += data.delta || ''
        scrollToBottom()
      }
      break
    case 'bot_stream_end': {
      // 流式结束：用完整正文（含参考来源）与附件定稿占位气泡
      if (isActive) {
        const streamMsg = ensureStreamMsg()
        streamMsg.content = data.content || streamMsg.content
        streamMsg.modality = data.modality || 'text'
        streamMsg.attachments = data.attachments || []
        streamMsg.citations = data.citations || []
        streamMsg.confidence = data.confidence
        streamMsg.audio_url = data.audio_url || '' // TTS：带朗读语音则气泡显示播放器
        if (data.timestamp) streamMsg.created_at = data.timestamp
        streamMsg.streaming = false
        streamMsg.id = '' // 清除临时 id，key 回退到索引
        sending.value = false
        scrollToBottom()
      }
      break
    }
    case 'generation_stopped': {
      // 用户点击「停止」：收尾占位气泡并复位等待态
      if (isActive) {
        finalizeStreaming()
        sending.value = false
      }
      break
    }
    case 'bot_reply':
      if (isActive) {
        sending.value = false
        discardEmptyStreamMsg()
        appendMessage({
          role: 'assistant',
          content: data.content,
          modality: data.modality || 'text',
          audio_url: data.audio_url || '', // 闲聊/短回复也可朗读
          attachments: data.attachments || [],
          citations: data.citations || [],
          emotion: data.emotion,
          created_at: data.timestamp || new Date().toISOString()
        })
      }
      break
    case 'agent_message':
      if (isActive) {
        discardEmptyStreamMsg()
        appendMessage({
          role: 'agent',
          content: data.content,
          modality: data.modality || 'text',
          attachments: data.attachments || [],
          created_at: data.timestamp || new Date().toISOString()
        })
      }
      break
    case 'system':
      if (isActive && data.content) {
        sending.value = false // 系统提示（如排队转接/会话已结束）即本轮结束，复位等待态
        discardEmptyStreamMsg()
        appendMessage({
          role: 'system',
          content: data.content,
          created_at: data.timestamp || new Date().toISOString()
        })
      }
      break
    case 'handoff_triggered':
      if (isActive) {
        sending.value = false // 短路转人工时不会有流式回复，据此复位等待态
        discardEmptyStreamMsg()
        updateActiveConv({ mode: 'waiting_human' })
        appendMessage({
          role: 'system',
          content: data.content || '正在为您转接人工客服，请稍候...',
          created_at: new Date().toISOString()
        })
      }
      break
    case 'agent_joined':
      if (isActive) {
        updateActiveConv({ mode: 'human' })
        appendMessage({
          role: 'system',
          content: data.content || '人工客服已接入',
          created_at: new Date().toISOString()
        })
      }
      break
    case 'return_to_bot':
      if (isActive) {
        updateActiveConv({ mode: 'bot' })
        appendMessage({
          role: 'system',
          content: data.content || '人工客服已交还 AI，继续为您服务',
          created_at: new Date().toISOString()
        })
      }
      break
    case 'mode_changed':
      if (isActive && data.mode) {
        updateActiveConv({ mode: data.mode })
      }
      break
    case 'conversation_update':
      // 后端重命名会话后主动推送，实时更新左侧列表与标题（不区分是否为当前会话）
      if (convId) patchConversation(convId, data)
      break
    default:
      break
  }
}

function connectWs() {
  ws.value = new WSClient('user')
  ws.value.on('message', handleWsMessage)
  // 同账号在别处登录被踢（4409）：停止自动重连并提示，刷新本页可重新接管
  ws.value.on('kicked', () => {
    // 被踢后不会再收到流式推送，需就地收尾占位并复位等待态
    finalizeStreaming()
    sending.value = false
    ElMessage.error('该账号已在其他窗口登录，本页实时连接已断开；刷新本页可重新接管')
  })
  // 连接中断：复位等待态并收尾流式占位，避免输入区永久置灰/气泡永久「思考中」
  ws.value.on('close', () => {
    finalizeStreaming()
    sending.value = false
  })
  ws.value.on('error', () => {
    finalizeStreaming()
    sending.value = false
  })
  ws.value.on('open', () => {
    // 连接建立后同步当前会话
    if (activeId.value) notifySwitch(activeId.value)
    // 重连（非首次连接）时补齐断线期间错过的消息与模式变更
    if (wsEverOpened && activeId.value) {
      loadConversations()
      loadMessages(activeId.value)
    }
    wsEverOpened = true
  })
  ws.value.connect()
}

/* ---------------- 生命周期 ---------------- */
onMounted(async () => {
  connectWs()
  await loadConversations()
  if (conversations.value.length) {
    const first = conversations.value[0]
    activeId.value = first.id
    notifySwitch(first.id)
    await loadMessages(first.id)
  } else {
    try {
      await createConversation()
    } catch {
      // ignore
    }
  }
})

onBeforeUnmount(() => {
  ws.value?.close()
  if (isRecording.value) stopRecording()
  audioStream?.getTracks().forEach((t) => t.stop())
})
</script>

<template>
  <div class="chat-page">
    <!-- 左侧历史会话 -->
    <aside class="chat-sidebar">
      <ConversationList
        :conversations="conversations"
        :active-id="activeId"
        :loading="loadingConvs"
        @select="selectConversation"
        @new="handleNewConversation"
        @tickets="openTickets"
        @delete="handleDeleteConversation"
      />
    </aside>

    <!-- 右侧聊天窗口 -->
    <section class="chat-main">
      <!-- 顶部：会话标题 + 模式横幅 -->
      <div class="chat-header">
        <div class="chat-title mde-ellipsis">
          {{ activeConv?.title || '新会话' }}
        </div>
        <el-tag v-if="closed" type="info" size="small" effect="plain">已结束</el-tag>
      </div>
      <ModeBanner :mode="mode" :closed="closed" :end-reason="endReason" />

      <!-- 消息区 -->
      <div ref="messagesRef" v-loading="loadingMsgs" class="chat-messages">
        <el-empty
          v-if="!messages.length && !loadingMsgs"
          description="发送第一条消息，开始咨询吧"
          :image-size="80"
        />
        <MessageBubble
          v-for="(m, i) in messages"
          :key="m._key || m.id || i"
          :message="m"
          :show-emotion="false"
        />
      </div>

      <!-- 输入区 -->
      <div class="chat-input">
        <div v-if="pendingImage" class="pending-preview">
          <el-image :src="mediaUrl(pendingImage.url)" fit="cover" class="pending-img" />
          <el-button
            class="remove-btn"
            circle
            size="small"
            type="danger"
            :icon="Close"
            @click="pendingImage = null"
          />
        </div>

        <div class="input-row">
          <el-button
            :type="isRecording ? 'danger' : 'default'"
            circle
            :icon="Microphone"
            :disabled="inputDisabled"
            :title="isRecording ? '点击停止并发送' : '点击开始录音'"
            @click="toggleRecording"
          />
          <el-button
            circle
            :icon="Camera"
            :disabled="inputDisabled"
            :loading="uploading"
            title="上传图片"
            @click="triggerImagePick"
          />
          <el-input
            v-model="inputText"
            class="text-input"
            type="textarea"
            :rows="2"
            resize="none"
            :disabled="inputDisabled"
            :placeholder="inputPlaceholder"
            @keydown.enter.exact.prevent="sendMessage"
          />
          <el-button
            :type="sending ? 'danger' : 'primary'"
            :icon="sending ? VideoPause : Promotion"
            :disabled="closed"
            @click="sending ? stopGeneration() : sendMessage()"
          >
            {{ sending ? '停止' : '发送' }}
          </el-button>
        </div>

        <div class="input-footer">
          <div class="footer-left">
            <el-button
              size="small"
              :disabled="closed || !activeId"
              @click="closeConversation('user_closed')"
            >
              结束会话
            </el-button>
            <el-button
              size="small"
              type="success"
              plain
              :disabled="closed || !activeId"
              @click="closeConversation('resolved')"
            >
              问题已解决
            </el-button>
            <span v-if="ticketReceipt" class="receipt">
              工单号：{{ ticketReceipt }}
            </span>
          </div>
          <div class="footer-right">
            <span v-if="isRecording" class="recording-tip">● 录音中…</span>
            <span v-else-if="closed" class="closed-tip">
              该会话已结束{{ endReason ? '（' + endReasonLabel(endReason) + '）' : '' }}
            </span>
            <span v-else class="mode-tip">{{ mode }}</span>
          </div>
        </div>
      </div>
    </section>

    <!-- 隐藏的图片选择器 -->
    <input
      ref="fileInputRef"
      type="file"
      accept="image/*"
      style="display: none"
      @change="onImageChosen"
    />

    <!-- 我的工单 -->
    <el-dialog v-model="ticketsVisible" title="我的工单" width="640px">
      <el-table :data="tickets" size="small" empty-text="暂无工单">
        <el-table-column prop="id" label="工单号" width="140" />
        <el-table-column prop="title" label="标题" show-overflow-tooltip />
        <el-table-column label="分类" width="100">
          <template #default="{ row }">{{ ticketCategoryLabel(row.category) }}</template>
        </el-table-column>
        <el-table-column label="优先级" width="90">
          <template #default="{ row }">
            <el-tag :type="priorityTagType(row.priority)" size="small" effect="plain">
              {{ priorityLabel(row.priority) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="ticketStatusTagType(row.status)" size="small">
              {{ ticketStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="160">
          <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
        </el-table-column>
      </el-table>
    </el-dialog>
  </div>
</template>

<style scoped>
.chat-page {
  display: flex;
  height: 100%;
  background: var(--mde-bg-page);
}

.chat-sidebar {
  width: 280px;
  flex-shrink: 0;
  background: var(--mde-bg-card);
  border-right: 1px solid var(--mde-border);
}

.chat-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--mde-bg-card);
}

.chat-header {
  height: 46px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 18px;
  border-bottom: 1px solid var(--mde-border);
}

.chat-title {
  font-size: 15px;
  font-weight: 600;
  max-width: 60%;
}

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 18px;
}

.chat-input {
  flex-shrink: 0;
  border-top: 1px solid var(--mde-border);
  padding: 12px 18px;
}

.pending-preview {
  position: relative;
  display: inline-block;
  margin-bottom: 8px;
}

.pending-img {
  width: 90px;
  height: 90px;
  border-radius: 6px;
  border: 1px solid var(--mde-border);
}

.remove-btn {
  position: absolute;
  top: -8px;
  right: -8px;
}

.input-row {
  display: flex;
  align-items: flex-end;
  gap: 10px;
}

.text-input {
  flex: 1;
}

.input-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 10px;
}

.footer-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.receipt {
  font-size: 12px;
  color: var(--mde-success);
}

.footer-right {
  font-size: 12px;
  color: var(--mde-text-secondary);
}

.recording-tip {
  color: var(--mde-danger);
}

.closed-tip {
  color: var(--mde-text-secondary);
}
</style>

<script setup>
// 消息气泡：区分 AI / 人工坐席 / 用户 / 系统 四类角色
// - AI：左侧蓝色，标签“AI客服”，渲染参考来源
// - 坐席：左侧绿色，标签“人工客服”
// - 用户：右侧
// - 系统：居中灰色小字
import { computed, onBeforeUnmount, ref } from 'vue'
import ImageAttachment from './ImageAttachment.vue'
import CitationList from './CitationList.vue'
import { mediaUrl } from '@/api/http'
import { formatTime, emotionTagType, emotionLabel, modalityLabel } from '@/utils/format'

const props = defineProps({
  message: { type: Object, required: true },
  // 是否展示情绪标签（仅坐席端与管理员日志可见，用户端不展示）
  showEmotion: { type: Boolean, default: false },
  // 视角：user=用户端（用户消息在右、标“我”）；agent=坐席端（用户消息在左标“用户”，坐席消息在右标“我”）
  perspective: { type: String, default: 'user' }
})

// 归一化角色
const role = computed(() => {
  const r = props.message.role
  if (r === 'assistant' || r === 'bot' || r === 'ai') return 'assistant'
  if (r === 'agent' || r === 'human') return 'agent'
  if (r === 'system') return 'system'
  return 'user'
})

// 图片类附件集合（含 image_url）
const images = computed(() => {
  const list = []
  if (props.message.image_url) {
    list.push({ url: mediaUrl(props.message.image_url), source: '', page: null })
  }
  const atts = props.message.attachments || []
  atts.forEach((a) => {
    if (a && (a.type === 'image' || a.modality === 'image') && (a.url || a.asset_id)) {
      list.push({
        url: mediaUrl(a.url || `/api/media/${a.asset_id}`),
        source: a.source || '',
        page: a.page || null
      })
    }
  })
  return list
})

// 音频地址
const audioUrl = computed(() => mediaUrl(props.message.audio_url || ''))

// 右上角喇叭朗读：点击播放/停止（audio_url 为空则无朗读入口）
const playing = ref(false)
let player = null
async function toggleSpeak() {
  if (!audioUrl.value) return
  if (playing.value) {
    player?.pause()
    playing.value = false
    return
  }
  try {
    if (!player) {
      player = new Audio(audioUrl.value)
      player.onended = () => (playing.value = false)
    }
    player.currentTime = 0
    await player.play()
    playing.value = true
  } catch {
    playing.value = false
  }
}

// 组件卸载（切换会话/关闭列表）时停止朗读并释放音频对象，避免后台继续出声
onBeforeUnmount(() => {
  if (player) {
    player.pause()
    player.src = ''
    player = null
  }
  playing.value = false
})

// 是否有正文文本
const hasText = computed(() => !!(props.message.content && String(props.message.content).trim()))

// AI 参考来源（仅 AI 回复且非空时渲染）
const citations = computed(() => {
  if (role.value !== 'assistant') return []
  return props.message.citations || []
})

// 该条消息是否“自己发的”：坐席端以坐席为准，其余以用户为准（决定气泡左右）
const isSelf = computed(() =>
  props.perspective === 'agent' ? role.value === 'agent' : role.value === 'user'
)

const label = computed(() => {
  if (role.value === 'assistant') return 'AI客服'
  if (role.value === 'agent') return props.perspective === 'agent' ? '' : '人工客服'
  return props.perspective === 'agent' ? '用户' : ''
})

const avatarText = computed(() => {
  if (role.value === 'assistant') return 'AI'
  if (role.value === 'agent') return props.perspective === 'agent' ? '我' : '人'
  return props.perspective === 'agent' ? '用' : '我'
})

const timeText = computed(() =>
  formatTime(props.message.created_at || props.message.timestamp)
)

const emotion = computed(() => props.message.emotion)
</script>

<template>
  <!-- 系统消息：居中灰色小字 -->
  <div v-if="role === 'system'" class="msg-system">
    <span class="system-text">{{ message.content }}</span>
    <span class="system-time">{{ timeText }}</span>
  </div>

  <!-- 普通消息气泡 -->
  <div v-else class="msg-row" :class="isSelf ? 'is-user' : 'is-left'">
    <div v-if="!isSelf" class="mde-avatar" :class="`mde-avatar--${role}`">
      {{ avatarText }}
    </div>

    <div class="msg-body">
      <div class="msg-meta">
        <span v-if="label" class="msg-label" :class="role">{{ label }}</span>
        <span class="msg-time">{{ timeText }}</span>
        <el-tag
          v-if="showEmotion && emotion"
          :type="emotionTagType(emotion)"
          size="small"
          effect="plain"
        >
          情绪：{{ emotionLabel(emotion) }}
        </el-tag>
        <el-tag
          v-if="message.modality && message.modality !== 'text'"
          size="small"
          effect="plain"
          type="info"
        >
          {{ modalityLabel(message.modality) }}
        </el-tag>
      </div>

      <div class="bubble" :class="role">
        <!-- 气泡右上角：文本朗读（喇叭）。有 audio_url 才显示 -->
        <button
          v-if="audioUrl"
          class="bubble-speak"
          type="button"
          :title="playing ? '停止朗读' : '朗读'"
          @click.stop="toggleSpeak"
        >
          {{ playing ? '⏸ 停止' : '🔊 朗读' }}
        </button>

        <!-- 流式输出中但尚无文字：三点「思考中」指示 -->
        <div v-if="message.streaming && !hasText" class="bubble-thinking">
          <span class="dot" />
          <span class="dot" />
          <span class="dot" />
        </div>

        <p v-if="hasText" class="bubble-text">{{ message.content }}<span v-if="message.streaming" class="stream-cursor" /></p>

        <div v-if="message.stopped" class="bubble-stopped">已停止生成</div>

        <div v-if="images.length" class="bubble-images">
          <ImageAttachment
            v-for="(img, i) in images"
            :key="i"
            :src="img.url"
            :source="img.source"
            :page="img.page"
          />
        </div>

        <CitationList v-if="citations.length" :citations="citations" />
      </div>
    </div>

    <div v-if="isSelf" class="mde-avatar" :class="`mde-avatar--${role}`">
      {{ avatarText }}
    </div>
  </div>
</template>

<style scoped>
.msg-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  margin-bottom: 18px;
}

.msg-row.is-user {
  flex-direction: row;
  justify-content: flex-end;
}

.msg-body {
  max-width: 72%;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.is-user .msg-body {
  align-items: flex-end;
}

.msg-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
  font-size: 12px;
  color: var(--mde-text-secondary);
}

.msg-label {
  font-weight: 600;
}

.msg-label.assistant {
  color: var(--mde-bot);
}

.msg-label.agent {
  color: var(--mde-agent);
}

.bubble {
  position: relative; /* 供右上角朗读按钮定位 */
  display: inline-block;
  padding: 10px 14px;
  padding-top: 34px; /* 给右上角朗读按钮留出空间 */
  border-radius: 10px;
  background: var(--mde-bg-card);
  border: 1px solid var(--mde-border);
  color: var(--mde-text-primary);
  line-height: 1.6;
  word-break: break-word;
  box-shadow: var(--mde-shadow);
}

/* 气泡右上角朗读按钮 */
.bubble-speak {
  position: absolute;
  top: 7px;
  right: 10px;
  border: none;
  background: transparent;
  color: var(--mde-bot);
  font-size: 12px;
  cursor: pointer;
  padding: 2px 6px;
  border-radius: 6px;
}
.bubble-speak:hover {
  background: rgba(64, 128, 255, 0.12);
}

/* AI：左侧浅蓝 */
.bubble.assistant {
  background: var(--mde-primary-light);
  border-color: #d3e2ff;
}

/* 坐席：左侧浅绿 */
.bubble.agent {
  background: var(--mde-success-light);
  border-color: #c9ecd9;
}

/* 用户：右侧主色 */
.bubble.user {
  background: var(--mde-primary);
  border-color: var(--mde-primary);
  color: #fff;
}

.bubble-text {
  margin: 0;
  white-space: pre-wrap;
}

/* 流式输出光标：跟随增量文本闪烁 */
.stream-cursor {
  display: inline-block;
  width: 2px;
  height: 1em;
  margin-left: 2px;
  vertical-align: text-bottom;
  background: var(--mde-bot);
  animation: stream-blink 1s steps(2, start) infinite;
}

@keyframes stream-blink {
  to {
    visibility: hidden;
  }
}

/* 等待首字时的「思考中」三点 */
.bubble-thinking {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 3px 2px;
}

.bubble-thinking .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--mde-bot);
  opacity: 0.4;
  animation: think-bounce 1.2s infinite ease-in-out;
}

.bubble-thinking .dot:nth-child(2) {
  animation-delay: 0.15s;
}

.bubble-thinking .dot:nth-child(3) {
  animation-delay: 0.3s;
}

@keyframes think-bounce {
  0%,
  60%,
  100% {
    opacity: 0.25;
    transform: translateY(0);
  }
  30% {
    opacity: 1;
    transform: translateY(-3px);
  }
}

/* 用户点击「停止」后保留的部分回复 */
.bubble-stopped {
  margin-top: 6px;
  font-size: 12px;
  color: var(--mde-text-secondary);
}

.bubble-images {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}

/* 系统消息 */
.msg-system {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  margin: 12px 0;
  font-size: 12px;
  color: var(--mde-text-secondary);
}

.system-text {
  background: #eef0f4;
  padding: 3px 10px;
  border-radius: 10px;
}

.system-time {
  color: var(--mde-text-placeholder);
}
</style>

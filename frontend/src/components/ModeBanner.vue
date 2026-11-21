<script setup>
// 会话模式横幅：展示当前模式（AI / 等待人工 / 人工服务中）或已结束提示
import { computed } from 'vue'
import { endReasonLabel } from '@/utils/format'

const props = defineProps({
  // bot | waiting_human | human
  mode: { type: String, default: 'bot' },
  // 会话是否已关闭
  closed: { type: Boolean, default: false },
  // 结束原因：resolved | user_closed | unresolved
  endReason: { type: String, default: '' }
})

const config = computed(() => {
  if (props.closed) {
    const reason = endReasonLabel(props.endReason)
    return {
      show: true,
      type: 'info',
      icon: 'CircleCheck',
      text: `该会话已结束${reason ? '（' + reason + '）' : ''}，如需继续请新建会话`
    }
  }
  if (props.mode === 'waiting_human') {
    return {
      show: true,
      type: 'warning',
      icon: 'Loading',
      text: '正在为您转接人工客服，请稍候...'
    }
  }
  if (props.mode === 'human') {
    return {
      show: true,
      type: 'success',
      icon: 'Headset',
      text: '人工客服已接入'
    }
  }
  // bot 模式默认不显示横幅
  return { show: false, type: 'info', icon: 'Service', text: '' }
})
</script>

<template>
  <div v-if="config.show" class="mode-banner" :class="config.type">
    <el-icon class="banner-icon"><component :is="config.icon" /></el-icon>
    <span>{{ config.text }}</span>
  </div>
</template>

<style scoped>
.mode-banner {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 34px;
  font-size: 13px;
  border-bottom: 1px solid transparent;
}

.mode-banner.warning {
  background: var(--mde-warning-light);
  color: #b88230;
  border-bottom-color: #f5dab1;
}

.mode-banner.success {
  background: var(--mde-success-light);
  color: #1c7a51;
  border-bottom-color: #c9ecd9;
}

.mode-banner.info {
  background: #eef0f4;
  color: var(--mde-text-regular);
  border-bottom-color: var(--mde-border);
}

.banner-icon {
  animation: none;
}

.mode-banner.warning .banner-icon {
  animation: spin 1.2s linear infinite;
}

@keyframes spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}
</style>

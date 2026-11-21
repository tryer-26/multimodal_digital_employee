<script setup>
// ECharts 基础封装：传入 option 自动渲染，容器尺寸变化自动 resize
import { ref, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import * as echarts from 'echarts'

const props = defineProps({
  option: { type: Object, required: true },
  height: { type: String, default: '300px' },
  loading: { type: Boolean, default: false }
})

const el = ref(null)
let chart = null
let resizeObserver = null

function render() {
  if (!chart) return
  chart.setOption(props.option, true)
}

function init() {
  if (!el.value) return
  chart = echarts.init(el.value)
  render()
  if (props.loading) chart.showLoading({ text: '加载中...', color: '#2f6bff' })
  else chart.hideLoading()
}

watch(
  () => props.option,
  () => render(),
  { deep: true }
)

watch(
  () => props.loading,
  (val) => {
    if (!chart) return
    if (val) chart.showLoading({ text: '加载中...', color: '#2f6bff' })
    else chart.hideLoading()
  }
)

onMounted(() => {
  nextTick(() => {
    init()
    resizeObserver = new ResizeObserver(() => chart?.resize())
    if (el.value) resizeObserver.observe(el.value)
  })
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div ref="el" class="base-chart" :style="{ height }"></div>
</template>

<style scoped>
.base-chart {
  width: 100%;
}
</style>

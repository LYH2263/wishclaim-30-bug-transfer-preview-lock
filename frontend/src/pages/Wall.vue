<template>
  <div class="wall">
    <h1 class="serif">愿望墙</h1>
    <p class="tag">无顶栏 · 瀑布流 · 点卡片认领</p>
    <div class="masonry">
      <article v-for="w in rows" :key="w.id" class="card" @click="$router.push('/wishes/'+w.id)">
        <h3>{{ w.title || '（无标题）' }}</h3>
        <p>{{ w.note }}</p>
        <p class="tag">{{ w.status }} · {{ w.data_quality }}</p>
        <p v-if="w.status==='claimed'" class="tag">
          认领人 {{ w.claimer || '—' }} · 倒计时 {{ fmtCountdown(w.expires_at, tick) }}
        </p>
        <p v-if="w.pending_transfer" class="tag transfer-tag">
          转让托管中 {{ w.pending_transfer.from_claimer }} → {{ w.pending_transfer.target_claimer }}
          （当前仍由 {{ w.claimer }} 持锁；确认后换新人并满额重开倒计时）
        </p>
      </article>
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { api } from '../api'
import { fmtCountdown } from '../util'
const rows = ref([])
const tick = ref(Date.now())
let timer = null
onMounted(async () => {
  rows.value = await api('/wishes')
  timer = setInterval(() => { tick.value = Date.now() }, 1000)
})
onUnmounted(() => timer && clearInterval(timer))
</script>

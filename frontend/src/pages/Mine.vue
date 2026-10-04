<template>
  <div class="wall">
    <h1 class="serif">我的认领</h1>
    <input v-model="name" @change="load" placeholder="认领人名" />
    <article v-for="w in rows" :key="w.id" class="card" @click="$router.push('/wishes/'+w.id)">
      <h3>{{ w.title }}</h3>
      <span class="tag">{{ w.status }} · 到期 {{ w.expires_at }} · 剩余 {{ fmtCountdown(w.expires_at, tick) }}</span>
      <p v-if="w.pending_transfer" class="tag transfer-tag">
        转让托管中：等待 {{ w.pending_transfer.target_claimer }} 确认；确认前锁仍归你独占（他人无法认领），
        确认成功后此行整行移到 {{ w.pending_transfer.target_claimer }} 名下并满额重开倒计时
      </p>
    </article>
    <p v-if="!rows.length" class="tag">暂无认领</p>
  </div>
</template>
<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { api } from '../api'
import { fmtCountdown } from '../util'
const name = ref('访客')
const rows = ref([])
const tick = ref(Date.now())
let timer = null
async function load() { rows.value = await api('/mine?claimer=' + encodeURIComponent(name.value)) }
onMounted(async () => { await load(); timer = setInterval(() => { tick.value = Date.now() }, 1000) })
onUnmounted(() => timer && clearInterval(timer))
</script>

<template>
  <div class="wall">
    <h1 class="serif">{{ w.title }}</h1>
    <p>{{ w.note }}</p>
    <p class="tag">状态 {{ w.status }} · 认领人 {{ w.claimer || '—' }}</p>
    <p v-if="w.status==='claimed'" class="tag">
      到期 {{ w.expires_at }} · 剩余 {{ fmtCountdown(w.expires_at, tick) }}
    </p>
    <p v-if="w.pending_transfer" class="tag transfer-tag">
      转让托管中：{{ w.pending_transfer.from_claimer }} → {{ w.pending_transfer.target_claimer }}
      （确认后 TTL 自转让时刻重开满额）
    </p>
    <p v-if="err" class="err">{{ errText(err) }}</p>
    <input v-model="claimer" placeholder="你的名字" />
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button @click="claim">认领锁定</button>
      <button class="ghost" @click="release">释放</button>
      <button class="ghost" @click="fulfill">核销完成</button>
    </div>

    <div v-if="w.status==='claimed'" class="transfer-box">
      <h3 class="serif">认领转让托管</h3>
      <template v-if="!w.pending_transfer">
        <input v-model="target" placeholder="目标认领人名字" />
        <button @click="offer">发起转让</button>
        <p class="tag">发起后进入托管：列出目标人与当前到期时间但暂不改认领；目标人确认前他人无法认领。</p>
      </template>
      <template v-else>
        <p>待确认转让单：{{ w.pending_transfer.from_claimer }} → {{ w.pending_transfer.target_claimer }}</p>
        <p class="tag">当前到期 {{ w.expires_at }}；确认后到期时间将以确认时刻满额重开。</p>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button @click="confirm">我是目标人，确认接手</button>
          <button class="ghost" @click="cancel">我是原认领人，撤销</button>
        </div>
      </template>
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { api } from '../api'
import { fmtCountdown, ERR_TEXT } from '../util'
const props = defineProps({ id: String })
const w = ref({})
const claimer = ref('访客')
const target = ref('')
const err = ref('')
const tick = ref(Date.now())
let timer = null
function errText(e) { return ERR_TEXT[e.message] || e.message }
async function load() { w.value = await api('/wishes/' + props.id) }
async function claim() {
  err.value=''; try { await api('/wishes/'+props.id+'/claim',{method:'POST',body:JSON.stringify({claimer:claimer.value})}); await load() } catch(e){ err.value=e }
}
async function release() {
  err.value=''; try { await api('/wishes/'+props.id+'/release',{method:'POST',body:'{}'}); await load() } catch(e){ err.value=e }
}
async function fulfill() {
  err.value=''; try { await api('/wishes/'+props.id+'/fulfill',{method:'POST',body:'{}'}); await load() } catch(e){ err.value=e }
}
async function offer() {
  err.value=''
  try {
    await api('/wishes/'+props.id+'/transfer',
      { method:'POST', body: JSON.stringify({ claimer: claimer.value, target: target.value }) })
    // 重载后该行进入「待确认转让单」分支，由目标人确认 / 原认领人撤销
    await load()
  } catch(e){ err.value=e }
}
async function confirm() {
  err.value=''
  try {
    const tid = w.value.pending_transfer.id
    await api('/transfers/'+tid+'/confirm',{method:'POST',body:JSON.stringify({claimer:claimer.value})})
    await load()
  } catch(e){ err.value=e }
}
async function cancel() {
  err.value=''
  try {
    const tid = w.value.pending_transfer.id
    await api('/transfers/'+tid+'/cancel',{method:'POST',body:JSON.stringify({claimer:claimer.value})})
    await load()
  } catch(e){ err.value=e }
}
onMounted(async () => { await load(); timer = setInterval(() => { tick.value = Date.now() }, 1000) })
onUnmounted(() => timer && clearInterval(timer))
</script>

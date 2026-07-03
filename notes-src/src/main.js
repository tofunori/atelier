import { Crepe } from '@milkdown/crepe'
import '@milkdown/crepe/theme/common/style.css'
import '@milkdown/crepe/theme/nord-dark.css'

const SAVE_DEBOUNCE_MS = 1000
const indicator = document.getElementById('save-indicator')

function showSaveFailed(failed) {
  indicator.classList.toggle('show', failed)
}

async function main() {
  // ---- 1. Initial load ----
  let initial = ''
  try {
    const r = await fetch('/notes/load')
    const data = await r.json()
    if (data && typeof data.markdown === 'string') initial = data.markdown
  } catch (e) {
    console.warn('notes/load failed', e)
  }

  const crepe = new Crepe({
    root: document.getElementById('app'),
    defaultValue: initial,
  })
  await crepe.create()

  // ---- 2. Autosave (debounced) ----
  let saveTimer = null
  const doSave = () => {
    saveTimer = null
    const markdown = crepe.getMarkdown()
    fetch('/notes/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ markdown }),
    })
      .then((r) => {
        if (!r.ok) throw new Error('bad status ' + r.status)
        showSaveFailed(false)
      })
      .catch((e) => {
        console.warn('notes/save failed', e)
        showSaveFailed(true)
      })
  }

  crepe.on((api) => {
    api.markdownUpdated(() => {
      if (saveTimer) clearTimeout(saveTimer)
      saveTimer = setTimeout(doSave, SAVE_DEBOUNCE_MS)
    })
  })

  // ---- 3. Flush pending save on hide via sendBeacon ----
  const flushOnHide = () => {
    if (!saveTimer) return
    clearTimeout(saveTimer)
    saveTimer = null
    const body = JSON.stringify({ markdown: crepe.getMarkdown() })
    navigator.sendBeacon('/notes/save', new Blob([body], { type: 'application/json' }))
  }
  window.addEventListener('pagehide', flushOnHide)
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flushOnHide()
  })
}

main()

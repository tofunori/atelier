import { Crepe } from '@milkdown/crepe'
import '@milkdown/crepe/theme/common/style.css'
import '@milkdown/crepe/theme/nord-dark.css'
import { editorViewCtx } from '@milkdown/kit/core'
import { callCommand } from '@milkdown/kit/utils'
import {
  toggleStrongCommand,
  toggleEmphasisCommand,
  wrapInHeadingCommand,
  turnIntoTextCommand,
  wrapInBulletListCommand,
  wrapInOrderedListCommand,
  wrapInBlockquoteCommand,
  createCodeBlockCommand,
  insertHrCommand,
  toggleLinkCommand,
} from '@milkdown/kit/preset/commonmark'
import { toggleStrikethroughCommand } from '@milkdown/kit/preset/gfm'

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

  // ---- 1b. Top toolbar ----
  const editor = crepe.editor
  const run = (command, payload) => editor.action(callCommand(command.key, payload))

  // Task list has no dedicated command in this Milkdown version: wrap the
  // selection in a bullet list, then flag each list_item as a checkbox item
  // (checked attr set -> GFM renders it as a task item).
  const makeTaskList = () => {
    editor.action((ctx) => {
      callCommand(wrapInBulletListCommand.key)(ctx)
      const view = ctx.get(editorViewCtx)
      const { state } = view
      const { from, to } = state.selection
      const tr = state.tr
      state.doc.nodesBetween(from, to, (node, pos) => {
        if (node.type.name === 'list_item' && node.attrs.checked == null) {
          tr.setNodeMarkup(pos, undefined, { ...node.attrs, checked: false })
        }
      })
      if (tr.docChanged) view.dispatch(tr)
    })
  }

  const actions = {
    paragraph: () => run(turnIntoTextCommand),
    h1: () => run(wrapInHeadingCommand, 1),
    h2: () => run(wrapInHeadingCommand, 2),
    h3: () => run(wrapInHeadingCommand, 3),
    bold: () => run(toggleStrongCommand),
    italic: () => run(toggleEmphasisCommand),
    strike: () => run(toggleStrikethroughCommand),
    bullet: () => run(wrapInBulletListCommand),
    ordered: () => run(wrapInOrderedListCommand),
    task: makeTaskList,
    quote: () => run(wrapInBlockquoteCommand),
    code: () => run(createCodeBlockCommand),
    link: () => run(toggleLinkCommand),
    hr: () => run(insertHrCommand),
  }

  const toolbar = document.getElementById('toolbar')
  toolbar.addEventListener('mousedown', (e) => {
    // never steal focus from the editor
    if (e.target.closest('button')) e.preventDefault()
  })
  toolbar.addEventListener('click', (e) => {
    const btn = e.target.closest('button')
    if (!btn) return
    const fn = actions[btn.dataset.cmd]
    if (!fn) return
    fn()
    editor.action((ctx) => ctx.get(editorViewCtx).focus())
  })

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

const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const ts = require('../web/node_modules/typescript')
const Module = require('node:module')
const path = require('node:path')
const source = fs.readFileSync(path.join(__dirname, '../web/src/lib/translationPriority.ts'), 'utf8')
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const mod = new Module('priority', module); mod._compile(js, 'priority.js')
const { priorityBlocks } = mod.exports
const blocks = Array.from({length: 8}, (_, i) => ({ id: `b${i}`, page: i < 4 ? 1 : 2, translatable: true, kind:'para', text:'paragraph', translated:'' }))
test('visible blocks across pages precede three blocks following the viewport', () => {
  assert.deepEqual(priorityBlocks(blocks, ['b3','b4'], false, 1), ['b3','b4','b5','b6','b7'])
})
test('pdf mode follows actual page and skips translated blocks', () => {
  const changed = blocks.map((b) => b.id === 'b4' ? {...b, translated:'done'} : b)
  assert.deepEqual(priorityBlocks(changed, ['b0'], true, 2), ['b5','b6','b7'])
})
test('failed untranslated blocks remain eligible on retry', () => {
  assert.deepEqual(priorityBlocks(blocks, ['b2'], false, 1), ['b2','b3','b4','b5'])
})

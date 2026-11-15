/**
 * 把反馈令牌加密成 Rust 壳可解密的两份资源文件。
 *
 * 产物（都在已 gitignore 的 `src-tauri/resources/` 下）：
 * - `feedback-token.key`：32 字节 AES-256 主密钥，build.rs 会把它编译进壳；
 * - `feedback-token.bin`：nonce(12) + 密文 + GCM tag(16)。
 *
 * 运行时由 `src-tauri/src/feedback.rs` 解密，再通过环境变量交给 Python sidecar，
 * 因此 sidecar 与数据目录里都不会出现明文令牌。
 *
 * 用法：
 *   node scripts/pack-feedback-token.mjs                      # 读 PAPEREAD_FEEDBACK_TOKEN
 *   node scripts/pack-feedback-token.mjs --token <令牌>
 *   node scripts/pack-feedback-token.mjs --rotate-key         # 连主密钥一起换
 *   node scripts/pack-feedback-token.mjs --from-file <明文文件>
 *
 * 注意：这是客户端内嵌凭据，只能提高提取门槛，无法阻止逆向者还原。
 */
import { createCipheriv, randomBytes } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const RESOURCES = join(ROOT, 'src-tauri', 'resources')
const KEY_PATH = join(RESOURCES, 'feedback-token.key')
const BLOB_PATH = join(RESOURCES, 'feedback-token.bin')

const KEY_BYTES = 32
const NONCE_BYTES = 12
const MIN_TOKEN_LEN = 16

function parseArgs(argv) {
  const args = { token: '', fromFile: '', rotateKey: false }
  for (let i = 0; i < argv.length; i += 1) {
    const item = argv[i]
    if (item === '--token') args.token = argv[++i] || ''
    else if (item === '--from-file') args.fromFile = argv[++i] || ''
    else if (item === '--rotate-key') args.rotateKey = true
  }
  return args
}

const args = parseArgs(process.argv.slice(2))
const token = (
  args.token ||
  (args.fromFile ? readFileSync(args.fromFile, 'utf8') : '') ||
  process.env.PAPEREAD_FEEDBACK_TOKEN ||
  ''
).trim()

if (token.length < MIN_TOKEN_LEN) {
  console.error('未提供有效令牌：请用 --token、--from-file 或 PAPEREAD_FEEDBACK_TOKEN。')
  process.exit(2)
}

mkdirSync(RESOURCES, { recursive: true })
let key
if (args.rotateKey || !existsSync(KEY_PATH)) {
  key = randomBytes(KEY_BYTES)
  writeFileSync(KEY_PATH, key)
  console.log(`已生成新主密钥：${relative(ROOT, KEY_PATH)}（${KEY_BYTES} 字节）`)
} else {
  key = readFileSync(KEY_PATH)
  if (key.length !== KEY_BYTES) {
    console.error(`已有主密钥长度不是 ${KEY_BYTES} 字节：${KEY_PATH}`)
    process.exit(3)
  }
}

const nonce = randomBytes(NONCE_BYTES)
const cipher = createCipheriv('aes-256-gcm', key, nonce)
const ciphertext = Buffer.concat([cipher.update(token, 'utf8'), cipher.final()])
const tag = cipher.getAuthTag()
const blob = Buffer.concat([nonce, ciphertext, tag])
writeFileSync(BLOB_PATH, blob)
console.log(`已写入密文：${relative(ROOT, BLOB_PATH)}（${blob.length} 字节）；令牌与密钥内容均未打印。`)
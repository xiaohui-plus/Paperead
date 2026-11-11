/**
 * 手机端离线库。
 *
 * 把「电脑已解析好的结果」整篇搬到本机：blocks JSON（含译文与高亮）、PDF 本体、
 * 缩略图和正文里的图片。离开电脑后阅读器直接读这里，不再请求服务器。
 *
 * 手机上只改译文（`saveOfflineBlocks`），不回传电脑，避免和电脑端的数据分叉。
 */
import type { Paper } from '../types';

const DB_NAME = 'paperead-offline';
const DB_VERSION = 2;
const STORE = 'papers';
const CHAT_STORE = 'chats';
/** 页图渲染宽度：和 PdfPages 的默认宽度一致，离线预览不用再请求 */
const PAGE_WIDTH = 1000;

export interface OfflineAsset {
  path: string;
  blob: Blob;
}

export interface OfflineRecord {
  id: string;
  title: string;
  authors: string;
  /** 服务端 `/blocks` 的原始响应；从 StoryReader 导入的是本地构造的同形状对象 */
  payload: Record<string, unknown> & { blocks?: unknown[] };
  pdf: Blob | null;
  thumb: Blob | null;
  assets: OfflineAsset[];
  bytes: number;
  updatedAt: number;
  /** 来源：电脑解析的论文，或从 StoryReader 导入的书 */
  source?: 'paper' | 'storyreader';
}

export interface OfflineSummary {
  id: string;
  title: string;
  authors: string;
  bytes: number;
  updatedAt: number;
  hasPdf: boolean;
  pages: number;
  source: 'paper' | 'storyreader';
}

export interface DownloadProgress {
  stage: 'blocks' | 'pdf' | 'thumb' | 'pages' | 'assets' | 'done';
  done: number;
  total: number;
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE, { keyPath: 'id' });
      }
      if (!db.objectStoreNames.contains(CHAT_STORE)) {
        db.createObjectStore(CHAT_STORE, { keyPath: 'paperId' });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error('打开离线库失败'));
  });
}

function finish(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error ?? new Error('写入离线库失败'));
    tx.onabort = () => reject(tx.error ?? new Error('写入离线库已取消'));
  });
}

async function getRecord(id: string): Promise<OfflineRecord | null> {
  const db = await openDb();
  try {
    return await new Promise((resolve, reject) => {
      const request = db.transaction(STORE, 'readonly').objectStore(STORE).get(id);
      request.onsuccess = () => resolve((request.result as OfflineRecord | undefined) ?? null);
      request.onerror = () => reject(request.error ?? new Error('读取离线库失败'));
    });
  } finally {
    db.close();
  }
}

async function fetchBlob(url: string): Promise<Blob | null> {
  try {
    const response = await fetch(url);
    if (!response.ok) return null;
    return await response.blob();
  } catch {
    return null;
  }
}

export function pageImagePath(paperId: string, page: number): string {
  return `/api/papers/${encodeURIComponent(paperId)}/page/${page}.png?width=${PAGE_WIDTH}`;
}

function collectAssetPaths(payload: { blocks?: unknown[] }): string[] {
  const paths = new Set<string>();
  for (const block of payload.blocks ?? []) {
    const path = (block as { image_path?: unknown }).image_path;
    if (typeof path === 'string' && path.startsWith('/api/')) paths.add(path);
  }
  return [...paths];
}

/** 原版 PDF 视图逐页取图；离线时同样读这些页图，不必在手机上再跑 pdf.js。 */
function collectPagePaths(paperId: string, payload: Record<string, unknown>): string[] {
  const pages = Number(payload.page_count ?? 0);
  return Array.from({ length: Math.min(Math.max(pages, 0), 500) }, (_, index) =>
    pageImagePath(paperId, index + 1),
  );
}

/** 把一篇论文从服务器下载到本机，已存在则覆盖。 */
export async function downloadPaper(
  paperId: string,
  onProgress?: (progress: DownloadProgress) => void,
): Promise<OfflineSummary> {
  const report = (stage: DownloadProgress['stage'], done: number, total: number) =>
    onProgress?.({ stage, done, total });

  report('blocks', 0, 1);
  const response = await fetch(`/api/papers/${paperId}/blocks`);
  if (!response.ok) throw new Error(`下载解析结果失败（HTTP ${response.status}）`);
  const payload = (await response.json()) as OfflineRecord['payload'];
  report('blocks', 1, 1);

  report('pdf', 0, 1);
  const pdf = await fetchBlob(`/api/papers/${paperId}/file`);
  report('pdf', 1, 1);

  report('thumb', 0, 1);
  const thumb = await fetchBlob(`/api/papers/${paperId}/thumb.png`);
  report('thumb', 1, 1);

  const pagePaths = collectPagePaths(paperId, payload);
  const paths = collectAssetPaths(payload);
  const assets: OfflineAsset[] = [];

  report('pages', 0, pagePaths.length);
  for (let index = 0; index < pagePaths.length; index += 1) {
    const blob = await fetchBlob(pagePaths[index]);
    if (blob) assets.push({ path: pagePaths[index], blob });
    report('pages', index + 1, pagePaths.length);
  }

  report('assets', 0, paths.length);
  for (let index = 0; index < paths.length; index += 1) {
    const blob = await fetchBlob(paths[index]);
    if (blob) assets.push({ path: paths[index], blob });
    report('assets', index + 1, paths.length);
  }

  const bytes =
    new Blob([JSON.stringify(payload)]).size +
    (pdf?.size ?? 0) +
    (thumb?.size ?? 0) +
    assets.reduce((sum, asset) => sum + asset.blob.size, 0);

  const record: OfflineRecord = {
    id: paperId,
    title: String(payload.title ?? ''),
    authors: String(payload.authors ?? ''),
    payload,
    pdf,
    thumb,
    assets,
    bytes,
    updatedAt: Date.now(),
    source: 'paper',
  };

  await putRecord(record);

  report('done', 1, 1);
  return summarize(record);
}

async function putRecord(record: OfflineRecord): Promise<void> {
  const db = await openDb();
  try {
    const tx = db.transaction(STORE, 'readwrite');
    tx.objectStore(STORE).put(record);
    await finish(tx);
  } finally {
    db.close();
  }
}

function summarize(record: OfflineRecord): OfflineSummary {
  return {
    id: record.id,
    title: record.title,
    authors: record.authors,
    bytes: record.bytes,
    updatedAt: record.updatedAt,
    hasPdf: Boolean(record.pdf) || record.assets.some((asset) => /\/page\/\d+\.png/.test(asset.path)),
    pages: Number(record.payload.page_count ?? 0),
    source: record.source === 'storyreader' ? 'storyreader' : 'paper',
  };
}

function recordBytes(record: OfflineRecord): number {
  return (
    new Blob([JSON.stringify(record.payload)]).size +
    (record.pdf?.size ?? 0) +
    (record.thumb?.size ?? 0) +
    record.assets.reduce((sum, asset) => sum + asset.blob.size, 0)
  );
}

/** 同步包导入：直接写入一条完整离线记录，同 id 会覆盖手机上的旧副本。 */
export async function importOfflineRecord(
  record: OfflineRecord,
): Promise<OfflineSummary> {
  await putRecord(record);
  return summarize(record);
}

/**
 * 本机翻译写回：只更新这篇的 blocks 与目标语言，不动图片和 PDF。
 * 没有离线副本时静默忽略（说明用户只是在线阅读）。
 */
export async function saveOfflineBlocks(
  paperId: string,
  blocks: unknown[],
  translationLang?: string,
): Promise<void> {
  const record = await getRecord(paperId);
  if (!record) return;
  record.payload = {
    ...record.payload,
    blocks,
    ...(translationLang ? { translation_lang: translationLang } : {}),
  };
  record.bytes = recordBytes(record);
  record.updatedAt = Date.now();
  await putRecord(record);
}

/** 从 StoryReader 导入的书：直接写入离线库，阅读时和下载的论文走同一条路径。 */
export async function saveLocalDocument(
  id: string,
  payload: Record<string, unknown> & { blocks?: unknown[] },
): Promise<OfflineSummary> {
  const record: OfflineRecord = {
    id,
    title: String(payload.title ?? ''),
    authors: String(payload.authors ?? ''),
    payload,
    pdf: null,
    thumb: null,
    assets: [],
    bytes: new Blob([JSON.stringify(payload)]).size,
    updatedAt: Date.now(),
    source: 'storyreader',
  };
  await putRecord(record);
  return summarize(record);
}

export async function listOffline(): Promise<OfflineSummary[]> {
  const db = await openDb();
  try {
    const records = await new Promise<OfflineRecord[]>((resolve, reject) => {
      const request = db.transaction(STORE, 'readonly').objectStore(STORE).getAll();
      request.onsuccess = () => resolve((request.result as OfflineRecord[]) ?? []);
      request.onerror = () => reject(request.error ?? new Error('读取离线库失败'));
    });
    return records
      .map(summarize)
      .sort((a, b) => b.updatedAt - a.updatedAt);
  } finally {
    db.close();
  }
}

export async function hasOffline(paperId: string): Promise<boolean> {
  try {
    return (await getRecord(paperId)) !== null;
  } catch {
    return false;
  }
}

const urlCache = new Map<string, string[]>();

function revoke(paperId: string): void {
  for (const url of urlCache.get(paperId) ?? []) URL.revokeObjectURL(url);
  urlCache.delete(paperId);
}

/** 读本地论文；图片路径换成 blob URL，返回的 pdfUrl 需要调用方负责回收。 */
export async function readOffline(
  paperId: string,
): Promise<{
  paper: Paper;
  pdfUrl: string | null;
  thumbUrl: string | null;
  pageUrls: Record<number, string>;
  source: 'paper' | 'storyreader';
} | null> {
  const record = await getRecord(paperId);
  if (!record) return null;

  revoke(paperId);
  const urls: string[] = [];
  const assetUrls = new Map<string, string>();
  const pageUrls: Record<number, string> = {};
  for (const asset of record.assets) {
    const url = URL.createObjectURL(asset.blob);
    assetUrls.set(asset.path, url);
    urls.push(url);
    const page = /\/page\/(\d+)\.png/.exec(asset.path);
    if (page) pageUrls[Number(page[1])] = url;
  }
  let pdfUrl: string | null = null;
  if (record.pdf) {
    pdfUrl = URL.createObjectURL(record.pdf);
    urls.push(pdfUrl);
  }
  let thumbUrl: string | null = null;
  if (record.thumb) {
    thumbUrl = URL.createObjectURL(record.thumb);
    urls.push(thumbUrl);
  }
  urlCache.set(paperId, urls);

  const data = record.payload as Record<string, unknown>;
  const paper: Paper = {
    paper_id: paperId,
    title: String(data.title ?? ''),
    title_zh: String(data.title_zh ?? ''),
    authors: String(data.authors ?? ''),
    affiliation: String(data.affiliation ?? ''),
    page_count: Number(data.page_count ?? 0),
    page_sizes: (data.page_sizes as Paper['page_sizes']) ?? [],
    columns: Number(data.columns ?? 1),
    glossary: (data.glossary as Paper['glossary']) ?? {},
    translation_lang: String(data.translation_lang ?? ''),
    progress: (data.progress as Paper['progress']) ?? { pct: 0, block_id: '', page: 0 },
    blocks: ((data.blocks as Paper['blocks']) ?? []).map((block) => {
      const path = (block as { image_path?: string | null }).image_path;
      if (!path) return block;
      const local = assetUrls.get(path);
      return local ? ({ ...block, image_path: local } as Paper['blocks'][number]) : block;
    }),
  };

  return {
    paper,
    pdfUrl,
    thumbUrl,
    pageUrls,
    source: record.source === 'storyreader' ? 'storyreader' : 'paper',
  };
}

export async function deleteOffline(paperId: string): Promise<void> {
  const db = await openDb();
  try {
    const tx = db.transaction(STORE, 'readwrite');
    tx.objectStore(STORE).delete(paperId);
    await finish(tx);
  } finally {
    db.close();
  }
  revoke(paperId);
}

export async function offlineUsage(): Promise<{ count: number; bytes: number }> {
  const items = await listOffline();
  return {
    count: items.length,
    bytes: items.reduce((sum, item) => sum + item.bytes, 0),
  };
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

// ── 本机问答记录：服务端不可达时，对话只存在手机上 ──────────────

export interface LocalChatTurn {
  id: string;
  question: string;
  answer: string;
  sources: unknown[];
  usedWeb: boolean;
  latency?: number;
  at: number;
}

interface ChatRecord {
  paperId: string;
  turns: LocalChatTurn[];
}

const CHAT_LIMIT = 100;

export async function loadLocalChat(paperId: string): Promise<LocalChatTurn[]> {
  const db = await openDb();
  try {
    const record = await new Promise<ChatRecord | undefined>((resolve, reject) => {
      const request = db.transaction(CHAT_STORE, 'readonly').objectStore(CHAT_STORE).get(paperId);
      request.onsuccess = () => resolve(request.result as ChatRecord | undefined);
      request.onerror = () => reject(request.error ?? new Error('读取本机对话失败'));
    });
    return record?.turns ?? [];
  } finally {
    db.close();
  }
}

export async function appendLocalChat(paperId: string, turn: LocalChatTurn): Promise<void> {
  const turns = await loadLocalChat(paperId);
  turns.push(turn);
  const db = await openDb();
  try {
    const tx = db.transaction(CHAT_STORE, 'readwrite');
    tx.objectStore(CHAT_STORE).put({ paperId, turns: turns.slice(-CHAT_LIMIT) });
    await finish(tx);
  } finally {
    db.close();
  }
}

export async function clearLocalChat(paperId: string): Promise<void> {
  const db = await openDb();
  try {
    const tx = db.transaction(CHAT_STORE, 'readwrite');
    tx.objectStore(CHAT_STORE).delete(paperId);
    await finish(tx);
  } finally {
    db.close();
  }
}

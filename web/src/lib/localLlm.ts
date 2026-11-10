/**
 * 手机端直连大模型的流式通道。
 *
 * 请求由 Rust 侧发出（WebView 里 fetch 会被厂商接口的 CORS 拦），
 * 增量通过 Tauri Channel 逐段回传，用法与流式 fetch 一致。
 */
import { Channel, invoke } from '@tauri-apps/api/core';

export interface LocalLlmConfig {
  baseUrl: string;
  apiKey: string;
  model: string;
}

export interface LocalChatMessage {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

interface LlmEvent {
  type: 'delta' | 'done' | 'error';
  text?: string;
  message?: string;
}

export interface StreamLocalChatOptions {
  config: LocalLlmConfig;
  messages: LocalChatMessage[];
  onDelta: (text: string) => void;
  temperature?: number;
  maxTokens?: number;
  signal?: AbortSignal;
}

function isTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

/** 是否具备本机直连模型的能力（只有安卓/iOS 壳里才有 Rust 通道）。 */
export function canStreamLocal(): boolean {
  return isTauri();
}

/**
 * 流式调用 OpenAI 兼容接口，返回完整文本。
 * 出错时抛出带可读信息的 Error，并已把错误推到 onDelta 之外。
 */
export async function streamLocalChat(options: StreamLocalChatOptions): Promise<string> {
  if (!isTauri()) {
    throw new Error('当前环境不支持本地模型通道');
  }

  const id = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `llm-${Date.now()}-${Math.random().toString(16).slice(2)}`;

  let full = '';
  let failure: string | null = null;
  const channel = new Channel<LlmEvent>();
  channel.onmessage = (event) => {
    if (event.type === 'delta' && event.text) {
      full += event.text;
      options.onDelta(event.text);
    } else if (event.type === 'error') {
      failure = event.message || '模型调用失败';
    }
  };

  const onAbort = () => {
    void invoke('llm_cancel', { id }).catch(() => undefined);
  };
  options.signal?.addEventListener('abort', onAbort, { once: true });

  try {
    await invoke('llm_stream', {
      request: {
        id,
        baseUrl: options.config.baseUrl,
        apiKey: options.config.apiKey,
        model: options.config.model,
        messages: options.messages,
        temperature: options.temperature,
        maxTokens: options.maxTokens,
      },
      onEvent: channel,
    });
  } finally {
    options.signal?.removeEventListener('abort', onAbort);
  }

  if (failure) {
    throw new Error(failure);
  }
  if (options.signal?.aborted) {
    throw new DOMException('已停止生成', 'AbortError');
  }
  return full;
}

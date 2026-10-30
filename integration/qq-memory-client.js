// QQ-Bridge ↔ qq-bridge-memory sidecar 客户端（Step 4 草案）
//
// 落点：本文件最终放到 qq-bridge/src/qq-memory-client.js，由 bridge.js import。
// 约束（超时、降级、开关、注入方式四类硬要求）：
//   · 硬超时 300ms（AbortController），超时/非 2xx/JSON 失败一律当作"没有记忆"；
//   · 不重试、不抛错、不阻塞主链路；失败日志同一错误 1 分钟最多 1 条；
//   · QQ_MEMORY=0（默认）时所有方法都是空操作，随时可回滚；
//   · 只做「取一段文本拼进 prompt」和「把消息写进库」，不改会话结构与发送链路。
//
// 会话轮换：
//   · 只在 turn/end 且回合成功结束后计数；
//   · 最少 50 轮保护；>80 轮 或 连续 3 轮 inputTokens 均值 > 窗口 60% 触发；
//   · 延迟 5s 执行（等本回合回复发送完），执行前再确认会话空闲、仍是同一 session；
//   · 先取续接包（失败用「未能保存」兜底），再 retireSession；generation + 1；
//   · 新会话首轮把缓存的续接包注入 prompt 一次。

import fs from 'node:fs';
import path from 'node:path';

const DEFAULT_TIMEOUT_MS = 300;
const DEFAULT_CONTEXT_TOKENS = 131072;
const DEFAULT_ROTATE_AT = 0.6;
const DEFAULT_MIN_TURNS = 50;
const DEFAULT_MAX_TURNS = 80;
const DEFAULT_ROTATE_DELAY_MS = 5000;
const DEFAULT_HANDOFF_LIMIT = 20;
const ERROR_LOG_COOLDOWN_MS = 60000;
const PENDING_HANDOFF_TTL_MS = 10 * 60 * 1000;

const FALLBACK_HANDOFF = [
  '【上一段对话（接着聊，不是新对话）】',
  '下面是你和对方刚才的对话。你是同一个人，语气和话题都要接上。',
  '不要向对方提起"上下文/记忆/会话"这些词，也不要复述这段摘要。',
  '',
  '（上一段对话未能保存，请自然接住对方的话。）',
  '【上一段对话结束】'
].join('\n');

function envInt(name, fallback, min, max) {
  const n = Number(process.env[name]);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, Math.round(n)));
}

function envFloat(name, fallback, min, max) {
  const n = Number(process.env[name]);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

function scopeOfKey(key) {
  const [kind, id] = String(key || '').split(':');
  if (kind === 'private') return { type: 'private', user_id: String(id || ''), group_id: '' };
  if (kind === 'group') return { type: 'group', user_id: '', group_id: String(id || '') };
  return null;
}

function usageOfEvent(event) {
  if (event?.type === 'assistant/message' && event.data?.usage !== undefined) return event.data.usage;
  const stream = event?.data?.stream;
  if (Array.isArray(stream)) {
    for (let i = stream.length - 1; i >= 0; i -= 1) {
      const chunk = stream[i];
      if (chunk && typeof chunk === 'object' && chunk.usage !== undefined) return chunk.usage;
    }
  }
  return null;
}

function inputTokensOf(usage) {
  const pick = (...keys) => {
    for (const key of keys) {
      const value = Number(usage?.[key]);
      if (Number.isFinite(value) && value >= 0) return value;
    }
    return null;
  };
  const miss = pick('cacheMiss', 'uncachedInputTokens', 'inputTokens');
  if (miss === null) return null;
  const hit = pick('cacheHit', 'cacheReadTokens') ?? 0;
  const write = pick('cacheWrite', 'cacheWriteTokens') ?? 0;
  const total = miss + hit + write;
  return total > 0 ? total : null;
}

function toIsoTime(value) {
  if (value == null || value === '') return new Date().toISOString();
  if (typeof value === 'string' && /[T-]/.test(value)) {
    const parsed = new Date(value);
    return Number.isFinite(parsed.getTime()) ? parsed.toISOString() : new Date().toISOString();
  }
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return new Date().toISOString();
  // OneBot 的 time 是秒；也容忍毫秒时间戳。
  return new Date(n < 1e12 ? n * 1000 : n).toISOString();
}

function latestUserText(state) {
  for (const list of [state?.unread, state?.recentMessages]) {
    const arr = Array.isArray(list) ? list : [];
    for (let i = arr.length - 1; i >= 0; i -= 1) {
      const msg = arr[i];
      if (!msg || msg.isSelf) continue;
      const text = String(msg.plain || msg.text || '').trim();
      if (text) return text.slice(0, 200);
    }
  }
  return '';
}

/**
 * 最近几轮对话的压缩上下文：只喂给「短查询召回」用。
 * 为什么不直接用 state 里全部消息：prompt 预算有限，200 字足够让检索拿到话题和事件主体。
 */
function recentContext(state, limit = 4) {
  const parts = [];
  for (const msg of Array.isArray(state?.recentMessages) ? state.recentMessages.slice(-limit) : []) {
    const text = String(msg?.plain || msg?.text || '').trim();
    if (text) parts.push((msg?.isSelf ? '我：' : '对方：') + text);
  }
  return parts.join(' ').slice(-240);
}

function recentMessageIds(state, limit = 20) {
  const ids = [];
  for (const list of [state?.unread, state?.recentMessages]) {
    for (const msg of Array.isArray(list) ? list : []) {
      if (msg?.messageId != null) ids.push(String(msg.messageId));
    }
  }
  return [...new Set(ids)].slice(-limit);
}

export function createMemoryClient({ log = () => {}, stateFile = '', accountProvider = () => '' } = {}) {
  const enabled = String(process.env.QQ_MEMORY ?? '0') === '1';
  const rotateEnabled = String(process.env.QQ_MEMORY_ROTATE ?? '1') !== '0';
  const base = String(process.env.QQ_MEMORY_BASE || 'http://127.0.0.1:8787').replace(/\/+$/, '');
  const token = String(process.env.QQ_MEMORY_TOKEN || '');
  const timeoutMs = envInt('QQ_MEMORY_TIMEOUT_MS', DEFAULT_TIMEOUT_MS, 50, 3000);
  const contextTokens = envInt('QQ_MEMORY_CONTEXT_TOKENS', DEFAULT_CONTEXT_TOKENS, 8192, 2000000);
  const rotateAt = envFloat('QQ_MEMORY_ROTATE_AT', DEFAULT_ROTATE_AT, 0.3, 0.9);
  const minTurns = envInt('QQ_MEMORY_MIN_TURNS', DEFAULT_MIN_TURNS, 1, 100000);
  const maxTurns = envInt('QQ_MEMORY_MAX_TURNS', DEFAULT_MAX_TURNS, 2, 100000);
  const rotateDelayMs = envInt('QQ_MEMORY_ROTATE_DELAY_MS', DEFAULT_ROTATE_DELAY_MS, 0, 60000);
  const handoffLimit = envInt('QQ_MEMORY_HANDOFF_LIMIT', DEFAULT_HANDOFF_LIMIT, 5, 40);

  const clientState = loadState();
  const pendingHandoffs = new Map(); // key -> { fromSessionId, block, at }
  const turnCounts = new Map(); // key -> completed turns
  const usageByTurn = new Map(); // sessionId -> Map(turn -> inputTokens)
  const rotateTimers = new Map(); // key -> timer
  const errorAt = new Map();

  function loadState() {
    const fallback = { generations: {} };
    if (!stateFile) return fallback;
    try {
      const raw = JSON.parse(String(fs.readFileSync(stateFile, 'utf8')));
      const generations = raw && typeof raw.generations === 'object' && raw.generations ? raw.generations : {};
      const account = typeof raw?.account === 'string' ? raw.account : '';
      return { generations: { ...generations }, account };
    } catch {
      return fallback;
    }
  }

  function persist() {
    if (!stateFile) return;
    try {
      const dir = path.dirname(stateFile);
      fs.mkdirSync(dir, { recursive: true });
      const tmp = `${stateFile}.tmp`;
      fs.writeFileSync(tmp, JSON.stringify(clientState), 'utf8');
      fs.renameSync(tmp, stateFile);
    } catch (error) {
      logOnce('persist', `[memory] 写入状态失败：${error?.message ?? error}`);
    }
  }

  function logOnce(op, message) {
    const now = Date.now();
    if (now - (errorAt.get(op) ?? 0) < ERROR_LOG_COOLDOWN_MS) return;
    errorAt.set(op, now);
    try { log(message); } catch { /* 日志本身失败也不能影响主链路 */ }
  }

  function generation(key) {
    const n = Number(clientState.generations[key]);
    return Number.isFinite(n) && n > 0 ? n : 0;
  }

  function bumpGeneration(key) {
    clientState.generations[key] = generation(key) + 1;
    persist();
  }

  async function httpJson(method, pathname, body, budgetMs = timeoutMs, attempt = 1) {
    if (!enabled) return null;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), budgetMs);
    try {
      const headers = { 'content-type': 'application/json' };
      if (token) headers['x-memory-token'] = token;
      const response = await fetch(base + pathname, {
        method,
        headers,
        signal: controller.signal,
        body: body === undefined ? undefined : JSON.stringify(body)
      });
      if (!response.ok) {
        logOnce(`${method} ${pathname}`, `[memory] ${pathname} HTTP ${response.status}，按无记忆处理`);
        return null;
      }
      return await response.json();
    } catch (error) {
      // undici 复用 keep-alive 连接：sidecar 一重启，旧连接会以 TypeError 立刻失败，
      // 整段记忆就此静默失效（2026-10-28 12:48 起的那次中断就是这个）。换条新连接重试一次即可自愈。
      if (attempt === 1 && error?.name === 'TypeError') {
        return await httpJson(method, pathname, body, budgetMs, 2);
      }
      const cause = error?.cause?.code ?? error?.cause?.message ?? '';
      logOnce(`${method} ${pathname}`, `[memory] ${pathname} 失败（${error?.name ?? 'Error'}${cause ? ':' + cause : ''}），按无记忆处理`);
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  function account() {
    try {
      const value = String(accountProvider() ?? '').trim();
      if (value) {
        if (clientState.account !== value) {
          clientState.account = value;
          persist();
        }
        return value;
      }
    } catch {
      // provider 抛错同样走回退，不能影响主链路。
    }
    // 拿不到机器人 QQ 号时（SnowLuma 刚重连、get_login_info 还没回来），
    // 老实现直接返回空 → buildScope 为 null → 写入/召回/续接全部**静默跳过**。
    // 这里回退到上次成功的账号，让记忆在这段窗口里继续工作。
    const cached = String(clientState.account ?? '').trim();
    if (cached) {
      logOnce('account-fallback', `[memory] 拿不到机器人 QQ 号，回退到上次已知账号 ${cached.slice(0, 4)}…（记忆继续工作）`);
      return cached;
    }
    logOnce('account-missing', '[memory] 拿不到机器人 QQ 号且无历史账号可回退：本轮记忆读写被跳过（检查 get_login_info / SnowLuma 连接）');
    return '';
  }

  function buildScope(key) {
    const scope = scopeOfKey(key);
    const acc = account();
    if (!scope || !acc) return null;
    return { account: acc, scope, key };
  }

  function writeIncoming({ key, userId, sender, text, messageId, happenedAt, forced = false, sessionId = null }) {
    if (!enabled) return;
    try {
      const target = buildScope(key);
      const content = String(text ?? '').trim();
      if (!target || !content) return;
      const body = {
        account: target.account,
        scope: target.scope,
        role: 'user',
        speaker: { id: userId != null ? String(userId) : '', name: String(sender ?? '') },
        content,
        happened_at: toIsoTime(happenedAt),
        source: 'auto',
        meta: { trigger: forced ? 'wake' : 'passive' },
        session_id: sessionId || undefined,
        generation: generation(key)
      };
      if (messageId != null) body.message_id = String(messageId);
      void httpJson('POST', '/v1/write', body);
    } catch (error) {
      logOnce('write-incoming', `[memory] 写入用户消息失败（已忽略）：${error?.message ?? error}`);
    }
  }

  function writeAssistant({ key, texts, sessionId = null }) {
    if (!enabled) return;
    try {
      const target = buildScope(key);
      if (!target) return;
      for (const text of Array.isArray(texts) ? texts : []) {
        const content = String(text ?? '').trim();
        if (!content) continue;
        void httpJson('POST', '/v1/write', {
          account: target.account,
          scope: target.scope,
          role: 'assistant',
          speaker: { id: target.account, name: '我' },
          content,
          source: 'auto',
          meta: { trigger: 'passive' },
          session_id: sessionId || undefined,
          generation: generation(key)
        });
      }
    } catch (error) {
      logOnce('write-assistant', `[memory] 写入助手消息失败（已忽略）：${error?.message ?? error}`);
    }
  }

  function recallBlock(key, query, excludeIds = [], context = '') {
    const target = buildScope(key);
    if (!target || !query) return Promise.resolve('');
    return httpJson('POST', '/v1/recall', {
      account: target.account,
      scope: target.scope,
      query,
      // 短查询（“大后天呢”“出去”）带上最近几轮对话，服务端在这些词太少时会把上下文拼进检索文本。
      ...(context ? { context } : {}),
      limit: 6,
      max_chars: 800,
      exclude_ids: Array.isArray(excludeIds) ? excludeIds : []
    }).then((json) => String(json?.block || ''));
  }

  function fetchHandoff(key) {
    const target = buildScope(key);
    if (!target) return Promise.resolve(null);
    const scopeParam = target.scope.type === 'private'
      ? `private:${target.scope.user_id}`
      : `group:${target.scope.group_id}`;
    const qs = new URLSearchParams({
      account: target.account,
      scope: scopeParam,
      limit: String(handoffLimit)
    });
    return httpJson('GET', `/v1/handoff?${qs.toString()}`).then((json) => {
      if (!json || !json.ok) return null;
      const block = String(json.block || '');
      if (!block) return null;
      return { block, chars: Number(json.chars) || block.length, degraded: !!json.degraded };
    });
  }

  function observeEvent(sessionId, event) {
    if (!enabled) return;
    if (!sessionId || event?.type !== 'assistant/message') return;
    const usage = usageOfEvent(event);
    if (!usage) return;
    const input = inputTokensOf(usage);
    if (input === null) return;
    const turn = Number(event.data?.turn);
    if (!Number.isFinite(turn)) return;
    let samples = usageByTurn.get(sessionId);
    if (!samples) {
      samples = new Map();
      usageByTurn.set(sessionId, samples);
    }
    samples.set(turn, input);
    while (samples.size > 5) samples.delete(samples.keys().next().value);
  }

  function averageInputTokens(sessionId) {
    const samples = usageByTurn.get(sessionId);
    if (!samples || samples.size < 3) return null;
    const values = [...samples.values()].slice(-3);
    return values.reduce((sum, value) => sum + value, 0) / values.length;
  }

  function noteTurnEnd({ key, sessionId, reasonKind, isBusy, isCurrent, rotate }) {
    if (!enabled) return;
    if (reasonKind !== 'completed') return;
    turnCounts.set(key, (turnCounts.get(key) ?? 0) + 1);
    if (!rotateEnabled) return;

    const turns = turnCounts.get(key) ?? 0;
    if (turns < minTurns) return;

    const avg = averageInputTokens(sessionId);
    const byTokens = avg !== null && avg > contextTokens * rotateAt;
    const byTurns = turns > maxTurns;
    if (!byTokens && !byTurns) return;
    if (rotateTimers.has(key)) return;

    const reason = byTokens
      ? `tokens ${Math.round(avg)} > ${Math.round(contextTokens * rotateAt)} (${Math.round(rotateAt * 100)}%)`
      : `turns ${turns} > ${maxTurns}`;

    const timer = setTimeout(async () => {
      rotateTimers.delete(key);
      try {
        if (typeof isCurrent === 'function' && !isCurrent()) return;
        if (typeof isBusy === 'function' && isBusy()) {
          log(`[memory] ${key} 轮换延后：会话仍忙（${reason}）`);
          return;
        }
        const handoff = await fetchHandoff(key);
        // 取续接包的 300ms 里可能已经有新回合开始：落刀前必须再确认一次。
        if (typeof isCurrent === 'function' && !isCurrent()) return;
        if (typeof isBusy === 'function' && isBusy()) {
          log(`[memory] ${key} 轮换延后：取续接包期间会话重新变忙`);
          return;
        }
        const block = handoff?.block || FALLBACK_HANDOFF;
        pendingHandoffs.set(key, { fromSessionId: sessionId, block, at: Date.now() });
        bumpGeneration(key);
        usageByTurn.delete(sessionId);
        turnCounts.set(key, 0);
        const nextGen = generation(key);
        if (typeof rotate === 'function') rotate();
        log(`[memory] 会话轮换 ${key}：原因=${reason}；续接包=${block.length} 字${handoff ? '' : '（降级兜底）'}；旧 session=${sessionId}；generation=${nextGen}`);
      } catch (error) {
        logOnce('rotate', `[memory] 轮换失败：${error?.message ?? error}`);
      }
    }, rotateDelayMs);
    timer.unref?.();
    rotateTimers.set(key, timer);
  }

  async function buildWakeExtras({ key, sessionId, wakeReason, state }) {
    const extras = { handoff: '', memory: '' };
    if (!enabled) return extras;
    try {
      const pending = pendingHandoffs.get(key);
      if (pending) {
        if (pending.fromSessionId !== sessionId) {
          pendingHandoffs.delete(key);
          extras.handoff = pending.block;
        } else if (Date.now() - pending.at > PENDING_HANDOFF_TTL_MS) {
          pendingHandoffs.delete(key);
        }
      }

      if (wakeReason) {
        const query = latestUserText(state);
        if (query) {
          extras.memory = await recallBlock(key, query, recentMessageIds(state), recentContext(state));
        }
      }
      return extras;
    } catch (error) {
      logOnce('wake-extras', `[memory] 组装注入块失败（已忽略）：${error?.message ?? error}`);
      return extras;
    }
  }

  function forget(key) {
    pendingHandoffs.delete(key);
    turnCounts.delete(key);
    const timer = rotateTimers.get(key);
    if (timer) {
      clearTimeout(timer);
      rotateTimers.delete(key);
    }
  }

  return {
    get enabled() { return enabled; },
    generation,
    writeIncoming,
    writeAssistant,
    observeEvent,
    noteTurnEnd,
    buildWakeExtras,
    forget
  };
}

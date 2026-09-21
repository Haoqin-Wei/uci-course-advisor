// Match app/response_language.py; neutral input inherits USER language only.
function detectResponseLanguage(message, previous = 'en') {
  if (/[\u3400-\u9fff]/u.test(message)) return 'zh';
  const prose = String(message || '').replace(/\b(?:[A-Za-z&]+\d+[A-Za-z]*|[A-Z&]+(?:[ \t]+[A-Z&]+){0,2}[ \t]+\d+[A-Za-z]*|\d+)\b/g, '');
  return /[A-Za-z]/u.test(prose) ? 'en' : previous;
}

function responseTerm(term, language) {
  if (language !== 'zh') return term;
  const seasons = {Fall: '秋季', Winter: '冬季', Spring: '春季', Summer: '夏季',
    Summer1: '夏季第一期', Summer2: '夏季第二期', Summer10wk: '夏季十周'};
  const match = canonicalTerm(term).match(/^(\d{4})\s+(\w+)$/u);
  return match && seasons[match[2]] ? `${match[1]}年${seasons[match[2]]}` : term;
}

/* ── Send / Stop ───────────────────────────────────── */
async function sendMessage(text) {
  const msg = text || inputEl.value.trim();
  if (!msg) return;
  if (currentAbortController) return;   // already generating

  const epoch = conversationEpoch;
  const welcomeSend = beginWelcomeSend();
  if (!currentSessionId) setThreadTitle(Array.from(msg).slice(0, 34).join(''));
  inputEl.value = '';
  syncComposer();

  const userMessage = appendUser(msg);
  toggleSendStop(true);

  const controller = new AbortController();
  currentAbortController = controller;

  // Round 4 — captured BEFORE the request so we can decide in `finally`
  // whether to re-poll the sidebar for the LLM-generated title. The
  // backend only runs `_auto_title_session` (BackgroundTask) on the
  // first turn of a brand-new session; for follow-ups the title is
  // already user-meaningful and we skip the extra fetches.
  const isNewSession = !currentSessionId;

  // Build request payload — send currentSessionId or "" for "new session"
  const customPrompt = getActivePrompt();
  const payload = {
    message: msg,
    session_id: currentSessionId || '',
  };
  if (customPrompt) payload.system_prompt = customPrompt;

  // Create the AI message bubble up-front; tokens stream into its body.
  currentResponseLanguage = detectResponseLanguage(msg, currentResponseLanguage);
  const aiMsg = startAiMessage(currentResponseLanguage);
  animateWelcomeSend(welcomeSend, userMessage, aiMsg);
  aiMsg._queryKind = /推荐|recommend|suggest|what (?:courses|classes).*take/iu.test(msg)
    ? 'recommendation'
    : /比较|对比|compare|comparison|versus|\bvs\.?\b/iu.test(msg) ? 'comparison' : 'query';

  let fullText = '';
  let meta = null;
  let stopped = false;

  try {
    const res = await fetch(`${API}/api/chat/stream`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }

    await consumeSSE(res, {
      token(event) {
        fullText += event.text;
        appendStreamingToken(aiMsg, fullText);
      },
      meta(event) {
        if (epoch !== conversationEpoch) return;
        meta = event;
        if (event.response_language) aiMsg._responseLanguage = event.response_language;
        markUserDelivery(userMessage, true);
        applyTermPayload(event);
        // Phase 3 R3 — backend's authoritative session_id. If this
        // is the first turn (we were sending "" before), capture
        // the new sess_XXXXXX so subsequent turns route to the
        // same session, and refresh the sidebar list.
        if (event.session_id && event.session_id !== currentSessionId) {
          currentSessionId = event.session_id;
        }
      },
      tool_call_start(event) {
        // Agent loop dispatched a tool — show a status chip in the
        // AI bubble so the user sees what's happening during the
        // silent stretch before the answer streams.
        startToolChip(aiMsg, event.label || event.name, event.name, event.response_language);
      },
      tool_call_done(event) {
        finishToolChip(
          aiMsg,
          event.ok !== false,
          event.fetch_summary || [],
          event,
        );
      },
      limit_reached(event) {
        // Agent hit its budget. Stash the continuation_id on the
        // message so finalizeAiMessage can render a Continue button
        // after the fallback answer finishes streaming.
        console.log('[chat] limit_reached:', event);
        aiMsg._continuationId = event.continuation_id;
        aiMsg._limitReason    = event.reason;
        if (event.response_language) aiMsg._responseLanguage = event.response_language;
      },
      error(event) {
        console.error('Stream error event:', event.message);
        fullText += aiMsg._responseLanguage === 'zh'
          ? '\n\n_暂时无法完成回复，请重试。_' : '\n\n_Unable to complete the response. Please try again._';
        appendStreamingToken(aiMsg, fullText); // re-render with error suffix
      },
      done() {
        // explicit termination — finalize below
      },
      parseError(payload) {
        console.warn('SSE parse error:', payload);
      },
    });
  } catch (err) {
    if (err.name === 'AbortError') {
      stopped = true;
      fullText += (fullText ? '\n\n' : '') + (aiMsg._responseLanguage === 'zh' ? '_已停止生成_' : '_(generation stopped)_');
    } else {
      console.error('chat stream failed:', err);
      fullText = fullText || (aiMsg._responseLanguage === 'zh' ? '无法连接，请重试。' : 'Unable to connect. Please try again.');
      markUserDelivery(userMessage, false);
    }
  } finally {
    if (epoch !== conversationEpoch) return;
    const finalText = (!stopped && meta && typeof meta.final_answer === 'string')
      ? meta.final_answer
      : fullText;
    finalizeAiMessage(aiMsg, finalText, meta || {}, stopped);
    currentAbortController = null;
    toggleSendStop(false);
    syncComposer();
    inputEl.focus();
    // Channel A may have updated profile.json (e.g. user mentioned a
    // newly-completed course). Re-fetch the sidebar so the UI stays in
    // sync with the durable profile state.
    loadSidebar(true);
    loadScheduleForSession(currentSessionId);

    // Round 4 — _persist_turn ran during the stream and set the snippet
    // title; the LLM auto-title BackgroundTask only fires *after* the
    // SSE stream closes (FastAPI runs BackgroundTasks post-response).
    // Refresh now to lock in the snippet, then retry a few times to
    // pick up the LLM title once DeepSeek returns — observed wall-time
    // ranges from <1s to ~6s, so a single 2s timeout misses the long
    // tail. Each fetch is ~1 KB so 3 retries cost ~3 KB total. Skipped
    // on stop because CancelledError bypasses persist entirely.
    if (isNewSession && !stopped) {
      loadSessionList();
      setTimeout(loadSessionList, 7000);
    }
  }
}

function stopGeneration() {
  if (currentAbortController) {
    currentAbortController.abort();
  }
}

function toggleSendStop(isGenerating) {
  const sendBtn = document.getElementById('sendBtn');
  const stopBtn = document.getElementById('stopBtn');
  if (isGenerating) {
    sendBtn.style.display = 'none';
    stopBtn.style.display = 'inline-flex';
    sendBtn.disabled = true;
  } else {
    sendBtn.style.display = '';
    stopBtn.style.display = 'none';
    sendBtn.disabled = !inputEl.value.trim();
  }
}

/* ── AI message bubble: streaming + finalize ──────── */
function startAiMessage(language = currentResponseLanguage) {
  const wrap = document.createElement('div');
  wrap.className = 'msg msg-ai';
  wrap._responseLanguage = language;
  // Initial state: shimmer "Thinking" until first token arrives.
  wrap.innerHTML = `<div class="msg-ai-label">${solonIcon('sparkles')}Solon</div>`
                 + `<div class="msg-ai-body thinking" aria-label="${language === 'zh' ? 'Solon 正在思考' : 'Solon is thinking'}"><span class="tdot"></span><span class="tdot"></span><span class="tdot"></span></div>`;
  document.getElementById('chatScroll').appendChild(wrap);
  scrollChat();
  // Per-message render scheduler — collapses bursts of tokens into one
  // markdown render per animation frame (~16ms).
  wrap._pendingFrame = null;
  wrap._pendingText  = '';
  wrap._gotFirstToken = false;
  // Pending agent tool-call chips: FIFO of {el} as start events arrive,
  // popped as done events arrive. Sequential because loop.py dispatches
  // tools one at a time per assistant message.
  wrap._toolChipQueue = [];
  wrap._webFetchRendered = false;
  return wrap;
}

function startToolChip(wrap, label, toolName, language) {
  if (language) wrap._responseLanguage = language;
  // Lazy-create the chips container above the body. We don't want it
  // on every AI message — only when the agent actually uses tools.
  let container = wrap.querySelector(':scope > .tool-calls');
  if (!container) {
    container = document.createElement('div');
    container.className = 'tool-calls';
    const body = wrap.querySelector('.msg-ai-body');
    wrap.insertBefore(container, body);
  }
  const chip = document.createElement('div');
  chip.className = 'tool-chip is-active';
  if (toolName) {
    chip.dataset.toolName = toolName;
    if (toolName === 'web_search') chip.classList.add('tool-chip-web-search');
    if (toolName === 'get_live_sections') chip.classList.add('tool-chip-live-websoc');
    if (toolName === 'get_department_restrictions') {
      chip.classList.add('tool-chip-websoc-comments');
    }
  }
  chip.innerHTML = '<span class="tool-dot"></span><span class="tool-label"></span>';
  chip.querySelector('.tool-label').textContent = label || (wrap._responseLanguage === 'zh' ? '调用工具' : 'Running tool');
  container.appendChild(chip);
  wrap._toolChipQueue.push(chip);
  scrollChat();
}

function finishToolChip(wrap, ok, fetchSummary, resultMeta = {}) {
  const zh = wrap._responseLanguage === 'zh';
  const chip = (wrap._toolChipQueue || []).shift();
  if (chip) {
    chip.classList.remove('is-active');
    chip.classList.add(ok ? 'is-done' : 'is-error');
    if (chip.dataset.toolName === 'get_sections') {
      const label = chip.querySelector('.tool-label');
      const count = Number(resultMeta.section_count || 0);
      if (count > 0) {
        chip.classList.add('is-sections-found');
        if (label) label.textContent += zh ? ` · 找到 ${count} 个教学班` : ` · ${count} sections found`;
      } else if (
        resultMeta.offering_status === 'not_offered'
        && resultMeta.authoritative === true
      ) {
        chip.classList.add('is-official-no-match');
        if (label) label.textContent += zh ? ' · 官方无匹配' : ' · No official match';
      } else if (resultMeta.offering_status === 'not_offered') {
        chip.classList.add('is-no-match');
        if (label) label.textContent += zh ? ' · 未找到教学班' : ' · No sections found';
      } else if (resultMeta.offering_status === 'unavailable') {
        chip.classList.remove('is-done');
        chip.classList.add('is-unavailable');
        if (label) label.textContent += zh ? ' · 数据不可用' : ' · Data unavailable';
      }
    }
  }
  if (Array.isArray(fetchSummary) && fetchSummary.length > 0) {
    renderWebFetchSummary(wrap, fetchSummary);
    wrap._webFetchRendered = true;
  }
}

function renderWebFetchSummary(wrap, fetches) {
  if (!wrap || !Array.isArray(fetches) || fetches.length === 0) return;
  const zh = wrap._responseLanguage === 'zh';

  let container = wrap.querySelector(':scope > .tool-calls');
  if (!container) {
    container = document.createElement('div');
    container.className = 'tool-calls';
    const body = wrap.querySelector('.msg-ai-body');
    wrap.insertBefore(container, body);
  }

  const previous = container.querySelector(':scope > .tool-fetch-details');
  if (previous) previous.remove();

  const details = document.createElement('details');
  details.className = 'tool-fetch-details';
  const summary = document.createElement('summary');
  summary.textContent = zh ? `实际抓取 ${fetches.length} 个请求` : `${fetches.length} web ${fetches.length === 1 ? 'request' : 'requests'}`;
  details.appendChild(summary);

  const list = document.createElement('div');
  list.className = 'tool-fetch-list';
  const roleLabels = zh ? {
    registrar_websoc_form: 'WebSoc 查询表单',
    registrar_websoc_results: 'WebSoc 部门结果',
    registrar_websoc_course_results: 'WebSoc 课程结果',
    undergrad_restrictions: '本科限制官方页',
    graduate_restrictions: '研究生限制官方页',
    policy: '政策官方页',
    restriction_spreadsheet: '课程限制表',
    official_link: '官方链接',
  } : {
    registrar_websoc_form: 'WebSoc query form',
    registrar_websoc_results: 'WebSoc department results',
    registrar_websoc_course_results: 'WebSoc course results',
    undergrad_restrictions: 'Official undergraduate restrictions',
    graduate_restrictions: 'Official graduate restrictions',
    policy: 'Official policy page',
    restriction_spreadsheet: 'Course restrictions spreadsheet',
    official_link: 'Official link',
  };

  for (const fetchItem of fetches) {
    if (!fetchItem || typeof fetchItem !== 'object') continue;
    const row = document.createElement('div');
    row.className = `tool-fetch-row ${fetchItem.ok === false ? 'is-error' : 'is-ok'}`;

    const header = document.createElement('div');
    header.className = 'tool-fetch-row-main';
    const status = document.createElement('span');
    status.className = 'tool-fetch-status';
    status.textContent = fetchItem.ok === false ? (zh ? '失败' : 'Failed') : (zh ? '成功' : 'Succeeded');
    header.appendChild(status);

    const method = document.createElement('span');
    method.className = 'tool-fetch-method';
    method.textContent = String(fetchItem.method || 'GET').toUpperCase();
    header.appendChild(method);

    const url = String(fetchItem.final_url || fetchItem.url || '');
    const host = String(fetchItem.host || url || (zh ? '未知地址' : 'Unknown address'));
    if (/^https?:\/\//i.test(url)) {
      const link = document.createElement('a');
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = host;
      link.title = url;
      header.appendChild(link);
    } else {
      const address = document.createElement('span');
      address.textContent = host;
      header.appendChild(address);
    }

    if (fetchItem.provides_evidence) {
      const evidence = document.createElement('span');
      evidence.className = 'tool-fetch-evidence';
      evidence.textContent = zh ? '用于最终事实' : 'Used as evidence';
      header.appendChild(evidence);
    }
    row.appendChild(header);

    const meta = document.createElement('div');
    meta.className = 'tool-fetch-meta';
    const role = roleLabels[fetchItem.source_role]
      || (zh ? '网页来源' : 'Web source');
    const fields = [role];
    if (fetchItem.status_code != null) fields.push(`HTTP ${fetchItem.status_code}`);
    if (fetchItem.bytes != null) fields.push(`${Number(fetchItem.bytes).toLocaleString()} B`);
    if (fetchItem.duration_ms != null) fields.push(`${Math.round(fetchItem.duration_ms)} ms`);
    if (fetchItem.depth != null) fields.push(zh ? `深度 ${fetchItem.depth}` : `Depth ${fetchItem.depth}`);
    meta.textContent = fields.join(' · ');
    row.appendChild(meta);

    if (fetchItem.parent_url) {
      const parent = document.createElement('div');
      parent.className = 'tool-fetch-parent';
      parent.textContent = zh ? `来自：${fetchItem.parent_url}` : `From: ${fetchItem.parent_url}`;
      row.appendChild(parent);
    }
    if (fetchItem.error) {
      const error = document.createElement('div');
      error.className = 'tool-fetch-error';
      error.textContent = zh ? '抓取失败，未能读取此来源。' : 'Request failed; this source could not be read.';
      row.appendChild(error);
    }
    list.appendChild(row);
  }
  details.appendChild(list);
  container.appendChild(details);
}

function appendStreamingToken(wrap, fullText) {
  if (!wrap._gotFirstToken) {
    // First chunk has arrived — swap thinking shimmer for streaming dot.
    const body = wrap.querySelector('.msg-ai-body');
    body.classList.remove('thinking');
    body.removeAttribute('aria-label');
    body.classList.add('streaming');
    body.textContent = '';
    wrap._gotFirstToken = true;
  }
  wrap._pendingText = fullText;
  if (wrap._pendingFrame) return;  // already scheduled this frame
  wrap._pendingFrame = requestAnimationFrame(() => {
    const body = wrap.querySelector('.msg-ai-body');
    if (body) {
      body.innerHTML = formatMarkdown(wrap._pendingText || '');
    }
    wrap._pendingFrame = null;
    scrollChat();
  });
}

function finalizeAiMessage(wrap, fullText, meta, stopped) {
  if (meta.response_language) wrap._responseLanguage = meta.response_language;
  if (meta.query_intent === 'comparison' && wrap._queryKind !== 'recommendation') {
    wrap._queryKind = 'comparison';
  }
  wrap._recommendationCount = Array.isArray(meta.cards) ? meta.cards.length : 0;
  // Flush any pending frame so we render the final text definitively
  if (wrap._pendingFrame) {
    cancelAnimationFrame(wrap._pendingFrame);
    wrap._pendingFrame = null;
  }
  const body = wrap.querySelector('.msg-ai-body');
  // Remove both indicator classes so neither thinking shimmer nor
  // streaming dot persists after generation ends.
  body.classList.remove('thinking');
  body.removeAttribute('aria-label');
  body.classList.remove('streaming');
  body.innerHTML = formatMarkdown(fullText || '');

  renderQueryTermBadge(wrap, meta);

  if (!wrap._webFetchRendered
      && Array.isArray(meta.web_fetches)
      && meta.web_fetches.length > 0) {
    renderWebFetchSummary(wrap, meta.web_fetches);
    wrap._webFetchRendered = true;
  }

  // Cards
  if (meta.cards && meta.cards.length > 0) {
    const block = document.createElement('div');
    block.innerHTML = renderCardsBlock(meta.cards);
    if (block.firstElementChild) wrap.appendChild(block.firstElementChild);
  }

  setComposerSuggestions(!stopped && Array.isArray(meta.followups) ? meta.followups : []);
  renderScheduleGrid();

  // Continue button — only when the agent loop hit a budget limit
  // and stashed a continuation_id during this stream. Skip when the
  // user explicitly stopped generation (would re-bill on resume).
  if (!stopped && wrap._continuationId) {
    renderContinueBanner(wrap, wrap._continuationId, wrap._limitReason);
  }

  scrollChat();
}

function renderQueryTermBadge(wrap, meta) {
  const terms = Array.isArray(meta?.query_terms)
    ? meta.query_terms.map(term => responseTerm(term, wrap._responseLanguage)) : [];
  if (!terms.length) return;
  const zh = wrap._responseLanguage === 'zh';
  const badge = document.createElement('div');
  badge.className = 'query-term-badge';
  badge.textContent = meta.query_term_source === 'history'
    ? (zh ? `开课规律：${terms.join(' · ')}` : `Offering history: ${terms.join(' · ')}`)
    : terms.length > 1
      ? (zh ? `本次比较：${terms.join(' ↔ ')}` : `Comparing: ${terms.join(' ↔ ')}`)
      : (zh ? `本次查询：${terms[0]}` : `Querying: ${terms[0]}`);
  if (meta.inferred_year || meta.query_term_source === 'inferred') {
    badge.textContent += zh ? '（年份自动推断）' : ' (year inferred)';
  }
  const body = wrap.querySelector('.msg-ai-body');
  wrap.insertBefore(badge, body);
}

function continuationCopy(wrap) {
  const zh = wrap._responseLanguage === 'zh';
  const count = wrap._recommendationCount || 0;
  const kind = count > 0 ? 'recommendation' : wrap._queryKind;
  const common = zh
    ? {loading: '正在核实…', error: '暂时无法继续查询，请稍后重新提问。'}
    : {loading: 'Checking…', error: 'Unable to continue right now. Please try asking again shortly.'};
  if (kind === 'recommendation') {
    return {...common,
      message: zh
        ? (count > 0
          ? `已整理出 ${count} 门课程的查询结果。其余候选课程的信息尚未核实完整，你可以先查看现有结果，或继续核实剩余课程。`
          : '目前的信息还不足以给出完整的课程推荐，部分课程的开课情况或选课条件仍需核实。你可以继续查询。')
        : (count > 0
          ? `Results for ${count} ${count === 1 ? 'course are' : 'courses are'} ready to review. Other candidates still need checking. You can review the current results or continue checking the remaining courses.`
          : 'There is not enough information yet to complete your course recommendations. Some course offerings or enrollment requirements still need checking. You can continue the search.'),
      button: zh ? '继续核实剩余课程' : 'Check remaining courses',
    };
  }
  if (kind === 'comparison') {
    return {...common,
      message: zh
        ? '对比中还有部分信息需要核实。你可以先查看现有结果，或继续补全对比。'
        : 'Some details still need checking to complete the comparison. You can review the current results or continue the comparison.',
      button: zh ? '继续完成对比' : 'Continue comparison',
    };
  }
  return {...common,
    message: zh
      ? '还有部分信息需要进一步核实。你可以先查看本次结果，或继续查询。'
      : 'Some information still needs checking. You can review the current results or continue the search.',
    button: zh ? '继续查询' : 'Continue search',
  };
}

function renderContinueBanner(wrap, continuationId, reason) {
  // Avoid stacking multiple banners on the same message if finalize
  // is called more than once (defensive — shouldn't happen).
  const existing = wrap.querySelector(':scope > .limit-notice');
  if (existing) existing.remove();

  // Technical reasons remain in SSE/logs; the notice describes the user's task.
  const copy = continuationCopy(wrap);

  const notice = document.createElement('div');
  notice.className = 'limit-notice';
  notice.setAttribute('role', 'status');
  notice.innerHTML = `
    <div class="limit-notice-text"></div>
    <button class="continue-btn" type="button"></button>
  `;
  notice.querySelector('.limit-notice-text').textContent = copy.message;
  notice.querySelector('.continue-btn').textContent = copy.button;
  notice.querySelector('.continue-btn').addEventListener('click', (ev) => {
    continueAgent(wrap, continuationId, ev.currentTarget);
  });
  wrap.appendChild(notice);
}

async function continueAgent(wrap, continuationId, btn) {
  // Lock the button so a double-click can't fire two resumes (and the
  // backend pops the continuation_id anyway, but better UX to disable).
  btn.disabled = true;
  btn.textContent = continuationCopy(wrap).loading;

  // Append a fresh body section for the resumed answer so the original
  // fallback reply stays visible above. Without this the new tokens
  // would overwrite the fallback answer in place.
  const resumeBody = document.createElement('div');
  resumeBody.className = 'msg-ai-body streaming';
  resumeBody.style.marginTop = 'var(--space-3)';
  resumeBody.style.borderTop = '1px solid var(--border-subtle)';
  resumeBody.style.paddingTop = 'var(--space-3)';
  resumeBody.textContent = '';
  // Insert above the limit-notice so the layout reads: original
  // answer → resumed answer → (any further notice).
  const notice = wrap.querySelector(':scope > .limit-notice');
  wrap.insertBefore(resumeBody, notice);

  // Build a synthetic render target reusing startAiMessage's contract
  // so we can pipe SSE events through the existing helpers.
  const resumeShell = {
    _pendingFrame: null,
    _pendingText:  '',
    _gotFirstToken: true,  // skip the thinking shimmer — we're mid-message
    _toolChipQueue: [],
    querySelector: (sel) => sel === '.msg-ai-body' ? resumeBody : null,
  };

  let resumedText = '';
  let resumedMeta = null;
  let newContinuationId = null;
  let newReason = null;

  try {
    const res = await fetch(`${API}/api/chat/continue`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        session_id: currentSessionId || '',
        continuation_id: continuationId,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    await consumeSSE(res, {
      token(event) {
        resumedText += event.text;
        appendStreamingToken(resumeShell, resumedText);
      },
      meta(event) {
        resumedMeta = event;
        if (event.response_language) wrap._responseLanguage = event.response_language;
        if (typeof event.final_answer === 'string') {
          resumedText = event.final_answer;
        }
      },
      tool_call_start(event) {
        // Reuse the existing chip container on `wrap`. Pass the
        // real wrap, not the shell, so chips land in the right DOM.
        startToolChip(wrap, event.label || event.name, event.name, event.response_language);
      },
      tool_call_done(event) {
        finishToolChip(
          wrap,
          event.ok !== false,
          event.fetch_summary || [],
          event,
        );
      },
      limit_reached(event) {
        newContinuationId = event.continuation_id;
        newReason = event.reason;
        if (event.response_language) wrap._responseLanguage = event.response_language;
      },
      error(event) {
        console.error('Continue error event:', event.message);
        resumedText += `\n\n${continuationCopy(wrap).error}`;
        appendStreamingToken(resumeShell, resumedText);
      },
      done() {
        // explicit termination — finalize below
      },
    });
  } catch (err) {
    console.error('continue failed:', err);
    resumedText += `\n\n${continuationCopy(wrap).error}`;
    appendStreamingToken(resumeShell, resumedText);
  } finally {
    // Flush any pending frame and remove the streaming class
    if (resumeShell._pendingFrame) {
      cancelAnimationFrame(resumeShell._pendingFrame);
      resumeShell._pendingFrame = null;
    }
    resumeBody.classList.remove('streaming');
    resumeBody.innerHTML = formatMarkdown(resumedText || '');

    // Replace the old notice. If the resumed loop ALSO hit a limit,
    // render a fresh Continue banner with the new continuation_id;
    // otherwise just drop the original banner since we're done.
    if (notice) notice.remove();
    wrap._continuationId = null;
    wrap._limitReason    = null;
    if (newContinuationId) {
      renderContinueBanner(wrap, newContinuationId, newReason);
    }
    scrollChat();
  }
}

function appendUser(text, options = {}) {
  const scroll = document.getElementById('chatScroll');
  scroll.querySelectorAll('.delivered').forEach(el => el.remove());
  const el = document.createElement('div');
  el.className = 'msg msg-user';
  if (!scroll.querySelector('.msg-user')) {
    const date = new Date(options.time || Date.now());
    if (!Number.isNaN(date.getTime()) && (!options.restored || options.time)) {
      const stamp = document.createElement('div');
      stamp.className = 'message-timestamp';
      const today = date.toDateString() === new Date().toDateString();
      stamp.textContent = `${today ? 'Today' : date.toLocaleDateString(undefined, {month: 'short', day: 'numeric'})} ${date.toLocaleTimeString(undefined, {hour: 'numeric', minute: '2-digit'})}`;
      el.appendChild(stamp);
    }
  }
  const bubble = document.createElement('div');
  bubble.className = 'bubble-me' + (options.restored ? '' : ' send');
  bubble.textContent = text;
  el.appendChild(bubble);
  scroll.appendChild(el);
  if (options.restored) markUserDelivery(el, true);
  scrollChat();
  return el;
}

function markUserDelivery(el, delivered) {
  if (!el || el !== [...document.querySelectorAll('#chatScroll .msg-user')].at(-1)) return;
  let receipt = el.querySelector('.delivered');
  if (!receipt) {
    receipt = document.createElement('div');
    receipt.className = 'delivered';
    el.appendChild(receipt);
  }
  receipt.textContent = delivered ? 'Delivered' : 'Not sent';
}

/* ── Markdown renderer ───────────────────────────────
   Pipeline (in order, so block-level wins over inline):
     1. extract fenced code blocks (their content is raw,
        must escape but not re-interpret)
     2. escape HTML on everything else
     3. detect GitHub-flavored tables → <table> blocks
     4. detect # / ## / ### headers
     5. detect ---/*** horizontal rules
     6. detect contiguous - / * / 1. lists
     7. inline: `code`, **bold**, [text](url)
     8. \n → <br> for paragraph flow
     9. restore block placeholders (eating one
        adjacent <br> so blocks don't get extra gaps)

   The function is called once per animation frame
   during streaming, so the input is often a partial
   document. The line-based detectors are tolerant —
   a header row without its separator falls through
   as plain text and lights up once the separator
   arrives in the next chunk.
*/
function formatMarkdown(text) {
  if (!text) return '';

  const blocks = [];
  const stash = (html) => {
    const idx = blocks.length;
    blocks.push(html);
    return `BLK${idx}`;
  };

  // 1. Fenced code blocks ``` first — their content
  //    must survive escape-then-markdown unchanged.
  text = text.replace(/```([\w-]*)\n?([\s\S]*?)```/g, (_m, _lang, code) => {
    const inner = _mdEscape(code.replace(/\n$/, ''));
    return stash(`<pre class="md-pre"><code>${inner}</code></pre>`);
  });

  // 2. Escape HTML on the remaining text so any <, >, &
  //    typed by the LLM render literally.
  text = _mdEscape(text);

  // 3. Tables (GitHub-flavored: header row, sep row, body rows).
  text = _mdRenderTables(text, stash);

  // 4. Headers — process longest first so ### doesn't get eaten by #.
  text = text.replace(/^###\s+(.+)$/gm,
    (_m, c) => stash(`<h3 class="md-h3">${_mdInline(c)}</h3>`));
  text = text.replace(/^##\s+(.+)$/gm,
    (_m, c) => stash(`<h2 class="md-h2">${_mdInline(c)}</h2>`));
  text = text.replace(/^#\s+(.+)$/gm,
    (_m, c) => stash(`<h1 class="md-h1">${_mdInline(c)}</h1>`));

  // 5. Horizontal rule (--- or *** on its own line).
  text = text.replace(/^[\s]*(?:-{3,}|\*{3,}|_{3,})[\s]*$/gm,
    () => stash('<hr class="md-hr">'));

  // 6. Lists.
  text = _mdRenderLists(text, stash);

  // 7. Inline formatting.
  text = _mdInline(text);

  // 8. Remaining newlines → <br>.
  text = text.replace(/\n/g, '<br>');

  // 9. Restore blocks, eating one adjacent <br> on each
  //    side so blocks don't carry extra visual padding.
  text = text.replace(/(?:<br>)?BLK(\d+)(?:<br>)?/g,
    (_m, idx) => blocks[parseInt(idx, 10)]);

  return text;
}

function _mdEscape(s) {
  return s.replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;');
}

/* Inline pass: code first (so its backtick content isn't
   mangled), then bold, then links. Italic intentionally
   omitted — _under_score collides with snake_case names. */
function _mdInline(s) {
  s = s.replace(/`([^`\n]+)`/g, '<code class="md-code">$1</code>');
  s = s.replace(/\*\*([^\n]+?)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return s;
}

/* Table detection. Walks lines; when a header row is
   immediately followed by a separator row (cells like
   ---, :--, --:, :--:), grabs subsequent body rows
   until a non-pipe line appears, and emits one <table>. */
function _mdRenderTables(text, stash) {
  const lines = text.split('\n');
  const out = [];
  let i = 0;
  while (i < lines.length) {
    if (_mdIsTableRow(lines[i]) && _mdIsTableSep(lines[i + 1])) {
      const headers = _mdParseRow(lines[i]);
      const align   = _mdParseAlign(lines[i + 1]);
      const rows    = [];
      let j = i + 2;
      while (j < lines.length && _mdIsTableRow(lines[j]) && !_mdIsTableSep(lines[j])) {
        rows.push(_mdParseRow(lines[j]));
        j++;
      }
      out.push(stash(_mdTableHTML(headers, align, rows)));
      i = j;
    } else {
      out.push(lines[i]);
      i++;
    }
  }
  return out.join('\n');
}

function _mdIsTableRow(line) {
  if (line == null) return false;
  const t = line.trim();
  return t.startsWith('|') && t.endsWith('|') && t.length >= 3 && t.indexOf('|', 1) > 0;
}
function _mdIsTableSep(line) {
  if (!_mdIsTableRow(line)) return false;
  const cells = _mdParseRow(line);
  return cells.length > 0 && cells.every(c => /^:?-{2,}:?$/.test(c.trim()));
}
function _mdParseRow(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|'))   s = s.slice(0, -1);
  return s.split('|').map(c => c.trim());
}
function _mdParseAlign(line) {
  return _mdParseRow(line).map(c => {
    const t = c.trim();
    const L = t.startsWith(':'), R = t.endsWith(':');
    if (L && R) return 'center';
    if (R)      return 'right';
    if (L)      return 'left';
    return null;
  });
}
function _mdTableHTML(headers, align, rows) {
  const alignAttr = (i) => align[i] ? ` style="text-align:${align[i]}"` : '';
  const th = headers.map((h, i) => `<th${alignAttr(i)}>${_mdInline(h)}</th>`).join('');
  const trs = rows.map(row => {
    const tds = row.map((cell, i) => `<td${alignAttr(i)}>${_mdInline(cell)}</td>`).join('');
    return `<tr>${tds}</tr>`;
  }).join('');
  const body = trs ? `<tbody>${trs}</tbody>` : '';
  return `<div class="md-table-wrap"><table class="md-table"><thead><tr>${th}</tr></thead>${body}</table></div>`;
}

/* List detection. Groups contiguous lines that all
   start with "-" / "*" (unordered) or "1." / "2." …
   (ordered). Sublists / continuation lines not handled —
   the LLM's lists are flat in practice. */
function _mdRenderLists(text, stash) {
  const lines = text.split('\n');
  const out = [];
  let i = 0;
  const UL = /^\s*[-*]\s+(.+)$/;
  const OL = /^\s*\d+\.\s+(.+)$/;
  while (i < lines.length) {
    let m = lines[i].match(UL);
    if (m) {
      const items = [];
      while (i < lines.length) {
        const mm = lines[i].match(UL);
        if (!mm) break;
        items.push(`<li>${_mdInline(mm[1])}</li>`);
        i++;
      }
      out.push(stash(`<ul class="md-ul">${items.join('')}</ul>`));
      continue;
    }
    m = lines[i].match(OL);
    if (m) {
      const items = [];
      while (i < lines.length) {
        const mm = lines[i].match(OL);
        if (!mm) break;
        items.push(`<li>${_mdInline(mm[1])}</li>`);
        i++;
      }
      out.push(stash(`<ol class="md-ol">${items.join('')}</ol>`));
      continue;
    }
    out.push(lines[i]);
    i++;
  }
  return out.join('\n');
}
/* ── Followup chips: re-frame advisor-perspective text ───
   Followups SHOULD be authored as user imperatives (see backend
   followup.py), but if anything slips through ("Want me to X?" /
   "Should I X?" / 中文 "要不要..." 等), transform it on click so the
   message bubble doesn't read as inverted nonsense.
*/
function reframeFollowupForUser(text) {
  if (!text) return text;
  let t = text.trim();
  // Strip trailing ? / ？ for cleaner imperative output.
  t = t.replace(/[?？]+\s*$/, '').trim();

  const PATTERNS = [
    // ── English ──
    [/^(?:Want|Would you want|Do you want)(?: me)?\s+to\s+(.+)$/i,
      (_, x) => x.charAt(0).toUpperCase() + x.slice(1)],
    [/^(?:Would you like)(?: me)?\s+to\s+(.+)$/i,
      (_, x) => x.charAt(0).toUpperCase() + x.slice(1)],
    [/^(?:Should I|Shall I)\s+(.+)$/i,
      (_, x) => 'Yes, ' + x],
    [/^Can I\s+(.+?)(?:\s+for you)?$/i,
      (_, x) => 'Please ' + x],
    // ── 中文 ──
    [/^要不要我?帮你?(.+)$/,        (_, x) => '帮我' + x],
    [/^要不要(.+)$/,                 (_, x) => '我想' + x],
    [/^需要(?:我)?帮?(?:你)?(.+?)(?:吗)?$/, (_, x) => '请' + x],
    [/^我可以(?:帮你)?(.+?)(?:吗)?$/, (_, x) => '请' + x],
    [/^你想(.+?)(?:吗)?$/,           (_, x) => '我想' + x],
    [/^是否(?:要|想)?(.+)$/,         (_, x) => '请' + x],
  ];

  for (const [re, fn] of PATTERNS) {
    const m = t.match(re);
    if (m) return fn(...m);
  }
  return text;  // no pattern matched — send original
}

function sendFollowup(text) {
  sendMessage(reframeFollowupForUser(text));
}

function escHTML(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
function scrollChat() {
  const el = document.getElementById('chatScroll');
  requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
}

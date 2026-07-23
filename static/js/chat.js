/* ── Send / Stop ───────────────────────────────────── */
async function sendMessage(text) {
  const msg = text || inputEl.value.trim();
  if (!msg) return;
  if (currentAbortController) return;   // already generating

  inputEl.value = '';

  // Phase 3 R3: leaving the empty/welcome state on first send. The
  // welcome block is removed and the scroll loses its centered layout.
  const scrollEl = document.getElementById('chatScroll');
  const ws = document.getElementById('welcomeState');
  if (ws) ws.remove();
  scrollEl.classList.remove('is-empty');

  appendUser(msg);
  toggleSendStop(true);

  currentAbortController = new AbortController();

  // Round 4 — captured BEFORE the request so we can decide in `finally`
  // whether to re-poll the sidebar for the LLM-generated title. The
  // backend only runs `_auto_title_session` (BackgroundTask) on the
  // first turn of a brand-new session; for follow-ups the title is
  // already user-meaningful and we skip the extra fetches.
  const isNewSession = !currentSessionId;

  // Build request payload — send currentSessionId or "" for "new session"
  const customPrompt = getActivePrompt();
  const payload = { message: msg, session_id: currentSessionId || '' };
  if (customPrompt) payload.system_prompt = customPrompt;

  // Create the AI message bubble up-front; tokens stream into its body.
  const aiMsg = startAiMessage();

  let fullText = '';
  let meta = null;
  let stopped = false;

  try {
    const res = await fetch(`${API}/api/chat/stream`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload),
      signal: currentAbortController.signal,
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
        meta = event;
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
        startToolChip(aiMsg, event.label || event.name, event.name);
      },
      tool_call_done(event) {
        finishToolChip(aiMsg, event.ok !== false, event.fetch_summary || []);
      },
      limit_reached(event) {
        // Agent hit its budget. Stash the continuation_id on the
        // message so finalizeAiMessage can render a Continue button
        // after the fallback answer finishes streaming.
        console.log('[chat] limit_reached:', event);
        aiMsg._continuationId = event.continuation_id;
        aiMsg._limitReason    = event.reason;
      },
      error(event) {
        console.error('Stream error event:', event.message);
        fullText += `\n\n_(server error: ${event.message})_`;
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
      fullText += fullText ? '\n\n_(generation stopped)_' : '_(generation stopped)_';
    } else {
      console.error('chat stream failed:', err);
      fullText = fullText || 'Connection error — is the backend running?';
    }
  } finally {
    const finalText = (!stopped && meta && typeof meta.final_answer === 'string')
      ? meta.final_answer
      : fullText;
    finalizeAiMessage(aiMsg, finalText, meta || {}, stopped);
    currentAbortController = null;
    toggleSendStop(false);
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
    sendBtn.disabled = false;
  }
}

/* ── AI message bubble: streaming + finalize ──────── */
function startAiMessage() {
  const wrap = document.createElement('div');
  wrap.className = 'msg msg-ai';
  // Initial state: shimmer "Thinking" until first token arrives.
  wrap.innerHTML = '<div class="msg-ai-label">Advisor</div>'
                 + '<div class="msg-ai-body thinking">Thinking</div>';
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

function startToolChip(wrap, label, toolName) {
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
  chip.querySelector('.tool-label').textContent = label || '调用工具';
  container.appendChild(chip);
  wrap._toolChipQueue.push(chip);
  scrollChat();
}

function finishToolChip(wrap, ok, fetchSummary) {
  const chip = (wrap._toolChipQueue || []).shift();
  if (chip) {
    chip.classList.remove('is-active');
    chip.classList.add(ok ? 'is-done' : 'is-error');
  }
  if (Array.isArray(fetchSummary) && fetchSummary.length > 0) {
    renderWebFetchSummary(wrap, fetchSummary);
    wrap._webFetchRendered = true;
  }
}

function renderWebFetchSummary(wrap, fetches) {
  if (!wrap || !Array.isArray(fetches) || fetches.length === 0) return;

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
  summary.textContent = `实际抓取 ${fetches.length} 个请求`;
  details.appendChild(summary);

  const list = document.createElement('div');
  list.className = 'tool-fetch-list';
  const roleLabels = {
    registrar_websoc_form: 'WebSoc 查询表单',
    registrar_websoc_results: 'WebSoc 部门结果',
    undergrad_restrictions: '本科限制官方页',
    graduate_restrictions: '研究生限制官方页',
    policy: '政策官方页',
    restriction_spreadsheet: '课程限制表',
    official_link: '官方链接',
  };

  for (const fetchItem of fetches) {
    if (!fetchItem || typeof fetchItem !== 'object') continue;
    const row = document.createElement('div');
    row.className = `tool-fetch-row ${fetchItem.ok === false ? 'is-error' : 'is-ok'}`;

    const header = document.createElement('div');
    header.className = 'tool-fetch-row-main';
    const status = document.createElement('span');
    status.className = 'tool-fetch-status';
    status.textContent = fetchItem.ok === false ? '失败' : '成功';
    header.appendChild(status);

    const method = document.createElement('span');
    method.className = 'tool-fetch-method';
    method.textContent = String(fetchItem.method || 'GET').toUpperCase();
    header.appendChild(method);

    const url = String(fetchItem.final_url || fetchItem.url || '');
    const host = String(fetchItem.host || url || '未知地址');
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
      evidence.textContent = '用于最终事实';
      header.appendChild(evidence);
    }
    row.appendChild(header);

    const meta = document.createElement('div');
    meta.className = 'tool-fetch-meta';
    const role = roleLabels[fetchItem.source_role]
      || String(fetchItem.source_role || '网页来源');
    const fields = [role];
    if (fetchItem.status_code != null) fields.push(`HTTP ${fetchItem.status_code}`);
    if (fetchItem.bytes != null) fields.push(`${Number(fetchItem.bytes).toLocaleString()} B`);
    if (fetchItem.duration_ms != null) fields.push(`${Math.round(fetchItem.duration_ms)} ms`);
    if (fetchItem.depth != null) fields.push(`深度 ${fetchItem.depth}`);
    meta.textContent = fields.join(' · ');
    row.appendChild(meta);

    if (fetchItem.parent_url) {
      const parent = document.createElement('div');
      parent.className = 'tool-fetch-parent';
      parent.textContent = `来自：${fetchItem.parent_url}`;
      row.appendChild(parent);
    }
    if (fetchItem.error) {
      const error = document.createElement('div');
      error.className = 'tool-fetch-error';
      error.textContent = String(fetchItem.error);
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
  // Flush any pending frame so we render the final text definitively
  if (wrap._pendingFrame) {
    cancelAnimationFrame(wrap._pendingFrame);
    wrap._pendingFrame = null;
  }
  const body = wrap.querySelector('.msg-ai-body');
  // Remove both indicator classes so neither thinking shimmer nor
  // streaming dot persists after generation ends.
  body.classList.remove('thinking');
  body.classList.remove('streaming');
  body.innerHTML = formatMarkdown(fullText || '');

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

  // Followups
  if (!stopped && meta.followups && meta.followups.length > 0) {
    const fuDiv = document.createElement('div');
    fuDiv.className = 'followups';
    let fuHtml = '';
    for (const fu of meta.followups) {
      fuHtml += `<button class="followup-chip" onclick="sendFollowup(\`${fu.replace(/`/g,"'")}\`)">${escHTML(fu)}</button>`;
    }
    fuDiv.innerHTML = fuHtml;
    wrap.appendChild(fuDiv);
  }

  // Validation footer
  if (!stopped && meta.validation_report
      && Array.isArray(meta.validation_report.issues)
      && meta.validation_report.issues.length > 0) {
    const vDiv = document.createElement('div');
    vDiv.innerHTML = renderValidationFooter(meta.validation_report);
    wrap.appendChild(vDiv.firstElementChild);
  }

  // Continue button — only when the agent loop hit a budget limit
  // and stashed a continuation_id during this stream. Skip when the
  // user explicitly stopped generation (would re-bill on resume).
  if (!stopped && wrap._continuationId) {
    renderContinueBanner(wrap, wrap._continuationId, wrap._limitReason);
  }

  scrollChat();
}

function renderContinueBanner(wrap, continuationId, reason) {
  // Avoid stacking multiple banners on the same message if finalize
  // is called more than once (defensive — shouldn't happen).
  const existing = wrap.querySelector(':scope > .limit-notice');
  if (existing) existing.remove();

  const reasonText = reason === 'max_tool_calls'
    ? '已达到工具调用上限，下面是基于已收集信息的回答。'
    : '已达到推理步数上限，下面是基于已收集信息的回答。';

  const notice = document.createElement('div');
  notice.className = 'limit-notice';
  notice.innerHTML = `
    <div class="limit-notice-text">${reasonText}</div>
    <button class="continue-btn" type="button">继续探索</button>
  `;
  notice.querySelector('.continue-btn').addEventListener('click', (ev) => {
    continueAgent(wrap, continuationId, ev.currentTarget);
  });
  wrap.appendChild(notice);
}

async function continueAgent(wrap, continuationId, btn) {
  // Lock the button so a double-click can't fire two resumes (and the
  // backend pops the continuation_id anyway, but better UX to disable).
  btn.disabled = true;
  btn.textContent = '继续中…';

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
      tool_call_start(event) {
        // Reuse the existing chip container on `wrap`. Pass the
        // real wrap, not the shell, so chips land in the right DOM.
        startToolChip(wrap, event.label || event.name, event.name);
      },
      tool_call_done(event) {
        finishToolChip(wrap, event.ok !== false, event.fetch_summary || []);
      },
      limit_reached(event) {
        newContinuationId = event.continuation_id;
        newReason = event.reason;
      },
      error(event) {
        resumedText += `\n\n_(continue error: ${event.message})_`;
        appendStreamingToken(resumeShell, resumedText);
      },
      done() {
        // explicit termination — finalize below
      },
    });
  } catch (err) {
    console.error('continue failed:', err);
    resumedText += `\n\n_(continue failed: ${err.message || err})_`;
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

function appendUser(text) {
  const el = document.createElement('div');
  el.className = 'msg msg-user';
  el.textContent = text;
  document.getElementById('chatScroll').appendChild(el);
  scrollChat();
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

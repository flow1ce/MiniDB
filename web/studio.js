(function () {
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const editor = $('#sqlEditor');
  const state = { rows: [], columns: [], pipelines: [], results: [], bufferDetails: [], selectedStatement: 0, selectedStage: 'parse', cacheVerification: '', running: false, frameCapacity: 16, sessionId: '' };

  function formatMs(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
    return `${Number(value).toFixed(2)} ms`;
  }

  function setTheme(theme) {
    document.body.classList.toggle('theme-light', theme === 'light');
    $('#themeToggle').textContent = theme === 'light' ? '●' : '◐';
    localStorage.setItem('minidb-theme', theme);
  }

  async function request(path, options = {}) {
    const headers = new Headers(options.headers || {});
    const sessionId = state.sessionId || sessionStorage.getItem('minidb-session');
    if (sessionId) headers.set('X-MiniDB-Session', sessionId);
    options = { ...options, headers };
    const response = await fetch(path, options);
    let payload;
    try { payload = await response.json(); }
    catch (_) { throw new Error('服务器返回了无法解析的响应'); }
    if (!response.ok) {
      const detail = payload.error;
      throw new Error(typeof detail === 'string' ? detail : (detail && detail.message) || `HTTP ${response.status}`);
    }
    return payload;
  }

  function post(path, sql) {
    return request(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sql === undefined ? {} : { sql }),
    });
  }

  function log(message, isError = false) {
    const row = document.createElement('div');
    const now = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    row.innerHTML = `<time>${now}</time><span></span><p></p>`;
    row.querySelector('p').textContent = message;
    if (isError) row.classList.add('error-event');
    $('#eventLog').prepend(row);
    while ($('#eventLog').children.length > 8) $('#eventLog').lastElementChild.remove();
  }

  function errorText(payload) {
    const error = payload && payload.error;
    if (!error) return '未知错误';
    if (typeof error === 'string') return error;
    const position = error.line == null ? '' : `（第 ${error.line} 行，第 ${error.column} 列）`;
    return `${error.stage || 'Error'}: ${error.message || '请求失败'}${position}`;
  }

  function planName(plan) { return plan && plan.op ? plan.op : '—'; }

  function containsPlan(plan, op) {
    if (!plan) return false;
    if (plan.op === op) return true;
    return (plan.children || []).some((child) => containsPlan(child, op));
  }

  function formatExpression(value) {
    if (value === null || value === undefined) return 'NULL';
    if (typeof value !== 'object') return String(value);
    switch (value.node) {
      case 'Star': return '*';
      case 'Identifier': return value.name || '标识符';
      case 'Literal': return typeof value.value === 'string' ? `'${value.value}'` : String(value.value);
      case 'Binary': return `${formatExpression(value.left)} ${value.op || ''} ${formatExpression(value.right)}`.trim();
      case 'Unary': return `${value.op || ''}${formatExpression(value.operand || value.expr || value.value)}`.trim();
      default: return value.node ? `${value.node}${value.name ? `(${value.name})` : ''}` : formatPlanValue(value);
    }
  }

  function formatPlanValue(value) {
    if (value === null || value === undefined) return 'NULL';
    if (Array.isArray(value)) return value.map(formatPlanValue).join(', ');
    if (typeof value !== 'object') return String(value);
    if (value.node) return formatExpression(value);
    const entries = Object.entries(value).filter(([key, item]) => !['line', 'column'].includes(key) && item !== undefined && item !== null);
    return entries.map(([key, item]) => `${key}: ${formatPlanValue(item)}`).join(' · ');
  }

  function renderPlanTree(container, plan, emptyText = '暂无计划') {
    container.replaceChildren();
    if (!plan) {
      container.textContent = emptyText;
      return;
    }
    const tree = document.createElement('div');
    tree.className = 'plan-tree';

    function appendNode(parent, current) {
      if (!current) return;
      const node = document.createElement('div');
      node.className = 'plan-node';
      const head = document.createElement('div');
      head.className = 'plan-node-head';
      const op = document.createElement('span');
      op.className = 'plan-op';
      op.textContent = current.op || 'Node';
      head.appendChild(op);
      const args = current.args || {};
      if (args.table) {
        const target = document.createElement('span');
        target.className = 'plan-target';
        target.textContent = String(args.table);
        head.appendChild(target);
      }
      node.appendChild(head);
      const entries = Object.entries(args).filter(([, value]) => value !== undefined && value !== null && value !== '');
      if (entries.length) {
        const details = document.createElement('div');
        details.className = 'plan-args';
        entries.forEach(([key, value]) => {
          const row = document.createElement('div');
          row.className = 'plan-arg';
          const label = document.createElement('span');
          label.className = 'plan-arg-key';
          label.textContent = key;
          const content = document.createElement('span');
          content.className = 'plan-arg-value';
          content.textContent = key === 'expr' ? formatExpression(value) : formatPlanValue(value);
          row.append(label, content);
          details.appendChild(row);
        });
        node.appendChild(details);
      }
      const children = current.children || [];
      if (children.length) {
        const branch = document.createElement('div');
        branch.className = 'plan-children';
        children.forEach((child) => appendNode(branch, child));
        node.appendChild(branch);
      }
      parent.appendChild(node);
    }

    appendNode(tree, plan);
    container.appendChild(tree);
  }

  function currentPipeline() { return state.pipelines[state.selectedStatement] || null; }

  function currentResult() { return state.results[state.selectedStatement] || null; }

  function updatePipeline(pipeline) {
    if (!pipeline) state.pipelines = [];
    else if (Array.isArray(pipeline)) state.pipelines = pipeline;
    else if (pipeline.pipeline && Array.isArray(pipeline.pipeline)) state.pipelines = pipeline.pipeline;
    else if (state.pipelines.length === 1 && (pipeline.plan || pipeline.optimized_plan)) state.pipelines = [{ ...state.pipelines[0], ...pipeline }];
    else state.pipelines = [pipeline];
    state.selectedStatement = Math.min(state.selectedStatement, Math.max(0, state.pipelines.length - 1));
    const selected = currentPipeline() || {};
    const optimized = selected && selected.optimized_plan;
    renderPlanTree($('#planOutput'), optimized || (selected && selected.plan));
    $('#planType').textContent = state.pipelines.length > 1 ? `多计划 · 第 ${state.selectedStatement + 1} 条` : planName(optimized);
    $('#planCheck').textContent = state.pipelines.length ? '✓' : '○';
    $$('.pipe-stage').forEach((stage) => stage.classList.toggle('done', Boolean(state.pipelines.length)));
    renderStageDetail(state.selectedStage);
  }

  function statementCount() { return Math.max(state.pipelines.length, state.results.length, state.bufferDetails.length); }

  function renderStatementSwitcher(detail, onSelect) {
    const count = statementCount();
    if (count <= 1) return null;
    const nav = document.createElement('div');
    nav.className = 'stage-statement-tabs';
    Array.from({ length: count }, (_, index) => index).forEach((index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `stage-statement-tab${index === state.selectedStatement ? ' active' : ''}`;
      button.textContent = `第 ${index + 1} 条语句`;
      button.addEventListener('click', () => onSelect(index));
      nav.appendChild(button);
    });
    detail.appendChild(nav);
    return nav;
  }

  function renderStageDetail(stage) {
    const detail = $('#stageDetail');
    state.selectedStage = stage;
    detail.replaceChildren();
    if (!state.pipelines.length && !state.bufferDetails.length) {
      detail.textContent = '请先运行 SQL 或查看执行计划。';
      return;
    }
    const selected = currentPipeline() || {};
    const selectedResult = currentResult() || (state.bufferDetails[state.selectedStatement] && state.bufferDetails[state.selectedStatement].warm);
    const selectStatement = (index) => {
      state.selectedStatement = index;
      updatePipeline(state.pipelines);
    };
    renderStatementSwitcher(detail, selectStatement);
    const values = {
      parse: `词法分析完成，共 ${selected.tokens ? selected.tokens.length : 0} 个词元。\n${(selected.tokens || []).map((token) => `${token.type}${token.lexeme ? `(${token.lexeme})` : ''}`).join('  ')}`,
      semantic: `语义检查：${selected.semantic || '未返回'}\nAST 节点：${selected.ast?.node || selected.ast?.type || 'Statement'}`,
      buffer: selectedResult && selectedResult.metrics
        ? `本条语句：+${selectedResult.metrics.buffer_hits || 0} 命中 / +${selectedResult.metrics.buffer_misses || 0} 未命中 · 读盘 ${selectedResult.metrics.pages_read || 0} 页`
        : (state.cacheVerification || $('#bufferDelta').textContent),
      execute: selectedResult ? (selectedResult.success === false ? errorText(selectedResult) : `本条语句执行完成，${selectedResult.row_count ?? selectedResult.affected_rows ?? selectedResult.rows?.length ?? 0} 行`) : $('#resultMeta').textContent,
    };
    if (stage === 'plan' || stage === 'optimize') {
      const tree = document.createElement('div');
      tree.className = 'stage-plan-tree';
      renderPlanTree(tree, stage === 'plan' ? selected.plan : selected.optimized_plan);
      const title = document.createElement('strong');
      title.className = 'stage-plan-title';
      title.textContent = stage === 'plan' ? '原始逻辑计划' : '优化后物理计划';
      detail.append(title, tree);
      return;
    }
    const text = document.createElement('pre');
    text.className = 'stage-text';
    text.textContent = values[stage] || '';
    detail.appendChild(text);
  }

  function selectStage(button) {
    $$('.pipe-stage').forEach((item) => item.classList.toggle('active', item === button));
    renderStageDetail(button.dataset.stage);
  }

  function renderTable(columns, rows) {
    state.columns = columns || [];
    state.rows = rows || [];
    $('#resultCards').hidden = true;
    const head = $('#resultTable thead');
    const body = $('#resultTable tbody');
    head.innerHTML = '';
    body.innerHTML = '';
    $('#resultMessage').hidden = true;
    if (!state.columns.length) {
      $('#resultMessage').hidden = false;
      return;
    }
    const headerRow = document.createElement('tr');
    state.columns.forEach((column) => {
      const th = document.createElement('th');
      th.textContent = column;
      headerRow.appendChild(th);
    });
    head.appendChild(headerRow);
    state.rows.forEach((row) => {
      const tr = document.createElement('tr');
      state.columns.forEach((column) => {
        const td = document.createElement('td');
        const value = row && typeof row === 'object' && !Array.isArray(row) ? row[column] : row;
        td.textContent = value === null || value === undefined ? 'NULL' : String(value);
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
  }

  function metadataRows(result) {
    if (Array.isArray(result.tables)) return { columns: ['table'], rows: result.tables.map((table) => ({ table })) };
    if (Array.isArray(result.columns)) return { columns: ['name', 'type', 'primary_key', 'unique', 'not_null'], rows: result.columns };
    return null;
  }

  function resultTable(result) {
    if (result.type === 'query') return { columns: result.columns || [], rows: result.rows || [] };
    if (result.type === 'metadata') return metadataRows(result);
    if (result.type === 'explain' && result.actual) return { columns: result.actual.columns || [], rows: result.actual.rows || [] };
    return null;
  }

  function renderBatchCards(results) {
    const cards = $('#resultCards');
    cards.innerHTML = '';
    cards.hidden = false;
    results.forEach((item, index) => {
      const card = document.createElement('article');
      card.className = `result-card${item.success === false ? ' result-card-error' : ''}`;
      const head = document.createElement('div');
      head.className = 'result-card-head';
      const title = document.createElement('strong');
      title.textContent = `第 ${index + 1} 条语句 · ${item.success === false ? '执行失败' : (item.type || '命令')}`;
      const count = document.createElement('span');
      const table = item.success === false ? null : resultTable(item);
      const rowCount = table ? table.rows.length : Number(item.row_count ?? item.affected_rows ?? 0);
      count.textContent = item.success === false ? '错误' : `${rowCount} 行`;
      head.append(title, count);
      card.appendChild(head);
      if (item.success === false) {
        const error = document.createElement('pre');
        error.className = 'result-card-message';
        error.textContent = errorText(item);
        card.appendChild(error);
      } else if (table) {
        const tableWrap = document.createElement('div');
        tableWrap.className = 'result-card-table';
        const htmlTable = document.createElement('table');
        const thead = document.createElement('thead');
        const headerRow = document.createElement('tr');
        table.columns.forEach((column) => {
          const th = document.createElement('th');
          th.textContent = column;
          headerRow.appendChild(th);
        });
        thead.appendChild(headerRow);
        const tbody = document.createElement('tbody');
        table.rows.forEach((row) => {
          const tr = document.createElement('tr');
          table.columns.forEach((column) => {
            const td = document.createElement('td');
            const value = row && typeof row === 'object' ? row[column] : row;
            td.textContent = value === null || value === undefined ? 'NULL' : String(value);
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        });
        htmlTable.append(thead, tbody);
        tableWrap.appendChild(htmlTable);
        card.appendChild(tableWrap);
      } else {
        const message = document.createElement('pre');
        message.className = 'result-card-message';
        message.textContent = item.message || `${rowCount} 行受影响`;
        card.appendChild(message);
      }
      cards.appendChild(card);
    });
  }

  function renderResult(raw) {
    // The server returns one structured result per SQL statement.
    const results = Array.isArray(raw) ? raw : (Array.isArray(raw && raw.value) ? raw.value : [raw]);
    state.results = results;
    state.bufferDetails = [];
    state.selectedStatement = 0;
    if (state.pipelines.length) updatePipeline(state.pipelines);
    const isBatch = results.length > 1;
    if (isBatch) renderBatchCards(results);
    else $('#resultCards').hidden = true;
    $('#resultTable').hidden = isBatch;
    $('#resultMessage').hidden = isBatch;
    const failed = results.find((item) => item && item.success === false);
    if (failed) {
      if (isBatch) {
        state.columns = [];
        state.rows = [];
        $('#resultMessage').textContent = `${results.filter((item) => item.success === false).length} 条语句执行失败`;
        $('#resultTitle').textContent = `批量执行 · ${results.length} 条语句`;
        $('#resultMeta').textContent = `完成 ${results.length} 条语句，其中第 ${results.indexOf(failed) + 1} 条失败`;
        setStatus('部分语句失败', true);
        log(errorText(failed), true);
        updateMetrics(failed.metrics || {});
        return false;
      }
      const message = errorText(failed);
      renderTable([], []);
      $('#resultMessage').textContent = message;
      $('#resultTitle').textContent = '执行失败';
      $('#resultMeta').textContent = `第 ${results.indexOf(failed) + 1} 条语句失败`;
      setStatus('查询失败', true);
      log(message, true);
      updateMetrics(failed.metrics || {});
      return false;
    }
    const result = results.at(-1) || {};
    if (!result.success) {
      const message = errorText(result);
      renderTable([], []);
      $('#resultMessage').textContent = message;
      $('#resultTitle').textContent = '执行失败';
      $('#resultMeta').textContent = message;
      setStatus('查询失败', true);
      log(message, true);
      updateMetrics(result.metrics || {});
      return false;
    }
    const mutations = results.filter((item) => item && item.type === 'mutation');
    const affectedTotal = mutations.reduce((total, item) => total + Number(item.affected_rows || 0), 0);
    const mutationSummary = mutations.length
      ? mutations.map((item, index) => `第 ${index + 1} 条：${item.message || `${item.affected_rows || 0} 行受影响`}`).join('\n')
      : '';
    const table = resultTable(result);
    if (isBatch) {
      state.columns = [];
      state.rows = [];
      $('#resultMessage').textContent = `已分别显示 ${results.length} 条语句的结果`;
      $('#resultTitle').textContent = `批量执行 · ${results.length} 条语句`;
      const returnedTotal = results.reduce((sum, item) => sum + Number(item.row_count || item.affected_rows || (resultTable(item)?.rows.length || 0)), 0);
      $('#resultMeta').textContent = mutations.length
        ? `完成 ${results.length} 条语句，共影响 ${affectedTotal} 行`
        : `完成 ${results.length} 条语句，共返回 ${returnedTotal} 行`;
      setStatus('查询完成');
      const metrics = results.reduce((total, item) => {
        const current = item.metrics || {};
        Object.keys(current).forEach((key) => { total[key] = Number(total[key] || 0) + Number(current[key] || 0); });
        return total;
      }, {});
      metrics.rows_returned = results.reduce((sum, item) => sum + Number(item.row_count || item.affected_rows || (resultTable(item)?.rows.length || 0)), 0);
      updateMetrics(metrics, `批量合计（${results.length} 条）`);
      if (result.optimized_plan || result.plan) updatePipeline({ plan: result.plan, optimized_plan: result.optimized_plan });
      log(`执行完成：批量 ${results.length} 条语句，共显示 ${results.length} 个结果`);
      return true;
    }
    if (table) renderTable(table.columns, table.rows);
    else {
      renderTable([], []);
      $('#resultMessage').textContent = isBatch && mutations.length ? mutationSummary : (result.message || '命令执行成功');
    }
    const count = isBatch && mutations.length ? affectedTotal : (result.row_count ?? result.affected_rows ?? (table ? table.rows.length : 0));
    $('#resultTitle').textContent = result.type === 'explain'
      ? '执行计划'
      : (isBatch && mutations.length ? `批量写入 · ${affectedTotal} 行` : `${result.type || 'command'} · ${count} 行`);
    $('#resultMeta').textContent = isBatch
      ? `完成 ${results.length} 条语句，共影响 ${count} 行`
      : `返回 ${count} 行`;
    setStatus('查询完成');
    const metrics = isBatch
      ? results.reduce((total, item) => {
          const current = item.metrics || {};
          Object.keys(current).forEach((key) => { total[key] = Number(total[key] || 0) + Number(current[key] || 0); });
          return total;
        }, {})
      : (result.metrics || {});
    metrics.rows_returned = count;
    updateMetrics(metrics);
    if (result.optimized_plan || result.plan) updatePipeline({ plan: result.plan, optimized_plan: result.optimized_plan });
    log(`执行完成：${isBatch ? `批量 ${results.length} 条语句` : (result.type || 'command')}，${count} 行`);
    return true;
  }

  function updateMetrics(metrics, label = '本次') {
    $('#elapsed').textContent = formatMs(metrics.total_time_ms);
    $('#rowCount').textContent = String(metrics.rows_returned ?? 0);
    const indexed = (metrics.index_entries_examined || 0) > 0 || containsPlan(currentPipeline() && currentPipeline().optimized_plan, 'IndexScan');
    $('#scanType').textContent = indexed ? '索引' : (metrics.rows_examined ? '顺序' : '—');
    $('#bufferDelta').textContent = `${label} +${metrics.buffer_hits || 0} 命中 / +${metrics.buffer_misses || 0} 未命中`;
  }

  function renderStats(stats) {
    const hits = stats.hits || 0;
    const misses = stats.misses || 0;
    const rate = Number(stats.hit_rate || 0) * 100;
    $('#hits').textContent = hits;
    $('#misses').textContent = misses;
    $('#evictions').textContent = stats.evictions || 0;
    $('#reads').textContent = stats.disk_reads || 0;
    $('#writes').textContent = stats.disk_writes || 0;
    $('#dirtyFrames').textContent = `${stats.dirty_frames || 0} / ${stats.frames || 0}`;
    $('#hitRate').textContent = `${rate.toFixed(1)}%`;
    $('#hitMeter').style.width = `${Math.min(100, rate)}%`;
    state.frameCapacity = stats.capacity || state.frameCapacity;
    $('#frameCapacity').textContent = `${stats.frames || 0} / ${state.frameCapacity} FRAMES`;
    $('#bufferPolicy').textContent = stats.policy || '—';
    $('#bufferCapacity').textContent = String(state.frameCapacity);
    const grid = $('#bufferGrid');
    grid.innerHTML = '';
    const details = Array.isArray(stats.frame_details) ? stats.frame_details : [];
    const count = Math.max(state.frameCapacity, stats.frames || 0);
    for (let index = 0; index < count; index += 1) {
      const frame = document.createElement('span');
      const info = details[index];
      frame.className = `buffer-frame ${info || index < (stats.frames || 0) ? 'hot' : 'clean'}`;
      if (info?.dirty) frame.classList.add('dirty');
      if ((info?.pin_count || 0) > 0) frame.classList.add('pinned');
      frame.title = info ? `页 ${info.page_id} · ${info.dirty ? '脏页' : '干净'} · Pin ${info.pin_count || 0}` : '空闲页框';
      grid.appendChild(frame);
    }
  }

  async function loadStats() {
    try {
      const payload = await request('/api/stats');
      renderStats(payload.stats || {});
      return payload;
    } catch (error) {
      log(`状态读取失败：${error.message}`, true);
      throw error;
    }
  }

  async function ensureSession() {
    const navigation = performance.getEntriesByType('navigation')[0];
    let clientId = sessionStorage.getItem('minidb-session');
    // A duplicated/new tab may inherit sessionStorage from its opener.  Give
    // every newly navigated page its own database session, while preserving
    // the id across an ordinary reload of the same tab.
    if (!clientId || (navigation && navigation.type !== 'reload')) {
      clientId = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      sessionStorage.setItem('minidb-session', clientId);
    }
    const payload = await request('/api/session', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: clientId }),
    });
    state.sessionId = payload.session_id;
    $('#sessionBadge').textContent = state.sessionId.slice(0, 8);
    $('#lockTimeout').value = String(payload.lock_timeout || 30);
  }

  function renderConcurrency(payload) {
    const sessionId = payload.session_id || state.sessionId;
    const own = (payload.sessions || []).find((item) => item.session_id === sessionId);
    const ownTxn = own && own.transaction;
    $('#sessionBadge').textContent = sessionId ? sessionId.slice(0, 8) : '—';
    $('#concurrencyState').textContent = ownTxn
      ? `本页会话 ${sessionId.slice(0, 8)} · TX-${ownTxn.id} · ${ownTxn.isolation.replaceAll('_', ' ')} · 持有 ${ownTxn.locks} 个锁`
      : `本页会话 ${sessionId ? sessionId.slice(0, 8) : '—'} · 无活动事务 · 可在另一标签页开启事务`;
    const rows = [];
    (payload.locks || []).forEach((lock) => {
      const owners = (lock.owners || []).map((owner) => `TX-${owner.txn_id} ${owner.mode === 'EXCLUSIVE' ? '排他' : '共享'}`).join('、');
      rows.push(`<div class="lock-row"><span>${lock.resource}</span><b>${owners || '—'}</b></div>`);
    });
    (payload.waiting || []).forEach((item) => {
      const blockers = item.blocked_by.map((id) => `TX-${id}`).join('、') || '其他会话';
      rows.push(`<div class="lock-row waiting"><span>TX-${item.txn_id} 等待 ${item.resource}</span><b>被 ${blockers} 阻塞</b></div>`);
    });
    $('#lockList').innerHTML = rows.length ? rows.join('') : '<p class="tree-empty">暂无锁等待</p>';
  }

  async function loadConcurrency() {
    if (!state.sessionId) return;
    try { renderConcurrency(await request('/api/concurrency')); }
    catch (_) { /* 服务器重启期间静默等待下一次轮询 */ }
  }

  async function loadTables() {
    try {
      const payload = await request('/api/tables');
      const tables = payload.tables || [];
      const indexes = payload.indexes || [];
      $('#tableCount').textContent = String(tables.length);
      $('#objectSummary').textContent = `${tables.length} TABLES`;
      $('#indexCount').textContent = String(indexes.length);
      const indexContainer = $('#indexObjects');
      indexContainer.innerHTML = '';
      if (!indexes.length) indexContainer.innerHTML = '<p class="tree-empty">暂无索引</p>';
      indexes.forEach((index) => {
        const item = document.createElement('button');
        item.className = 'object-item';
        item.type = 'button';
        item.innerHTML = '<span class="object-glyph">⌁</span><b></b><small></small>';
        item.querySelector('b').textContent = index.name;
        item.querySelector('small').textContent = `${index.table}.${index.column}`;
        indexContainer.appendChild(item);
      });
      const txnContainer = $('#transactionObjects');
      txnContainer.innerHTML = '';
      if (payload.transaction) {
        $('#transactionCount').textContent = '1';
        const txn = document.createElement('div');
        txn.className = 'object-item';
        txn.innerHTML = '<span class="object-glyph">◌</span><b></b><small></small>';
        txn.querySelector('b').textContent = `TX-${payload.transaction.id}`;
        txn.querySelector('small').textContent = `${payload.transaction.isolation} · ${payload.transaction.state}`;
        txnContainer.appendChild(txn);
        $('#isolationLevel').textContent = payload.transaction.isolation.replaceAll('_', ' ');
      } else {
        $('#transactionCount').textContent = '0';
        txnContainer.innerHTML = '<p class="tree-empty">无活动事务</p>';
        $('#isolationLevel').textContent = 'READ COMMITTED';
      }
      const container = $('#tableObjects');
      container.innerHTML = '';
      if (!tables.length) {
        container.innerHTML = '<p class="tree-empty">暂无数据表</p>';
        return;
      }
      tables.forEach((table) => {
        const button = document.createElement('button');
        button.className = 'table-item';
        button.type = 'button';
        button.dataset.table = table;
        button.innerHTML = '<span>▤</span><b></b><small>查看数据</small>';
        button.querySelector('b').textContent = table;
        button.addEventListener('click', () => {
          $$('.table-item').forEach((item) => item.classList.remove('active'));
          button.classList.add('active');
          editor.value = `SELECT * FROM ${table};`;
          updateLines();
          editor.focus();
        });
        container.appendChild(button);
      });
    } catch (error) {
      $('#objectSummary').textContent = 'OFFLINE';
      $('#tableObjects').innerHTML = '<p class="tree-empty">目录读取失败</p>';
      log(`目录读取失败：${error.message}`, true);
    }
  }

  async function explain() {
    const sql = editor.value.trim();
    if (!sql) return setStatus('请输入 SQL', true);
    setStatus('正在生成计划…');
    try {
      const payload = await post('/api/explain', sql);
      updatePipeline(payload.pipeline);
      $('#resultTitle').textContent = '编译成功';
      $('#resultMeta').textContent = '已生成原始逻辑计划和优化后物理计划';
      setStatus('计划已生成');
      log('编译完成，已生成执行计划');
      // Viewing a plan is a compiler action: focus the original logical plan.
      selectStage($('.pipe-stage[data-stage="plan"]'));
    } catch (error) { showRequestError(error); }
  }

  async function run() {
    if (state.running) return;
    const sql = editor.value.trim();
    if (!sql) return setStatus('请输入 SQL', true);
    state.running = true;
    setStatus('正在执行…');
    $('#runTop').disabled = true;
    $('#runTime').textContent = '运行中';
    try {
      try {
        const compiled = await post('/api/explain', sql);
        updatePipeline(compiled.pipeline);
      } catch (_) { updatePipeline(null); }
      const payload = await post('/api/execute', sql);
      renderResult(payload);
      $('#runTime').textContent = $('#elapsed').textContent;
      await Promise.all([loadStats(), loadTables()]);
    } catch (error) { showRequestError(error); }
    finally { state.running = false; $('#runTop').disabled = false; }
  }

  async function verifyCache() {
    const sql = editor.value.trim();
    const bufferStage = $('.pipe-stage[data-stage="buffer"]');
    const selectBufferStage = () => {
      if (bufferStage) selectStage(bufferStage);
    };
    if (!sql) {
      selectBufferStage();
      $('#resultTitle').textContent = '缓存验证失败';
      $('#resultMessage').textContent = '请输入一条 SELECT 语句后再进行缓存验证。';
      $('#resultMeta').textContent = '缓存验证需要单条 SELECT';
      setStatus('缓存验证失败', true);
      return;
    }
    if (!/^select\b/i.test(sql)) {
      selectBufferStage();
      $('#resultTitle').textContent = '缓存验证失败';
      $('#resultMessage').textContent = '缓存验证只支持 SELECT 语句。';
      $('#resultMeta').textContent = '请使用一条或多条 SELECT';
      setStatus('缓存验证失败', true);
      return;
    }
    setStatus('正在执行冷/热缓存验证…');
    try {
      const payload = await post('/api/cache-test', sql);
      state.pipelines = [];
      state.selectedStatement = 0;
      if (!payload.success) {
        renderResult(payload);
        selectBufferStage();
        return;
      }
      if (payload.type === 'cache_validation_batch') {
        const checks = payload.checks || {};
        const total = payload.statement_count || 0;
        const verified = payload.verified_count || 0;
        const details = (payload.results || []).map((item) => {
          const mark = item.verified ? '通过' : '未通过';
          return `第 ${item.statement_index} 条：${mark}（冷 ${formatMs(item.cold?.metrics?.total_time_ms)} · 热 ${formatMs(item.warm?.metrics?.total_time_ms)}）`;
        });
        state.results = [];
        state.bufferDetails = payload.results || [];
        renderTable([], []);
        $('#resultTitle').textContent = `批量缓存验证 · ${verified}/${total} 通过`;
        $('#resultMessage').textContent = details.join('\n');
        $('#resultMeta').textContent = `完成 ${total} 条 SELECT，${checks.all_verified ? '全部通过' : '存在未通过项'}`;
        state.cacheVerification = `逐条验证 ${verified}/${total} 通过 · 冷读盘 ${checks.cold_read_from_disk ? '有' : '无'} · 热命中 ${checks.warm_hit_cache ? '有' : '无'}`;
        const warmMetrics = (payload.results || []).reduce((totalMetrics, item) => {
          const current = item.warm?.metrics || {};
          Object.keys(current).forEach((key) => { totalMetrics[key] = Number(totalMetrics[key] || 0) + Number(current[key] || 0); });
          return totalMetrics;
        }, {});
        updateMetrics(warmMetrics, `批量热缓存（${total} 条）`);
        renderStats(payload.stats || {});
        setStatus(payload.verified ? '批量缓存验证通过' : '批量缓存验证未通过', !payload.verified);
        log(`批量缓存验证：${verified}/${total} 通过`, !payload.verified);
        selectBufferStage();
        return;
      }
      const checks = payload.checks || {};
      const passed = payload.verified === true;
      state.results = [];
      state.bufferDetails = payload.warm ? [payload] : [];
      renderTable([], []);
      $('#resultTitle').textContent = passed ? '缓存验证通过' : '缓存验证未通过';
      $('#resultMessage').textContent = [
        `冷缓存读取磁盘：${checks.cold_read_from_disk ? '通过' : '未通过'}`,
        `热缓存命中：${checks.warm_hit_cache ? '通过' : '未通过'}`,
        `热缓存避免磁盘读取：${checks.warm_avoided_disk ? '通过' : '未通过'}`,
        `两次结果一致：${checks.same_result ? '通过' : '未通过'}`,
      ].join('\n');
      $('#resultMeta').textContent = passed ? '全部缓存检查通过' : '存在未通过的缓存检查';
      state.cacheVerification = [
        `冷缓存读盘：${checks.cold_read_from_disk ? '通过' : '未通过'}`,
        `热缓存命中：${checks.warm_hit_cache ? '通过' : '未通过'}`,
        `热缓存避免读盘：${checks.warm_avoided_disk ? '通过' : '未通过'}`,
        `结果一致：${checks.same_result ? '通过' : '未通过'}`,
      ].join(' · ');
      updateMetrics((payload.warm && payload.warm.metrics) || {});
      renderStats(payload.stats || {});
      setStatus(passed ? '缓存验证通过' : '缓存验证未通过', !passed);
      log(passed ? '冷/热缓存验证通过' : '冷/热缓存验证未通过', !passed);
      // Cache validation exercises the buffer-pool stage, not plan rewriting.
      selectBufferStage();
    } catch (error) { showRequestError(error); }
  }

  function showRequestError(error) {
    renderTable([], []);
    $('#resultMessage').textContent = error.message;
    $('#resultTitle').textContent = '请求失败';
    $('#resultMeta').textContent = error.message;
    $('#runTime').textContent = '失败';
    setStatus('请求失败', true);
    log(error.message, true);
  }

  function setStatus(text, failed = false) {
    $('#queryStatus').textContent = text;
    $('#queryStatus').classList.toggle('status-error', failed);
  }

  function updateLines() {
    const lines = Math.max(7, editor.value.split('\n').length);
    $('#lineNumbers').innerHTML = Array.from({ length: lines }, (_, index) => index + 1).join('<br>');
  }

  function delimited(separator) {
    if (!state.columns.length) return '';
    const escape = (value) => {
      const text = value === null || value === undefined ? '' : String(value);
      return separator === ',' && /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
    };
    return [state.columns, ...state.rows.map((row) => state.columns.map((column) => row[column]))]
      .map((row) => row.map(escape).join(separator)).join('\n');
  }

  function batchDelimited(separator) {
    const escape = (value) => {
      const text = value === null || value === undefined ? 'NULL' : String(value);
      return separator === ',' && /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
    };
    return state.results.map((result, index) => {
      const table = resultTable(result);
      if (!table) return `第 ${index + 1} 条语句：${result.success === false ? errorText(result) : (result.message || `${result.affected_rows || 0} 行受影响`)}`;
      const rows = [table.columns, ...table.rows.map((row) => table.columns.map((column) => row[column]))];
      return [`第 ${index + 1} 条语句`, ...rows.map((row) => row.map(escape).join(separator))].join('\n');
    }).join(`\n${separator === ',' ? '' : '────────'}\n`);
  }

  $$('.pipe-stage').forEach((stage) => stage.addEventListener('click', () => selectStage(stage)));
  $$('.tree-group').forEach((group) => group.addEventListener('click', () => {
    const target = document.getElementById(group.getAttribute('aria-controls'));
    if (!target) return;
    const expanded = group.getAttribute('aria-expanded') === 'true';
    group.setAttribute('aria-expanded', String(!expanded));
    group.classList.toggle('open', !expanded);
    target.classList.toggle('collapsed', expanded);
  }));
  $('#themeToggle').addEventListener('click', () => setTheme(document.body.classList.contains('theme-light') ? 'dark' : 'light'));
  $('#runTop').addEventListener('click', run);
  $('#explainBtn').addEventListener('click', explain);
  $('#cacheBtn').addEventListener('click', verifyCache);
  $('#newQuery').addEventListener('click', () => { editor.value = 'SHOW TABLES;'; updateLines(); editor.focus(); });
  $('#clearBtn').addEventListener('click', () => { editor.value = ''; updateLines(); setStatus('编辑器已清空'); });
  $('#formatBtn').addEventListener('click', () => {
    editor.value = editor.value.replace(/\s+/g, ' ').replace(/\sFROM\s/i, '\nFROM ').replace(/\sJOIN\s/gi, '\nJOIN ').replace(/\sWHERE\s/i, '\nWHERE ').replace(/\sORDER BY\s/i, '\nORDER BY ').trim();
    updateLines();
  });
  editor.addEventListener('input', updateLines);
  editor.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); run(); }
  });
  $('#copyBtn').addEventListener('click', async () => {
    const text = state.results.length > 1 ? batchDelimited('\t') : (delimited('\t') || $('#resultMessage').textContent);
    await navigator.clipboard?.writeText(text);
    $('#copyBtn').textContent = '已复制';
    setTimeout(() => { $('#copyBtn').textContent = '复制'; }, 1200);
  });
  $('#csvBtn').addEventListener('click', () => {
    const csv = state.results.length > 1 ? batchDelimited(',') : delimited(',');
    if (!csv) return;
    const link = document.createElement('a');
    link.href = URL.createObjectURL(new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' }));
    link.download = 'minidb-result.csv';
    link.click();
    URL.revokeObjectURL(link.href);
  });
  $('#flushBtn').addEventListener('click', async () => {
    try {
      const payload = await post('/api/flush');
      renderStats(payload.stats || {});
      $('#bufferDelta').textContent = '脏页已刷新落盘';
      log('缓冲池脏页已刷新到磁盘');
    } catch (error) { showRequestError(error); }
  });
  $('#refreshTree').addEventListener('click', loadTables);
  $('#connectionRow').addEventListener('click', async () => {
    try { await loadStats(); log('数据库连接正常 · 127.0.0.1:8080'); }
    catch (_) { setStatus('数据库连接失败', true); }
  });
  $('#clearLog').addEventListener('click', () => { $('#eventLog').innerHTML = ''; });
  $('#lockTimeout').addEventListener('change', async (event) => {
    try {
      await request('/api/session/settings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lock_timeout: Number(event.target.value) }),
      });
      log(`锁等待超时已设为 ${event.target.value} 秒`);
    } catch (error) { showRequestError(error); }
  });
  window.addEventListener('pagehide', () => {
    if (state.sessionId) navigator.sendBeacon('/api/session/close', JSON.stringify({ session_id: state.sessionId }));
  });

  setTheme(new URLSearchParams(location.search).get('theme') || localStorage.getItem('minidb-theme') || 'dark');
  updateLines();
  renderTable([], []);
  ensureSession().then(() => Promise.all([loadTables(), loadStats(), loadConcurrency()]))
    .then(() => { log(`已连接本地数据库引擎 · 会话 ${state.sessionId.slice(0, 8)}`); setInterval(loadConcurrency, 700); })
    .catch(() => setStatus('数据库连接失败', true));
})();

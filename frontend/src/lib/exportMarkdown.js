import { buildPipelineSteps } from './pipelineSteps';

// Serialize an entire chat session (the shape produced by useSessions +
// App.handleSend) to a single Markdown document for offline review. Aimed
// at testing/QA: we surface everything the UI shows (agent activity,
// stage, meta, citations, confidence, validated flag, command results)
// plus a raw JSON dump of the assistant `data` payload so nothing is lost.

const STATE_LABEL = {
  done: '[done]',
  active: '[active]',
  warn: '[warn]',
  '': '[pending]',
};

export function exportSessionToMarkdown(session) {
  if (!session) return '';
  const lines = [];
  const title = session.title || 'New trace';
  const exportedAt = new Date().toISOString();
  const createdAt = session.createdAt ? new Date(session.createdAt).toISOString() : 'unknown';
  const msgs = session.messages || [];

  lines.push(`# ${title}`);
  lines.push('');
  lines.push(`- **Session id:** \`${session.id || 'n/a'}\``);
  lines.push(`- **Created:** ${createdAt}`);
  lines.push(`- **Exported:** ${exportedAt}`);
  lines.push(`- **Messages:** ${msgs.length}`);
  lines.push('');
  lines.push('---');
  lines.push('');

  msgs.forEach((msg, i) => {
    if (msg.role === 'user') {
      lines.push(`## ${i + 1}. User`);
      lines.push('');
      lines.push(msg.content || '');
      lines.push('');
    } else {
      lines.push(`## ${i + 1}. R-TIE`);
      lines.push('');
      appendAssistantMessage(lines, msg);
    }
    lines.push('---');
    lines.push('');
  });

  return lines.join('\n');
}

function appendAssistantMessage(lines, msg) {
  const data = msg.data;
  const isCommand = data?.type === 'command';

  // Status line — surface in-progress / cancelled / error states so the
  // export is faithful to whatever was on screen at the moment of capture.
  const status = [];
  if (msg.loading) status.push('loading');
  if (msg.streaming) status.push('streaming');
  if (msg.cancelled) status.push('cancelled');
  if (msg.error) status.push('error');
  if (msg.clarification) status.push('clarification');
  if (status.length > 0) {
    lines.push(`> _Status: ${status.join(', ')}_`);
    lines.push('');
  }

  // Agent activity (pipeline steps) — same builder the UI uses.
  if (!isCommand && !msg.error && !msg.clarification) {
    const steps = buildPipelineSteps({
      stage: msg.stage,
      data,
      streaming: msg.streaming,
      loading: msg.loading,
    });
    const started = steps.filter((s) => s.state);
    if (started.length > 0) {
      lines.push('### Agent activity');
      lines.push('');
      steps.forEach((s) => {
        const tag = STATE_LABEL[s.state] ?? `[${s.state}]`;
        const detail = s.liveDetail && s.liveDetail !== s.label ? ` — ${s.liveDetail}` : '';
        lines.push(`- ${tag} ${s.label}${detail}`);
      });
      lines.push('');
    }

    if (msg.stage) {
      lines.push(`- **Last stage:** \`${msg.stage.stage || 'n/a'}\`${msg.stage.message ? ` — ${msg.stage.message}` : ''}`);
      lines.push('');
    }
  }

  if (msg.error) {
    lines.push('### Error');
    lines.push('');
    lines.push('```');
    lines.push(String(msg.error));
    lines.push('```');
    lines.push('');
    return;
  }

  if (msg.clarification) {
    lines.push('### Clarification requested');
    lines.push('');
    lines.push(msg.clarification.message || '');
    lines.push('');
    return;
  }

  if (isCommand) {
    appendCommandResult(lines, data);
    return;
  }

  // Functions analyzed (from meta or final data)
  const functions = data?.functions_analyzed || msg.meta?.functions_analyzed || [];
  if (functions.length > 0) {
    lines.push('### Functions analyzed');
    lines.push('');
    functions.forEach((fn) => lines.push(`- \`${fn}\``));
    lines.push('');
  }

  // Answer markdown — finalised or partial-streaming.
  const markdown = data?.explanation?.markdown ?? msg.streamedMarkdown ?? '';
  if (markdown) {
    lines.push('### Answer');
    lines.push('');
    lines.push(markdown);
    lines.push('');
  } else if (msg.streaming || msg.loading) {
    lines.push('_(no content yet — response was still in progress)_');
    lines.push('');
  }

  // Source citations
  const citations = Array.isArray(data?.source_citations) ? data.source_citations : [];
  if (citations.length > 0) {
    lines.push(`### Sources (${citations.length})`);
    lines.push('');
    citations.forEach((c, i) => {
      const idx = String(i + 1).padStart(2, '0');
      const parts = [`**${idx}.**`];
      if (c.source) parts.push(`\`${c.source}\``);
      if (c.line) parts.push(`L${c.line}`);
      if (c.context) parts.push(`context: _${truncate(c.context, 120)}_`);
      if (c.text) parts.push(`text: _${truncate(c.text, 120)}_`);
      lines.push(`- ${parts.join(' · ')}`);
    });
    lines.push('');
  }

  // Footer signals
  const footer = [];
  if (typeof data?.confidence === 'number') {
    footer.push(`confidence: **${Math.round(data.confidence * 100)}%**`);
  }
  if (typeof data?.validated === 'boolean') {
    footer.push(`validated: **${data.validated}**`);
  }
  if (typeof data?.correlation_id === 'string') {
    footer.push(`correlation: \`${data.correlation_id}\``);
  }
  if (footer.length > 0) {
    lines.push(footer.join(' · '));
    lines.push('');
  }

  // Raw payload — for testing fidelity. Wrapped in <details> so the
  // markdown stays readable on GitHub-flavoured renderers but the full
  // JSON is still recoverable.
  if (data || msg.meta || msg.stage) {
    lines.push('<details><summary>Raw payload</summary>');
    lines.push('');
    lines.push('```json');
    lines.push(JSON.stringify({ data, meta: msg.meta, stage: msg.stage }, null, 2));
    lines.push('```');
    lines.push('');
    lines.push('</details>');
    lines.push('');
  }
}

function appendCommandResult(lines, data) {
  const r = data?.result || {};
  lines.push('### Command result');
  lines.push('');
  if (r.status) lines.push(`- **status:** \`${r.status}\``);
  if (data?.correlation_id) lines.push(`- **correlation:** \`${data.correlation_id}\``);
  lines.push('');
  if (r.report) {
    lines.push('```');
    lines.push(String(r.report));
    lines.push('```');
    lines.push('');
  }
  const rest = Object.entries(r).filter(([k]) => k !== 'status' && k !== 'report');
  if (rest.length > 0) {
    lines.push('```json');
    lines.push(JSON.stringify(Object.fromEntries(rest), null, 2));
    lines.push('```');
    lines.push('');
  }
}

function truncate(s, n) {
  const t = String(s).replace(/\s+/g, ' ').trim();
  return t.length > n ? t.slice(0, n - 1) + '…' : t;
}

export function downloadSessionAsMarkdown(session) {
  const content = exportSessionToMarkdown(session);
  const safeTitle = (session?.title || 'rtie-trace')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60) || 'rtie-trace';
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const filename = `${safeTitle}-${stamp}.md`;

  const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

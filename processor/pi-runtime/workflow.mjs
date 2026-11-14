// Workflow bodies stay separate from the small, frequently polled progress stream.
export function workflowRecorder(onRecord, secret = '') {
  const cleanText = value => {
    let text = secret ? value.split(secret).join('[credential omitted]') : value;
    if (/^\s*[\[{]/.test(text)) {
      try { text = JSON.stringify(project(JSON.parse(text))); } catch { /* Keep ordinary prose as text. */ }
    }
    text = text.replace(/data:[\w.+/-]+;base64,[A-Za-z0-9+/=\s]+/g, '[binary omitted]')
      .replace(/\b(Bearer|Basic)\s+[A-Za-z0-9+/_.=-]+/gi, '$1 [credential omitted]')
      .replace(/((?:api[_-]?key|(?:access[_-]?)?token|client[_-]?secret|password)\s*[=:]\s*)[^\s,;"']+/gi, '$1[credential omitted]');
    text = text.replace(/https?:\/\/[^\s<>"']+/gi, address => {
      try {
        const url = new URL(address);
        let changed = !!(url.username || url.password);
        url.username = ''; url.password = '';
        for (const key of [...url.searchParams.keys()]) {
          if (/token|secret|password|signature|credential|auth|(?:^|[_-])(?:key|sig)(?:$|[_-])/i.test(key)) {
            url.searchParams.set(key, '[omitted]'); changed = true;
          }
        }
        return changed ? url.toString() : address;
      } catch { return address; }
    });
    return text.length > 524288 ? {omitted: 'text_limit', chars: text.length, preview: text.slice(0, 65536)} : text;
  };
  const project = (value, depth = 0) => {
    if (depth > 40) return {omitted: 'depth_limit'};
    if (typeof value === 'string') return cleanText(value);
    if (value === null || typeof value !== 'object') return value;
    if (['thinking', 'reasoning', 'redacted_thinking'].includes(value.type)) return {omitted: 'private_reasoning'};
    if (value.type === 'image' || value.type === 'image_url') return {type: value.type, omitted: 'image_body', mimeType: value.mimeType};
    if (Array.isArray(value)) return value.length > 10000 ? {omitted: 'array_limit', items: value.length} : value.map(item => project(item, depth + 1));
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key,
      /(?:authorization|credential|password|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|cookie|headers|signature|reasoning|thinking|(?:^|[_-])(?:token|auth)(?:$|[_-]))/i.test(key)
        ? '[omitted]' : project(item, depth + 1)]));
  };
  return event => {
    const record = project({time: new Date().toISOString(), ...event});
    const serialized = JSON.stringify(record);
    if (Buffer.byteLength(serialized, 'utf8') > 1048576) {
      record.body = {omitted: 'record_limit', bytes: Buffer.byteLength(serialized, 'utf8')};
    }
    onRecord(record);
  };
}

export function requestBody(payload) {
  // Provider request envelopes may contain credentials; only transcript/tool fields belong in the UI.
  return {messages: payload?.messages ?? [], tools: payload?.tools ?? [],
    model: payload?.model, max_tokens: payload?.max_tokens, stream: payload?.stream};
}

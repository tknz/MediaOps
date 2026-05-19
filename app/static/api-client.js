(function () {
  function query(params) {
    const search = new URLSearchParams();
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== '') search.set(key, value);
    });
    const text = search.toString();
    return text ? `?${text}` : '';
  }

  async function request(path, options) {
    const response = await fetch(path, {
      cache: 'no-store',
      credentials: 'same-origin',
      headers: {
        Accept: 'application/json',
        ...(options && options.body ? { 'Content-Type': 'application/json' } : {}),
      },
      ...options,
    });

    const contentType = response.headers.get('content-type') || '';
    const payload = contentType.includes('application/json') ? await response.json() : null;
    if (!response.ok) {
      const message = payload && payload.error ? payload.error : `Request failed: ${response.status}`;
      throw new Error(message);
    }
    return payload;
  }

  window.MediaOpsApi = {
    query,
    get(path, params) {
      return request(`${path}${query(params)}`);
    },
    post(path, payload) {
      return request(path, { method: 'POST', body: JSON.stringify(payload || {}) });
    },
    delete(path, payload) {
      return request(path, { method: 'DELETE', body: JSON.stringify(payload || {}) });
    },
  };
})();

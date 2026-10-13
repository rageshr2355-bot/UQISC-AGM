// Shared helpers for the UQISC AGM pages.
const UQ = {
  logo(cls = "logo") {
    const rays = Array.from({ length: 12 }, (_, i) => {
      const a = (i * Math.PI) / 6;
      const p = (r) => `${(108 + r * Math.cos(a)).toFixed(2)} ${(18 + r * Math.sin(a)).toFixed(2)}`;
      return `M${p(7.5)} L${p(12)}`;
    }).join(" ");
    return `<svg class="${cls}" viewBox="0 0 208 78" role="img" aria-label="UQISC">
      <line x1="24" y1="5" x2="184" y2="5" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>
      <text x="34" y="62" textLength="64" lengthAdjust="spacingAndGlyphs" font-family="Figtree, sans-serif" font-size="54" font-weight="500" fill="currentColor">uq</text>
      <rect x="104" y="35" width="8" height="27" rx="1.5" fill="currentColor"/>
      <g class="sun"><circle cx="108" cy="18" r="5" fill="currentColor"/>
        <path d="${rays}" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></g>
      <text x="118" y="62" textLength="58" lengthAdjust="spacingAndGlyphs" font-family="Figtree, sans-serif" font-size="54" font-weight="500" fill="currentColor">sc</text>
      <line x1="24" y1="73" x2="184" y2="73" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>
    </svg>`;
  },

  esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  },

  async post(path, body = {}, headers = {}) {
    let res;
    try {
      res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...headers },
        body: JSON.stringify(body),
      });
    } catch {
      throw new Error("Can't reach the server. Check your connection.");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.error || "Something went wrong.");
      err.status = res.status;
      throw err;
    }
    return data;
  },

  // Live state via Server-Sent Events, reconnecting on drop.
  // getParams() is re-read on every (re)connect so token changes take effect.
  stream(getParams, onData, onStatus = () => {}) {
    let es = null, closed = false, retry = null;
    const open = () => {
      if (closed) return;
      es = new EventSource("/api/stream?" + new URLSearchParams(getParams()));
      es.onopen = () => onStatus(true);
      es.onmessage = (e) => onData(JSON.parse(e.data));
      es.onerror = () => {
        onStatus(false);
        es.close();
        clearTimeout(retry);
        retry = setTimeout(open, 2000);
      };
    };
    open();
    return {
      reconnect() { es && es.close(); clearTimeout(retry); open(); },
      close() { closed = true; es && es.close(); clearTimeout(retry); },
    };
  },

  toast(msg, bad = false) {
    let el = document.querySelector(".toast");
    if (!el) {
      el = document.createElement("div");
      el.className = "toast";
      el.setAttribute("role", "status");
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.toggle("bad", bad);
    el.classList.add("show");
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove("show"), 2600);
  },

  storage: {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch {} },
    del(k) { try { localStorage.removeItem(k); } catch {} },
  },

  // Replace innerHTML but keep typed values, focus and cursor in inputs with an id.
  render(el, html) {
    const saved = {};
    el.querySelectorAll("input[id], textarea[id], select[id]").forEach((i) => {
      saved[i.id] = i.type === "checkbox" ? i.checked : i.value;
    });
    const active = document.activeElement && el.contains(document.activeElement) ? document.activeElement : null;
    const focusId = active && active.id;
    const sel = active && "selectionStart" in active ? [active.selectionStart, active.selectionEnd] : null;
    el.innerHTML = html;
    for (const [id, v] of Object.entries(saved)) {
      const i = document.getElementById(id);
      if (!i || i.dataset.reset) continue;
      if (i.type === "checkbox") i.checked = v; else i.value = v;
    }
    if (focusId) {
      const f = document.getElementById(focusId);
      if (f) {
        f.focus();
        if (sel && f.setSelectionRange) try { f.setSelectionRange(...sel); } catch {}
      }
    }
  },
};

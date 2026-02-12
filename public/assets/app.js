(() => {
  "use strict";

  const API_BASE = window.location.origin;
  const $ = (id) => document.getElementById(id);

  // ==== API Functions ====
  async function apiGet(path) {
    const res = await fetch(path, { headers: { "Accept": "application/json" } });
    if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`);
    return res.json();
  }

  async function apiPostJSON(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  async function apiPostForm(path, formData) {
    const res = await fetch(path, { method: "POST", body: formData });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  // ==== Page Navigation ====
  function showLanding() {
    $("landingPage").classList.remove("hidden");
    $("dashboardPage").classList.add("hidden");
  }

  function showDashboard() {
    $("landingPage").classList.add("hidden");
    $("dashboardPage").classList.remove("hidden");
    initDashboard();
  }

  $("goDashboardBtn")?.addEventListener("click", showDashboard);
  $("goDashboardBtn2")?.addEventListener("click", showDashboard);
  $("backLandingBtn")?.addEventListener("click", showLanding);

  // ==== Tabs ====
  let currentGroup = "Permanentes";

  document.querySelectorAll(".tabBtn").forEach(btn => {
    btn.addEventListener("click", () => {
      const target = btn.getAttribute("data-tab");
      document.querySelectorAll(".tabPanel").forEach(p => p.classList.add("hidden"));
      document.querySelectorAll(".tabBtn").forEach(b => {
        b.classList.remove("gradient-bg", "text-white");
        b.classList.add("bg-white", "border", "border-gray-200");
      });
      $(target)?.classList.remove("hidden");
      btn.classList.add("gradient-bg", "text-white");
      btn.classList.remove("bg-white", "border", "border-gray-200");
    });
  });

  // ==== Health Check ====
  async function pingHealth() {
    try {
      const data = await apiGet("/api/health");
      $("healthBadge").textContent = data.success ? "✓ OK" : "✗ Error";
      $("apiStatus").textContent = `OK · Gemini ${data.gemini_configured ? "✓" : "✗"}`;
    } catch (e) {
      $("healthBadge").textContent = "✗ Offline";
      $("apiStatus").textContent = `Error: ${e.message}`;
    }
  }

  // ==== Commissions ====
  async function loadCommissions(q = "") {
    const group = $("groupSelect").value || "Permanentes";
    currentGroup = group;
    const data = await apiGet(`/api/commissions?group=${encodeURIComponent(group)}&q=${encodeURIComponent(q)}`);
    const items = data.commissions || [];
    $("commissionsHint").textContent = `${items.length} comisión(es) en ${group}`;
    renderCommissions(items);
  }

  function renderCommissions(items) {
    const el = $("commissionsList");
    if (!el) return;

    if (!items.length) {
      el.innerHTML = `<div class="text-sm text-gray-500">No hay comisiones.</div>`;
      return;
    }

    el.innerHTML = items.map(c => `
      <button 
        class="w-full text-left bg-white rounded-lg border border-gray-200 p-3 hover:border-purple-500 hover:shadow-md transition"
        onclick="window.openCommission('${c.group}', '${c.commission_name}')"
      >
        <div class="font-bold text-gray-900 text-sm">${c.nombre}</div>
        <div class="text-xs text-gray-500">${c.total_sessions} sesiones</div>
      </button>
    `).join("");
  }

  async function openCommission(group, name) {
    $("commissionTitle").textContent = name;
    $("commissionSubtitle").textContent = `Cargando sesiones...`;
    $("commissionMeta").textContent = `${group}`;
    $("commissionDetail").innerHTML = `<div class="text-sm text-gray-500 animate-pulse">Cargando sesiones...</div>`;

    try {
      const data = await apiGet(`/api/commissions/${encodeURIComponent(group)}/${encodeURIComponent(name)}/sessions`);
      
      console.log("DEBUG - Response from API:", data);
      
      if (!data.success) {
        $("commissionDetail").innerHTML = `<div class="text-sm text-red-600">Error: ${data.error || "Desconocido"}</div>`;
        return;
      }

      const c = data.commission;
      console.log("DEBUG - Commission data:", c);
      console.log("DEBUG - Years:", c.years);
      console.log("DEBUG - Sessions by year:", c.sessions_by_year);

      const totalSessions = Object.values(c.sessions_by_year || {}).reduce((sum, arr) => sum + arr.length, 0);
      $("commissionSubtitle").textContent = `${c.years.length} año(s) · ${totalSessions} sesiones`;

      if (!c.years || c.years.length === 0) {
        $("commissionDetail").innerHTML = `<div class="text-sm text-gray-500">No hay sesiones registradas en el historial.csv</div>`;
        return;
      }

      $("commissionDetail").innerHTML = renderCommissionYears(c);
    } catch (e) {
      console.error("ERROR loading commission:", e);
      $("commissionDetail").innerHTML = `<div class="text-sm text-red-600">Error: ${e.message}</div>`;
    }
  }

  function renderCommissionYears(c) {
    return c.years.map(year => {
      const sessions = c.sessions_by_year[year] || [];
      console.log(`DEBUG - Year ${year} has ${sessions.length} sessions`);
      
      if (sessions.length === 0) {
        return `
          <div class="bg-white rounded-xl border border-gray-200 p-4">
            <div class="flex items-center justify-between mb-3">
              <div class="text-lg font-bold text-gray-900">${year}</div>
              <div class="text-xs text-gray-400">0 sesiones</div>
            </div>
            <div class="text-sm text-gray-400">No hay sesiones registradas para este año</div>
          </div>
        `;
      }

      const rows = sessions.slice(0, 100).map(s => renderSessionRow(s)).join("");
      return `
        <div class="bg-white rounded-xl border border-gray-200 p-4">
          <div class="flex items-center justify-between mb-3">
            <div class="text-lg font-bold text-gray-900">${year}</div>
            <div class="text-xs text-gray-500">${sessions.length} sesiones</div>
          </div>
          <div class="overflow-x-auto">
            <table class="w-full text-sm">
              <thead class="text-gray-500 border-b border-gray-200">
                <tr>
                  <th class="text-left py-2 px-2 w-24">Mes</th>
                  <th class="text-left py-2 px-2 w-28">Fecha</th>
                  <th class="text-left py-2 px-2 w-24">Estado</th>
                  <th class="text-left py-2 px-2">Documentos</th>
                  <th class="text-center py-2 px-2 w-16">TXT</th>
                </tr>
              </thead>
              <tbody>
                ${rows}
              </tbody>
            </table>
          </div>
        </div>
      `;
    }).join("");
  }

  function renderSessionRow(s) {
    const estado = s.Estado || "-";
    const estadoBadge = `<span class="inline-flex items-center px-2 py-1 rounded-full text-xs font-semibold ${getEstadoColor(estado)}">${estado}</span>`;
    const transcript = s.transcript ? "📄" : "—";

    const docs = [];
    if (s.Citacion && s.Citacion !== "No" && s.Citacion.startsWith("http")) {
      docs.push(`<a href="${s.Citacion}" target="_blank" rel="noopener noreferrer" class="inline-flex items-center px-2 py-1 rounded text-xs font-semibold bg-blue-100 text-blue-700 hover:bg-blue-200 transition">Citación ↗</a>`);
    }
    if (s.Cuenta && s.Cuenta !== "No" && s.Cuenta.startsWith("http")) {
      docs.push(`<a href="${s.Cuenta}" target="_blank" rel="noopener noreferrer" class="inline-flex items-center px-2 py-1 rounded text-xs font-semibold bg-green-100 text-green-700 hover:bg-green-200 transition">Cuenta ↗</a>`);
    }
    if (s.Acta && s.Acta !== "No" && s.Acta.startsWith("http")) {
      docs.push(`<a href="${s.Acta}" target="_blank" rel="noopener noreferrer" class="inline-flex items-center px-2 py-1 rounded text-xs font-semibold bg-purple-100 text-purple-700 hover:bg-purple-200 transition">Acta ↗</a>`);
    }

    const docsHTML = docs.length ? docs.join(" ") : `<span class="text-xs text-gray-400">Sin documentos</span>`;

    return `
      <tr class="border-t border-gray-100 hover:bg-gray-50">
        <td class="py-2 px-2 text-xs text-gray-600">${s.Mes || ""}</td>
        <td class="py-2 px-2 text-xs">${s.Fecha || ""}</td>
        <td class="py-2 px-2">${estadoBadge}</td>
        <td class="py-2 px-2"><div class="flex flex-wrap gap-1">${docsHTML}</div></td>
        <td class="py-2 px-2 text-center text-lg">${transcript}</td>
      </tr>
    `;
  }

  function getEstadoColor(estado) {
    const colors = {
      'CITADA': 'bg-blue-100 text-blue-700',
      'CELEBRADA': 'bg-green-100 text-green-700',
      'SUSPENDIDA': 'bg-yellow-100 text-yellow-700',
      'FRACASADA': 'bg-red-100 text-red-700',
    };
    return colors[estado] || 'bg-gray-100 text-gray-700';
  }

  // ==== Politicians ====
  async function loadPoliticians(q = "") {
    const data = await apiGet(`/api/politicians?q=${encodeURIComponent(q)}`);
    const items = data.politicians || [];
    renderPoliticians(items);
  }

  function renderPoliticians(items) {
    const el = $("politiciansGrid");
    if (!el) return;

    if (!items.length) {
      el.innerHTML = `<div class="text-sm text-gray-500">No hay resultados.</div>`;
      return;
    }

    el.innerHTML = items.map(p => `
      <div class="card-hover bg-white rounded-2xl border border-gray-200 p-4">
        <div class="font-bold text-gray-900">${p.nombre}</div>
        <div class="text-xs text-gray-500 mb-3">${p.cargo || "Parlamentario"} · ${p.chamber || ""}</div>
        <div class="grid grid-cols-2 gap-2">
          ${p.url_ficha ? `<a class="text-center text-xs px-3 py-2 rounded-lg gradient-bg text-white font-semibold" href="${p.url_ficha}" target="_blank">Ver ficha →</a>` : `<div class="text-xs text-gray-400 text-center py-2">Sin ficha</div>`}
          <button onclick="window.openKomModal('${p.chamber || 'camara'}', '${p.id}', '${encodeURIComponent(p.nombre)}', '${encodeURIComponent(p.cargo || 'Parlamentario')}')" class="text-xs px-3 py-2 rounded-lg bg-green-600 hover:bg-green-700 text-white font-semibold transition">
            Perfil KOM
          </button>
        </div>
      </div>
    `).join("");
  }

  // ==== KOM Modal ====
  let currentKomProfile = null;
  let editingTopicoIdx = null;

  async function openKomModal(chamber, id, nameEnc, cargoEnc) {
    const nombre = decodeURIComponent(nameEnc);
    const cargo = decodeURIComponent(cargoEnc || "Parlamentario");
    
    console.log("Opening KOM modal for:", { chamber, id, nombre, cargo });
    
    $("komNombre").textContent = nombre;
    $("komCargo").textContent = cargo;

    try {
      const data = await apiGet(`/api/kom/${chamber}/${id}`);
      const profile = data.profile || {};

      currentKomProfile = {
        chamber,
        id,
        nombre,
        cargo,
        foto_url: profile.foto_url || "",
        biografia: profile.biografia || "",
        email: profile.email || "",
        telefono: profile.telefono || "",
        web: profile.web || "",
        topicos: profile.topicos || [],
        notas: profile.notas || profile.notes || "",
        links: profile.links || [],
      };

      $("komFotoUrl").value = currentKomProfile.foto_url;
      $("komBiografia").value = currentKomProfile.biografia;
      $("komEmail").value = currentKomProfile.email;
      $("komTelefono").value = currentKomProfile.telefono;
      $("komWeb").value = currentKomProfile.web;
      $("komNotas").value = currentKomProfile.notas;
      
      updateKomFoto();
      renderKomTopicos();
      renderKomLinks();

      $("komModal").classList.remove("hidden");
      $("komModal").classList.add("flex");
    } catch (e) {
      console.error("Error loading KOM profile:", e);
      alert("Error al cargar perfil KOM: " + e.message);
    }
  }

  function closeKomModal() {
    $("komModal").classList.add("hidden");
    $("komModal").classList.remove("flex");
    currentKomProfile = null;
    editingTopicoIdx = null;
    hideTopicoEditor();
  }

  function updateKomFoto() {
    const url = $("komFotoUrl").value.trim();
    if (currentKomProfile) currentKomProfile.foto_url = url;
    
    if (url) {
      $("komFoto").src = url;
      $("komFoto").classList.remove("hidden");
      $("komFotoPlaceholder").classList.add("hidden");
    } else {
      $("komFoto").classList.add("hidden");
      $("komFotoPlaceholder").classList.remove("hidden");
    }
  }

  function renderKomTopicos() {
    const el = $("komTopicos");
    if (!currentKomProfile.topicos.length) {
      el.innerHTML = `<div class="text-xs text-gray-400 p-3 bg-gray-50 rounded">Sin tópicos agregados</div>`;
      return;
    }
    
    el.innerHTML = currentKomProfile.topicos.map((topico, idx) => `
      <div class="bg-green-50 border border-green-200 rounded-lg p-3">
        <div class="flex items-start justify-between gap-2">
          <div class="flex-1">
            <div class="font-bold text-gray-900 text-sm">${topico.titulo}</div>
            <div class="text-xs text-gray-600 mt-1">${topico.contenido}</div>
          </div>
          <div class="flex gap-1">
            <button onclick="window.editTopico(${idx})" class="text-green-600 hover:text-green-800 text-xs px-2 py-1">Editar</button>
            <button onclick="window.removeTopico(${idx})" class="text-red-600 hover:text-red-800 text-xs px-2 py-1">Eliminar</button>
          </div>
        </div>
      </div>
    `).join("");
  }

  function showTopicoEditor() {
    const titulo = $("komTopicoTitulo").value.trim();
    if (!titulo) {
      alert("Escribe un título para el tópico");
      return;
    }
    
    $("topicoEditorTitle").textContent = "Nuevo Tópico: " + titulo;
    $("topicoContenido").value = "";
    $("topicoEditor").classList.remove("hidden");
    editingTopicoIdx = null;
  }

  function editTopico(idx) {
    const topico = currentKomProfile.topicos[idx];
    $("komTopicoTitulo").value = topico.titulo;
    $("topicoEditorTitle").textContent = "Editar Tópico: " + topico.titulo;
    $("topicoContenido").value = topico.contenido;
    $("topicoEditor").classList.remove("hidden");
    editingTopicoIdx = idx;
  }

  function saveTopico() {
    const titulo = $("komTopicoTitulo").value.trim();
    const contenido = $("topicoContenido").value.trim();
    
    if (!titulo || !contenido) {
      alert("Completa título y contenido");
      return;
    }

    if (editingTopicoIdx !== null) {
      currentKomProfile.topicos[editingTopicoIdx] = { titulo, contenido };
    } else {
      currentKomProfile.topicos.push({ titulo, contenido });
    }

    $("komTopicoTitulo").value = "";
    hideTopicoEditor();
    renderKomTopicos();
  }

  function removeTopico(idx) {
    if (confirm("¿Eliminar este tópico?")) {
      currentKomProfile.topicos.splice(idx, 1);
      renderKomTopicos();
    }
  }

  function hideTopicoEditor() {
    $("topicoEditor").classList.add("hidden");
    $("topicoContenido").value = "";
    editingTopicoIdx = null;
  }

  function renderKomLinks() {
    const el = $("komLinks");
    if (!currentKomProfile.links.length) {
      el.innerHTML = `<div class="text-xs text-gray-400 p-2 bg-gray-50 rounded">Sin enlaces</div>`;
      return;
    }
    el.innerHTML = currentKomProfile.links.map((link, idx) => `
      <div class="flex items-center gap-2 p-2 bg-gray-50 rounded-lg border border-gray-200">
        <a href="${link.url}" target="_blank" class="flex-1 text-sm text-green-700 hover:underline">${link.title}</a>
        <button onclick="window.removeKomLink(${idx})" class="text-red-600 hover:text-red-800 text-xs font-semibold">Eliminar</button>
      </div>
    `).join("");
  }

  function addKomLink() {
    const title = $("komNewLinkTitle").value.trim();
    const url = $("komNewLinkUrl").value.trim();
    if (!title || !url) {
      alert("Completa título y URL");
      return;
    }
    currentKomProfile.links.push({ title, url });
    $("komNewLinkTitle").value = "";
    $("komNewLinkUrl").value = "";
    renderKomLinks();
  }

  function removeKomLink(idx) {
    currentKomProfile.links.splice(idx, 1);
    renderKomLinks();
  }

  async function saveKomProfile() {
    if (!currentKomProfile) return;

    currentKomProfile.biografia = $("komBiografia").value || "";
    currentKomProfile.email = $("komEmail").value || "";
    currentKomProfile.telefono = $("komTelefono").value || "";
    currentKomProfile.web = $("komWeb").value || "";
    currentKomProfile.notas = $("komNotas").value || "";

    try {
      await apiPostJSON(`/api/kom/${currentKomProfile.chamber}/${currentKomProfile.id}`, {
        foto_url: currentKomProfile.foto_url,
        biografia: currentKomProfile.biografia,
        email: currentKomProfile.email,
        telefono: currentKomProfile.telefono,
        web: currentKomProfile.web,
        topicos: currentKomProfile.topicos,
        notas: currentKomProfile.notas,
        links: currentKomProfile.links,
      });
      alert("✓ Perfil KOM guardado correctamente");
      closeKomModal();
    } catch (e) {
      alert(`✗ Error al guardar: ${e.message}`);
    }
  }

  // ==== Global functions ====
  window.openCommission = openCommission;
  window.openKomModal = openKomModal;
  window.closeKomModal = closeKomModal;
  window.updateKomFoto = updateKomFoto;
  window.showTopicoEditor = showTopicoEditor;
  window.editTopico = editTopico;
  window.saveTopico = saveTopico;
  window.removeTopico = removeTopico;
  window.hideTopicoEditor = hideTopicoEditor;
  window.addKomLink = addKomLink;
  window.removeKomLink = removeKomLink;
  window.saveKomProfile = saveKomProfile;

  // ==== Activity ====
  async function loadActivity() {
    const group = $("activityGroup").value || "";
    const status = $("activityStatus").value || "";
    const q = $("activityQ").value || "";

    const data = await apiGet(`/api/activity?group=${encodeURIComponent(group)}&status=${encodeURIComponent(status)}&q=${encodeURIComponent(q)}`);
    const items = data.items || [];
    renderActivity(items);
  }

  function renderActivity(items) {
    const el = $("activityList");
    if (!el) return;

    if (!items.length) {
      el.innerHTML = `<div class="text-sm text-gray-500">No hay actividad para esos filtros.</div>`;
      return;
    }

    el.innerHTML = items.slice(0, 120).map(x => `
      <div class="bg-white border border-gray-200 rounded-2xl p-4">
        <div class="flex items-start justify-between gap-3">
          <div>
            <div class="font-bold text-gray-900">${x.commission}</div>
            <div class="text-xs text-gray-500">${x.group} · ${x.fecha || "—"} · sesión ${x.session_id || "—"}</div>
          </div>
          <span class="text-xs px-2 py-1 rounded-full ${getEstadoColor(x.estado || "—")}">${x.estado || "—"}</span>
        </div>
        ${x.citacion && x.citacion.startsWith("http") ? `<div class="mt-2"><a href="${x.citacion}" target="_blank" class="text-sm text-purple-700 hover:underline">Ver citación ↗</a></div>` : ``}
      </div>
    `).join("");
  }

  // ==== News ====
  async function loadNews() {
    const source = $("newsSource").value || "diario_oficial";
    const q = $("newsQ").value || "";
    const data = await apiGet(`/api/news?source=${encodeURIComponent(source)}&q=${encodeURIComponent(q)}`);
    const items = data.items || [];
    renderNews(items);
  }

  function renderNews(items) {
    const el = $("newsList");
    if (!el) return;

    if (!items.length) {
      el.innerHTML = `<div class="text-sm text-gray-500">No hay noticias cargadas.</div>`;
      return;
    }

    el.innerHTML = items.slice(0, 120).map(it => `
      <div class="bg-white border border-gray-200 rounded-2xl p-4">
        <div class="font-bold text-gray-900">${it.title || it.titulo || "Sin título"}</div>
        <div class="text-xs text-gray-500">${it.date || it.fecha || ""}</div>
        ${it.summary || it.resumen ? `<div class="mt-2 text-sm text-gray-700">${it.summary || it.resumen}</div>` : ``}
        ${it.url ? `<a class="mt-2 inline-block text-sm text-purple-700 font-semibold hover:underline" href="${it.url}" target="_blank">Abrir →</a>` : ``}
      </div>
    `).join("");
  }

  // ==== Chat ====
  async function sendChat() {
    const msg = ($("chatInput").value || "").trim();
    if (!msg) return;

    $("chatBtn").disabled = true;
    $("chatBtn").textContent = "Enviando...";
    $("chatMeta").textContent = `POST /api/chat · ${new Date().toLocaleTimeString("es-CL")}`;

    try {
      const data = await apiPostJSON("/api/chat", { message: msg });
      $("chatOutput").textContent = data.response || JSON.stringify(data, null, 2);
    } catch (e) {
      $("chatOutput").textContent = `❌ ${e.message}`;
    } finally {
      $("chatBtn").disabled = false;
      $("chatBtn").textContent = "Enviar";
    }
  }

  async function uploadFile() {
    const f = $("uploadFile").files?.[0];
    if (!f) {
      $("uploadStatus").textContent = "Selecciona un archivo";
      return;
    }

    $("uploadBtn").disabled = true;
    $("uploadStatus").textContent = "Subiendo...";

    try {
      const fd = new FormData();
      fd.append("file", f);
      const data = await apiPostForm("/api/upload", fd);
      $("uploadStatus").textContent = data.success ? `✓ Subido: ${data.saved_as}` : `✗ ${data.error || "Error"}`;
    } catch (e) {
      $("uploadStatus").textContent = `❌ ${e.message}`;
    } finally {
      $("uploadBtn").disabled = false;
    }
  }

  // ==== Init ====
  async function initDashboard() {
    await pingHealth();
    currentGroup = $("groupSelect").value || "Permanentes";
    await loadCommissions("");
    await loadPoliticians("");
  }

  // ==== Bind UI ====
  $("backendOrigin").textContent = API_BASE;

  $("groupSelect")?.addEventListener("change", async () => {
    currentGroup = $("groupSelect").value;
    await loadCommissions(($("commissionSearch").value || "").trim());
  });

  $("commissionSearchBtn")?.addEventListener("click", async () => {
    await loadCommissions(($("commissionSearch").value || "").trim());
  });

  $("commissionSearch")?.addEventListener("keydown", async (e) => {
    if (e.key === "Enter") await loadCommissions(($("commissionSearch").value || "").trim());
  });

  $("politicianSearchBtn")?.addEventListener("click", async () => {
    await loadPoliticians(($("politicianSearch").value || "").trim());
  });

  $("politicianSearch")?.addEventListener("keydown", async (e) => {
    if (e.key === "Enter") await loadPoliticians(($("politicianSearch").value || "").trim());
  });

  $("activityBtn")?.addEventListener("click", loadActivity);
  $("newsBtn")?.addEventListener("click", loadNews);

  $("chatBtn")?.addEventListener("click", sendChat);
  $("chatInput")?.addEventListener("keydown", (e) => {
    if (e.ctrlKey && e.key === "Enter") sendChat();
  });

  $("uploadBtn")?.addEventListener("click", uploadFile);

  pingHealth();
})();
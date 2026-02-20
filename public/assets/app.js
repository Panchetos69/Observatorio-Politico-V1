// ============================================================
// assets/app.js — VERSIÓN MEJORADA
// Cambios:
//  1. renderPoliticians → muestra foto real desde url_ficha
//  2. Perfil KOM → diseño elegante con foto automática
//  3. renderSessionRow → columna Presentaciones separada
//  4. Fix bug: camara param correcto en loadPoliticians
// ============================================================

// ─── Helpers base ────────────────────────────────────────────
function $(id) { return document.getElementById(id); }

async function apiGet(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function apiPostJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ─── Helpers de foto parlamentario ───────────────────────────
/**
 * Intenta extraer la URL de foto oficial a partir de url_ficha.
 * Diputados: https://www.camara.cl/diputados/detalle/...?prmID=XXXX → img?prmID=XXXX
 * Senado: https://www.senado.cl/senadores/detalle/... → intenta construir
 */
function getFotoFromFicha(urlFicha) {
  if (!urlFicha) return "";
  try {
    // Cámara de Diputados
    if (urlFicha.includes("camara.cl")) {
      // Extraer prmID
      const match = urlFicha.match(/[?&]prmID=(\d+)/i);
      if (match) {
        return `https://www.camara.cl/img.aspx?prmID=${match[1]}&prmTipo=DIPUTADOS`;
      }
    }
    // Senado
    if (urlFicha.includes("senado.cl")) {
      // Intentar extraer ID de URL como /senadores/detalle/XXXX
      const match = urlFicha.match(/\/(\d+)\/?$/);
      if (match) {
        return `https://www.senado.cl/senadores/img/${match[1]}.jpg`;
      }
    }
  } catch {}
  return "";
}

// Avatar con foto o iniciales
function avatarHTML(p, size = "lg") {
  const isSenado = (p.chamber || "").toLowerCase() === "senado";
  const bgColor = isSenado ? "bg-red-600" : "bg-purple-600";
  const dim = size === "lg" ? "w-16 h-16 text-lg" : "w-12 h-12 text-sm";
  const initials = p.nombre.split(" ").slice(0, 2).map(x => x[0] || "").join("").toUpperCase();

  // Intentar foto oficial
  const fotoUrl = getFotoFromFicha(p.url_ficha);

  if (fotoUrl) {
    return `
      <div class="${dim} shrink-0 rounded-full overflow-hidden border-2 ${isSenado ? "border-red-200" : "border-purple-200"} shadow">
        <img src="${fotoUrl}" alt="${p.nombre}"
             class="w-full h-full object-cover object-top"
             onerror="this.parentElement.innerHTML='<div class=\\'w-full h-full ${bgColor} text-white flex items-center justify-center font-bold\\'>${initials}</div>'" />
      </div>
    `;
  }
  return `<div class="${dim} shrink-0 rounded-full ${bgColor} text-white flex items-center justify-center font-bold">${initials}</div>`;
}

function chamberBadge(chamber) {
  if (chamber === "senado")
    return `<span class="text-xs font-bold px-2 py-0.5 rounded-full bg-red-100 text-red-700">Senador/a</span>`;
  if (chamber === "camara" || chamber === "diputado")
    return `<span class="text-xs font-bold px-2 py-0.5 rounded-full bg-purple-100 text-purple-700">Diputado/a</span>`;
  return `<span class="text-xs font-bold px-2 py-0.5 rounded-full bg-gray-100 text-gray-500">${chamber}</span>`;
}

function avatarClass(chamber) {
  if (chamber === "senado") return "bg-red-600";
  if (chamber === "camara" || chamber === "diputado") return "bg-purple-600";
  return "bg-gray-600";
}

// ─── Estado colores ───────────────────────────────────────────
function getEstadoColor(estado) {
  const colors = {
    'CITADA':     'bg-blue-100 text-blue-700',
    'CELEBRADA':  'bg-green-100 text-green-700',
    'SUSPENDIDA': 'bg-yellow-100 text-yellow-700',
    'FRACASADA':  'bg-red-100 text-red-700',
  };
  return colors[estado] || 'bg-gray-100 text-gray-700';
}

// ─── Sessions ─────────────────────────────────────────────────
// Variable global para saber qué cámara está activa en comisiones
window._currentComCamara = window._currentComCamara || "diputados";

function renderSessionRow(s) {
  const estado = s.Estado || "-";
  const estadoBadge = `<span class="inline-flex items-center px-2 py-1 rounded-full text-xs font-semibold ${getEstadoColor(estado)}">${estado}</span>`;
  const transcript = s.transcript ? `<span title="Transcripción disponible" class="text-green-600 text-lg">📄</span>` : `<span class="text-gray-300">—</span>`;

  // ── Documentos (Citación, Cuenta, Acta) ──
  const docs = [];
  if (s.Citacion && s.Citacion !== "No" && s.Citacion.startsWith("http")) {
    docs.push(`<a href="${s.Citacion}" target="_blank" rel="noopener" title="Citación"
      class="inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-semibold bg-blue-50 text-blue-700 border border-blue-200 hover:bg-blue-100 transition">
      📋 Citación</a>`);
  }
  if (s.Cuenta && s.Cuenta !== "No" && s.Cuenta.startsWith("http")) {
    docs.push(`<a href="${s.Cuenta}" target="_blank" rel="noopener" title="Cuenta"
      class="inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-semibold bg-green-50 text-green-700 border border-green-200 hover:bg-green-100 transition">
      📊 Cuenta</a>`);
  }
  if (s.Acta && s.Acta !== "No" && s.Acta.startsWith("http")) {
    docs.push(`<a href="${s.Acta}" target="_blank" rel="noopener" title="Acta"
      class="inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-semibold bg-purple-50 text-purple-700 border border-purple-200 hover:bg-purple-100 transition">
      📝 Acta</a>`);
  }
  const docsHTML = docs.length ? `<div class="flex flex-wrap gap-1">${docs.join("")}</div>` : `<span class="text-xs text-gray-300">—</span>`;

  // ── Presentaciones (buscar campos: Presentacion, Presentaciones, Informe, MaterialApoyo, etc.) ──
  const pres = [];
  const presFields = [
    { key: "Presentacion",   label: "📑 Presentación",   color: "amber" },
    { key: "Presentaciones", label: "📑 Presentaciones",  color: "amber" },
    { key: "Informe",        label: "📰 Informe",         color: "orange" },
    { key: "MaterialApoyo",  label: "🗂 Material",         color: "teal" },
    { key: "Anexo",          label: "📎 Anexo",            color: "teal" },
  ];
  presFields.forEach(({ key, label, color }) => {
    const val = s[key] || s[key.toLowerCase()] || "";
    if (val && val !== "No" && val.startsWith("http")) {
      pres.push(`<a href="${val}" target="_blank" rel="noopener"
        class="inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-semibold bg-${color}-50 text-${color}-700 border border-${color}-200 hover:bg-${color}-100 transition">
        ${label}</a>`);
    }
  });
  const presHTML = pres.length ? `<div class="flex flex-wrap gap-1">${pres.join("")}</div>` : `<span class="text-xs text-gray-300">—</span>`;

  // ── Video (Senado) ──
  const videoURL = s.URL_Video || s.url_video || "";
  const videoCell = videoURL
    ? `<a href="${videoURL}" target="_blank" class="inline-flex items-center px-2 py-1 rounded text-xs font-semibold bg-red-50 text-red-700 border border-red-200 hover:bg-red-100">🎥 Ver</a>`
    : `<span class="text-gray-300">—</span>`;

  const isSenado = (window._currentComCamara || "") === "senado";

  return `
    <tr class="border-t border-gray-100 hover:bg-gray-50 transition">
      <td class="py-2 px-2 text-xs text-gray-500 whitespace-nowrap">${s.Mes || ""}</td>
      <td class="py-2 px-2 text-xs font-mono text-gray-700 whitespace-nowrap">${s.Fecha || ""}</td>
      <td class="py-2 px-2">${estadoBadge}</td>
      <td class="py-2 px-2">${docsHTML}</td>
      <td class="py-2 px-2">${presHTML}</td>
      <td class="py-2 px-2 text-center">${transcript}</td>
      ${isSenado ? `<td class="py-2 px-2 text-center">${videoCell}</td>` : ""}
    </tr>
  `;
}

function renderCommissionYears(c, camaraActual) {
  // Actualizar variable global para que renderSessionRow sepa
  window._currentComCamara = camaraActual || "diputados";
  const isSenado = window._currentComCamara === "senado";

  return c.years.map(year => {
    const sessions = c.sessions_by_year[year] || [];
    if (!sessions.length) {
      return `
        <div class="bg-white rounded-xl border border-gray-200 p-4">
          <div class="flex items-center justify-between mb-2">
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
            <thead class="text-gray-500 border-b border-gray-200 bg-gray-50">
              <tr>
                <th class="text-left py-2 px-2 w-16 font-semibold">Mes</th>
                <th class="text-left py-2 px-2 w-28 font-semibold">Fecha</th>
                <th class="text-left py-2 px-2 w-28 font-semibold">Estado</th>
                <th class="text-left py-2 px-2 font-semibold">📋 Documentos</th>
                <th class="text-left py-2 px-2 font-semibold">📑 Presentaciones</th>
                <th class="text-center py-2 px-2 w-12 font-semibold">TXT</th>
                ${isSenado ? '<th class="text-center py-2 px-2 w-16 font-semibold">🎥</th>' : ""}
              </tr>
            </thead>
            <tbody>
              ${rows}
            </tbody>
          </table>
          ${sessions.length > 100 ? `<div class="text-xs text-gray-400 mt-2 text-center">Mostrando 100 de ${sessions.length} sesiones</div>` : ""}
        </div>
      </div>
    `;
  }).join("");
}

// ─── Politicians ──────────────────────────────────────────────
let currentPolCamara = "all";

function setPolCamara(c) {
  currentPolCamara = c;
  const configs = {
    all:    { id: "polBtn-all",       cls: "bg-gray-700 text-white border-gray-700",     label: "Todas las cámaras" },
    camara: { id: "polBtn-diputados", cls: "bg-purple-600 text-white border-purple-600", label: "🟣 Diputados" },
    senado: { id: "polBtn-senado",    cls: "bg-red-600 text-white border-red-600",        label: "🔴 Senado" },
  };

  ["polBtn-all", "polBtn-diputados", "polBtn-senado"].forEach(id => {
    const el = $(id);
    if (el) el.className = "text-xs px-3 py-1.5 rounded-lg font-bold bg-white text-gray-500 border-2 border-gray-200";
  });

  const cfg = configs[c] || configs.all;
  const btn = $(cfg.id);
  if (btn) btn.className = `text-xs px-3 py-1.5 rounded-lg font-bold border-2 ${cfg.cls}`;
  const lbl = $("polCamaraLabel");
  if (lbl) lbl.textContent = cfg.label;

  const grid = $("politiciansGrid");
  if (grid) grid.innerHTML = `<div class="col-span-3 text-xs text-gray-400 py-4 text-center">Cargando ${cfg.label}…</div>`;

  loadPoliticians(($("politicianSearch")?.value || "").trim());
}

async function loadPoliticians(q = "") {
  const el = $("politiciansGrid");
  if (el) el.innerHTML = '<div class="col-span-3 text-xs text-gray-400 py-4 text-center">Cargando…</div>';
  try {
    // FIX: usar "camara" (no "chamber") como parámetro del backend
    const data = await apiGet(`/api/politicians?q=${encodeURIComponent(q)}&camara=${currentPolCamara}`);
    const items = data.politicians || [];
    renderPoliticians(items);
  } catch (e) {
    if (el) el.innerHTML = `<div class="col-span-3 text-sm text-red-500 p-4">Error: ${e.message}</div>`;
  }
}

function renderComisionesBadges(comisiones, max = 2) {
  if (!comisiones || !comisiones.length) return "";
  const shown = comisiones.slice(0, max);
  const rest = comisiones.length - max;
  let html = shown.map(c => {
    const nombre = c.commission_name.replace(/^Comisi[oó]n de /i, "").slice(0, 24);
    return `<span class="inline-block text-xs px-2 py-0.5 rounded-full bg-gray-100 text-gray-600 truncate max-w-[130px]" title="${c.commission_name}">${nombre}</span>`;
  }).join("");
  if (rest > 0) html += `<span class="text-xs text-gray-400">+${rest} más</span>`;
  return `<div class="flex flex-wrap gap-1 mt-1">${html}</div>`;
}

function renderPoliticians(items) {
  const el = $("politiciansGrid");
  if (!el) return;

  if (!items.length) {
    const msg = currentPolCamara === "senado"
      ? `<div class="col-span-3 text-sm text-gray-400 p-4 bg-red-50 rounded-xl border border-red-100">
           <div class="font-semibold text-red-700 mb-1">🔴 Sin senadores indexados</div>
           <div>Ejecuta <code>scraper_senado.py</code> para generar el <code>REPO_SENADO/</code>.</div>
         </div>`
      : `<div class="col-span-3 text-sm text-gray-500 py-8 text-center">Sin resultados</div>`;
    el.innerHTML = msg;
    return;
  }

  el.innerHTML = "";
  items.forEach(p => {
    const isSenado = (p.chamber || "").toLowerCase() === "senado";
    const badgeCls = isSenado ? "bg-red-100 text-red-700" : "bg-purple-100 text-purple-700";
    const badgeTxt = isSenado ? "Senador/a" : "Diputado/a";
    const initials = p.nombre.split(" ").slice(0, 2).map(x => x[0] || "").join("").toUpperCase();
    const bgColor = isSenado ? "bg-red-600" : "bg-purple-600";
    const borderColor = isSenado ? "border-red-200" : "border-purple-200";

    // Foto oficial
    const fotoUrl = getFotoFromFicha(p.url_ficha);
    const avatarEl = fotoUrl
      ? `<div class="w-14 h-14 shrink-0 rounded-full overflow-hidden border-2 ${borderColor} shadow-sm">
           <img src="${fotoUrl}" alt="${p.nombre}" class="w-full h-full object-cover object-top"
                onerror="this.parentElement.innerHTML='<div class=\\'w-full h-full ${bgColor} text-white flex items-center justify-center font-bold text-sm\\'>${initials}</div>'" />
         </div>`
      : `<div class="w-14 h-14 shrink-0 rounded-full ${bgColor} text-white flex items-center justify-center font-bold text-sm border-2 ${borderColor}">${initials}</div>`;

    const comBadges = renderComisionesBadges(p.comisiones || []);
    const totalComs = (p.comisiones || []).length;

    const card = document.createElement("div");
    card.className = "bg-white rounded-2xl shadow-sm border border-gray-100 p-4 hover:shadow-md hover:-translate-y-0.5 transition-all duration-200 flex flex-col justify-between";
    card.innerHTML = `
      <div>
        <div class="flex items-center gap-3 mb-2">
          ${avatarEl}
          <div class="min-w-0 flex-1">
            <div class="font-extrabold text-gray-900 leading-tight line-clamp-2">${p.nombre}</div>
            <div class="flex items-center gap-1 mt-1 flex-wrap">
              <span class="text-xs font-bold px-2 py-0.5 rounded-full ${badgeCls}">${badgeTxt}</span>
              ${p.cargo ? `<span class="text-xs text-gray-500">${p.cargo}</span>` : ""}
            </div>
          </div>
        </div>
        ${comBadges}
        ${totalComs > 0 ? `<div class="text-xs text-gray-400 mt-1">${totalComs} comisión(es)</div>` : ""}
      </div>
      <div class="mt-3 flex gap-2">
        ${p.url_ficha
          ? `<a class="flex-1 text-center text-xs px-3 py-2 rounded-lg bg-gray-100 hover:bg-gray-200 text-gray-700 font-semibold transition"
                href="${p.url_ficha}" target="_blank" rel="noopener">Ver Perfil Oficial ↗</a>`
          : `<div class="flex-1"></div>`}
        <button class="flex-1 text-xs px-3 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white font-semibold transition kom-btn"
          data-chamber="${p.chamber || "camara"}"
          data-id="${p.id || ""}"
          data-nombre="${encodeURIComponent(p.nombre)}"
          data-cargo="${encodeURIComponent(p.cargo || "Parlamentario")}"
          data-url="${encodeURIComponent(p.url_ficha || "")}">
          📋 Perfil KOM
        </button>
      </div>
    `;
    el.appendChild(card);
  });

  // Bind KOM buttons
  el.querySelectorAll(".kom-btn").forEach(btn => {
    btn.addEventListener("click", function () {
      openKomModal(
        btn.dataset.chamber,
        btn.dataset.id,
        btn.dataset.nombre,
        btn.dataset.cargo,
        btn.dataset.url
      );
    });
  });
}

// ─── KOM Modal ────────────────────────────────────────────────
let currentKomProfile = null;
let editingTopicoIdx = null;

async function openKomModal(chamber, id, nameEnc, cargoEnc, urlEnc) {
  const nombre = decodeURIComponent(nameEnc || "");
  const cargo = decodeURIComponent(cargoEnc || "Parlamentario");
  const urlFicha = decodeURIComponent(urlEnc || "");

  chamber = (chamber || "camara").toString().trim();
  id = (id || nombre).toString().trim();

  // Header
  const isSenado = chamber === "senado";
  const fotoUrl = getFotoFromFicha(urlFicha);
  const initials = nombre.split(" ").slice(0, 2).map(x => x[0] || "").join("").toUpperCase();

  // Foto en el modal
  const komFotoDiv = $("komFotoContainer");
  if (komFotoDiv) {
    if (fotoUrl) {
      komFotoDiv.innerHTML = `
        <img src="${fotoUrl}" alt="${nombre}"
             class="w-24 h-24 rounded-2xl object-cover object-top border-4 ${isSenado ? "border-red-200" : "border-purple-200"} shadow-lg"
             onerror="this.parentElement.innerHTML='<div class=\\'w-24 h-24 rounded-2xl ${isSenado ? "bg-red-600" : "bg-purple-600"} text-white flex items-center justify-center font-bold text-2xl shadow-lg\\'>${initials}</div>'" />
      `;
    } else {
      komFotoDiv.innerHTML = `
        <div class="w-24 h-24 rounded-2xl ${isSenado ? "bg-red-600" : "bg-purple-600"} text-white flex items-center justify-center font-bold text-2xl shadow-lg">${initials}</div>
      `;
    }
  }

  // Nombre y badge
  const komNombre = $("komNombre");
  if (komNombre) komNombre.textContent = nombre;
  const komCargo = $("komCargo");
  if (komCargo) komCargo.textContent = cargo;
  const komBadge = $("komCamaraBadge");
  if (komBadge) {
    komBadge.innerHTML = isSenado
      ? `<span class="text-xs font-bold px-3 py-1 rounded-full bg-red-100 text-red-700">🔴 Senado</span>`
      : `<span class="text-xs font-bold px-3 py-1 rounded-full bg-purple-100 text-purple-700">🟣 Diputados</span>`;
  }

  // Link oficial
  const komLink = $("komLinkOficial");
  if (komLink) {
    komLink.innerHTML = urlFicha
      ? `<a href="${urlFicha}" target="_blank" rel="noopener" class="text-xs text-indigo-600 hover:underline font-semibold">🔗 Ver perfil oficial ↗</a>`
      : "";
  }

  // Cargar datos guardados
  try {
    const data = await apiGet(`/api/kom/${encodeURIComponent(chamber)}/${encodeURIComponent(id)}`);
    const profile = data.profile || {};

    currentKomProfile = {
      chamber, id, nombre, cargo,
      foto_url:   profile.foto_url   || fotoUrl || "",
      biografia:  profile.biografia  || "",
      email:      profile.email      || "",
      telefono:   profile.telefono   || "",
      web:        profile.web        || "",
      topicos:    profile.topicos    || [],
      notas:      profile.notas      || profile.notes || "",
      links:      profile.links      || [],
    };

    if ($("komFotoUrl"))    $("komFotoUrl").value   = currentKomProfile.foto_url;
    if ($("komBiografia"))  $("komBiografia").value = currentKomProfile.biografia;
    if ($("komEmail"))      $("komEmail").value      = currentKomProfile.email;
    if ($("komTelefono"))   $("komTelefono").value   = currentKomProfile.telefono;
    if ($("komWeb"))        $("komWeb").value         = currentKomProfile.web;
    if ($("komNotas"))      $("komNotas").value       = currentKomProfile.notas;

    // Si hay foto_url guardada, actualizar
    if (currentKomProfile.foto_url && komFotoDiv) {
      komFotoDiv.innerHTML = `
        <img src="${currentKomProfile.foto_url}" alt="${nombre}"
             class="w-24 h-24 rounded-2xl object-cover object-top border-4 ${isSenado ? "border-red-200" : "border-purple-200"} shadow-lg"
             onerror="this.parentElement.innerHTML='<div class=\\'w-24 h-24 rounded-2xl ${isSenado ? "bg-red-600" : "bg-purple-600"} text-white flex items-center justify-center font-bold text-2xl shadow-lg\\'>${initials}</div>'" />
      `;
    }

    renderKomTopicos();
    renderKomLinks();

    $("komModal").classList.remove("hidden");
    $("komModal").classList.add("flex");
  } catch (e) {
    console.error("Error cargando perfil KOM:", e);
    alert(`Error al cargar perfil KOM (${chamber}/${id}): ${e.message}`);
  }
}

function closeKomModal() {
  $("komModal").classList.add("hidden");
  $("komModal").classList.remove("flex");
  currentKomProfile = null;
  editingTopicoIdx = null;
}

function updateKomFoto() {
  const url = ($("komFotoUrl")?.value || "").trim();
  if (currentKomProfile) currentKomProfile.foto_url = url;
  const fotoDiv = $("komFotoContainer");
  if (!fotoDiv) return;
  if (url) {
    fotoDiv.innerHTML = `<img src="${url}" alt="Foto" class="w-24 h-24 rounded-2xl object-cover border-4 border-indigo-200 shadow-lg" onerror="this.parentElement.innerHTML='<div class=\\'w-24 h-24 rounded-2xl bg-gray-400 text-white flex items-center justify-center font-bold text-2xl\\'>?</div>'" />`;
  }
}

function renderKomTopicos() {
  const el = $("komTopicos");
  if (!el || !currentKomProfile) return;
  if (!currentKomProfile.topicos.length) {
    el.innerHTML = `<div class="text-xs text-gray-400 p-3 bg-gray-50 rounded-lg border border-dashed border-gray-200">Sin tópicos agregados</div>`;
    return;
  }
  el.innerHTML = currentKomProfile.topicos.map((t, idx) => `
    <div class="bg-indigo-50 border border-indigo-200 rounded-xl p-3">
      <div class="flex items-start justify-between gap-2">
        <div class="flex-1">
          <div class="font-bold text-gray-900 text-sm">${t.titulo}</div>
          ${t.descripcion ? `<div class="text-xs text-gray-600 mt-1 line-clamp-2">${t.descripcion}</div>` : ""}
        </div>
        <div class="flex gap-1 shrink-0">
          <button onclick="window.editTopico(${idx})" class="text-xs px-2 py-1 rounded bg-white border hover:bg-gray-50 text-gray-600">✏️</button>
          <button onclick="window.removeTopico(${idx})" class="text-xs px-2 py-1 rounded bg-white border hover:bg-red-50 text-red-600">✕</button>
        </div>
      </div>
    </div>
  `).join("");
}

function showTopicoEditor(idx = null) {
  editingTopicoIdx = idx;
  const editor = $("topicoEditor");
  if (!editor) return;
  if (idx !== null && currentKomProfile) {
    const t = currentKomProfile.topicos[idx];
    if ($("topicoTitulo")) $("topicoTitulo").value = t.titulo || "";
    if ($("topicoContenido")) $("topicoContenido").value = t.descripcion || "";
  } else {
    if ($("topicoTitulo")) $("topicoTitulo").value = "";
    if ($("topicoContenido")) $("topicoContenido").value = "";
  }
  editor.classList.remove("hidden");
}

function editTopico(idx) { showTopicoEditor(idx); }

function saveTopico() {
  if (!currentKomProfile) return;
  const titulo = ($("topicoTitulo")?.value || "").trim();
  const descripcion = ($("topicoContenido")?.value || "").trim();
  if (!titulo) { alert("Ingresa un título"); return; }

  if (editingTopicoIdx !== null) {
    currentKomProfile.topicos[editingTopicoIdx] = { titulo, descripcion };
  } else {
    currentKomProfile.topicos.push({ titulo, descripcion });
  }
  hideTopicoEditor();
  renderKomTopicos();
}

function removeTopico(idx) {
  if (!currentKomProfile) return;
  currentKomProfile.topicos.splice(idx, 1);
  renderKomTopicos();
}

function hideTopicoEditor() {
  $("topicoEditor")?.classList.add("hidden");
  editingTopicoIdx = null;
}

function renderKomLinks() {
  const el = $("komLinks");
  if (!el || !currentKomProfile) return;
  if (!currentKomProfile.links.length) {
    el.innerHTML = `<div class="text-xs text-gray-400 p-2 bg-gray-50 rounded-lg border border-dashed border-gray-200">Sin enlaces</div>`;
    return;
  }
  el.innerHTML = currentKomProfile.links.map((link, idx) => `
    <div class="flex items-center gap-2 p-2 bg-gray-50 rounded-lg border border-gray-200">
      <a href="${link.url}" target="_blank" class="flex-1 text-sm text-indigo-700 hover:underline truncate">${link.title}</a>
      <button onclick="window.removeKomLink(${idx})" class="shrink-0 text-red-500 hover:text-red-700 text-xs font-semibold px-2">✕</button>
    </div>
  `).join("");
}

function addKomLink() {
  const title = ($("komNewLinkTitle")?.value || "").trim();
  const url = ($("komNewLinkUrl")?.value || "").trim();
  if (!title || !url) { alert("Completa título y URL"); return; }
  if (!currentKomProfile) return;
  currentKomProfile.links.push({ title, url });
  if ($("komNewLinkTitle")) $("komNewLinkTitle").value = "";
  if ($("komNewLinkUrl")) $("komNewLinkUrl").value = "";
  renderKomLinks();
}

function removeKomLink(idx) {
  if (!currentKomProfile) return;
  currentKomProfile.links.splice(idx, 1);
  renderKomLinks();
}

async function saveKomProfile() {
  if (!currentKomProfile) return;
  currentKomProfile.biografia = $("komBiografia")?.value || "";
  currentKomProfile.email     = $("komEmail")?.value     || "";
  currentKomProfile.telefono  = $("komTelefono")?.value  || "";
  currentKomProfile.web       = $("komWeb")?.value       || "";
  currentKomProfile.notas     = $("komNotas")?.value     || "";
  currentKomProfile.foto_url  = $("komFotoUrl")?.value   || currentKomProfile.foto_url || "";

  try {
    await apiPostJSON(`/api/kom/${currentKomProfile.chamber}/${currentKomProfile.id}`, {
      foto_url:  currentKomProfile.foto_url,
      biografia: currentKomProfile.biografia,
      email:     currentKomProfile.email,
      telefono:  currentKomProfile.telefono,
      web:       currentKomProfile.web,
      topicos:   currentKomProfile.topicos,
      notas:     currentKomProfile.notas,
      links:     currentKomProfile.links,
      nombre:    currentKomProfile.nombre,
    });
    // Feedback visual
    const btn = $("komSaveBtn");
    if (btn) {
      btn.textContent = "✓ Guardado";
      btn.classList.add("bg-green-600");
      btn.classList.remove("bg-indigo-600");
      setTimeout(() => {
        btn.textContent = "Guardar perfil";
        btn.classList.remove("bg-green-600");
        btn.classList.add("bg-indigo-600");
      }, 2000);
    }
  } catch (e) {
    alert(`Error al guardar: ${e.message}`);
  }
}

// ─── Expose globals ───────────────────────────────────────────
window.openKomModal    = openKomModal;
window.closeKomModal   = closeKomModal;
window.updateKomFoto   = updateKomFoto;
window.showTopicoEditor = showTopicoEditor;
window.editTopico      = editTopico;
window.saveTopico      = saveTopico;
window.removeTopico    = removeTopico;
window.hideTopicoEditor = hideTopicoEditor;
window.addKomLink      = addKomLink;
window.removeKomLink   = removeKomLink;
window.saveKomProfile  = saveKomProfile;
window.setPolCamara    = setPolCamara;
window.loadPoliticians = loadPoliticians;
window.renderCommissionYears = renderCommissionYears;
window.renderSessionRow = renderSessionRow;
window.getFotoFromFicha = getFotoFromFicha;